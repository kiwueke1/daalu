"""Terminal coding-assistant REPL — Claude-Code-style.

Lives inside the workspace pod. The user opens a terminal in code-server
and types ``daalu chat``; this module is the entry point invoked by
:func:`daalu_automation.cli.chat`.

Architecture mirrors the VS Code Daalu Assistant extension
(``extensions/daalu-assistant/``) so the two surfaces converge on the
same prompt + tool conventions:

* OpenAI-compatible POST to the daalu inference gateway with the
  service-token JWT minted by the token-minter sidecar.
* The model emits *tool requests* as machine-readable blocks. The CLI
  parses them, asks the human for confirmation on writes/runs, executes,
  and feeds the results back as a follow-up user turn. That's the
  agent loop.

Three tools, one approval per side-effect:

  ``daalu:read <relative path>``
      The CLI prints the file and forwards it back to the model on the
      next turn. No prompt — reading the user's own files is implicit.

  ``daalu:write <relative path>``
      Shows a unified diff against the existing contents (or "new file")
      and prompts y/N before writing. Refusal feeds an "I declined"
      message back into the conversation so the model can adjust.

  ``daalu:run <shell command>``
      Runs in the workspace root, 60-second timeout, output captured.
      Prompts y/N before each run. Same refusal-feedback pattern.

The tools are deliberately not OpenAI function-calling — the gateway's
backing models don't all implement it reliably, and the prompt-driven
convention works with any chat-completion endpoint.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover — daalu image always has it
    httpx = None  # type: ignore[assignment]


_SYSTEM_PROMPT = """\
You are Daalu Assistant running as a terminal REPL inside the user's \
workspace pod. The user is editing code; help them.

You can request three actions by emitting them on their own lines, \
exactly as written below. The CLI parses these and asks the user for \
permission on writes/runs.

  daalu:read <workspace-relative-path>
      The CLI will reply with the file contents on the next turn.

  daalu:write <workspace-relative-path>
  ```<language>
  <new file contents>
  ```
      Followed immediately by a fenced code block. The CLI prompts the \
      user y/N with a diff before writing.

  daalu:run <shell command>
      Runs from the workspace root with a 60-second timeout. The CLI \
      prompts y/N first. Output is fed back to you.

Rules:
- Emit at most one tool request per turn. Wait for the result.
- Never propose ``rm -rf`` or other destructive commands without an \
  explicit user ask.
- Never write outside the workspace root.
- When the user asks a question without asking for code, just answer.

Be terse. The terminal is narrow.
"""

_MAX_TOOL_CALLS = 20


@dataclass
class _ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class _Config:
    workspace_root: Path
    gateway_url: str
    token_file: Path
    model: str
    max_tokens: int = 2048
    history: list[_ChatMessage] = field(default_factory=list)


def run_chat(
    *,
    workspace_root: str,
    gateway_url: str,
    token_file: str,
    model: str,
) -> None:
    """Top-level entry called by the ``daalu chat`` CLI command."""
    if httpx is None:
        sys.stderr.write("daalu chat requires httpx (`pip install httpx`)\n")
        sys.exit(2)

    gateway_url = (gateway_url or os.environ.get("DAALU_GATEWAY_URL") or "").rstrip("/")
    if not gateway_url:
        sys.stderr.write(
            "error: no DAALU_GATEWAY_URL — this CLI only works inside a "
            "workspace pod where the env is injected by workspace-controller.\n"
        )
        sys.exit(2)

    cfg = _Config(
        workspace_root=Path(workspace_root).resolve(),
        gateway_url=gateway_url,
        token_file=Path(token_file),
        model=model,
        history=[_ChatMessage(role="system", content=_SYSTEM_PROMPT)],
    )
    if not cfg.workspace_root.exists():
        sys.stderr.write(f"workspace root does not exist: {cfg.workspace_root}\n")
        sys.exit(2)

    _banner(cfg)
    while True:
        try:
            line = input("\033[1;36mdaalu›\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line == "/quit" or line == "/exit":
            return
        if line == "/reset":
            cfg.history = [cfg.history[0]]
            print("(history cleared)")
            continue
        if line == "/help":
            _help()
            continue

        cfg.history.append(_ChatMessage(role="user", content=line))
        _drive_turn(cfg)


# ── presentation ────────────────────────────────────────────────────────


def _banner(cfg: _Config) -> None:
    print("\n\033[1;32mdaalu chat\033[0m — coding assistant (terminal mode)")
    print(f"workspace:  {cfg.workspace_root}")
    print(f"gateway:    {cfg.gateway_url}")
    print(f"model:      {cfg.model}")
    print("commands:   /reset  /help  /quit\n")


def _help() -> None:
    print(
        "  /quit    end the session\n"
        "  /reset   clear conversation history\n"
        "  /help    this message\n"
    )


# ── one round-trip + tool loop ──────────────────────────────────────────


def _drive_turn(cfg: _Config) -> None:
    """Send the current history, parse tool requests, loop until done.

    A single user turn may produce multiple assistant turns: each tool
    request is executed, its result is appended as a synthetic user
    message, and the model is asked to continue. Loop terminates when
    the assistant emits a turn with no tool request.

    Capped at 20 tool calls per user turn so a misbehaving model can't
    spin forever. When the cap is hit, we ask the model for one final,
    tool-less answer instead of returning silently.
    """
    for _ in range(_MAX_TOOL_CALLS):
        text = _request_completion(cfg)
        if text is None:
            return
        cfg.history.append(_ChatMessage(role="assistant", content=text))
        _print_assistant(text)
        tool, tail = _extract_tool_request(text)
        if tool is None:
            return
        result = _execute_tool(cfg, tool, tail)
        if result is None:
            # User declined or tool produced no follow-up — let them
            # type the next prompt rather than spinning.
            return
        cfg.history.append(_ChatMessage(role="user", content=result))
    # Tool-call budget exhausted. Don't vanish silently: ask the model to
    # stop using tools and answer with what it has already gathered.
    cfg.history.append(
        _ChatMessage(
            role="user",
            content=(
                "(tool-call budget reached — stop using tools and answer now "
                "with what you've gathered.)"
            ),
        )
    )
    final = _request_completion(cfg)
    if final:
        cfg.history.append(_ChatMessage(role="assistant", content=final))
        _print_assistant(final)
    else:
        print("(reached tool-call limit; type a follow-up to keep going)")


def _request_completion(cfg: _Config) -> str | None:
    token = _read_token(cfg.token_file)
    if token is None:
        return None
    body = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "messages": [
            {"role": m.role, "content": m.content} for m in cfg.history
        ],
    }
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{cfg.gateway_url}/v1/chat/completions",
                headers={
                    "authorization": f"Bearer {token}",
                    "content-type": "application/json",
                },
                content=json.dumps(body),
            )
    except httpx.HTTPError as e:
        sys.stderr.write(f"gateway unreachable: {e}\n")
        return None
    if resp.status_code != 200:
        snippet = resp.text[:200]
        sys.stderr.write(f"gateway returned {resp.status_code}: {snippet}\n")
        return None
    data: dict[str, Any] = resp.json()
    choice = (data.get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or ""


def _read_token(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError as e:
        sys.stderr.write(
            f"cannot read service token at {path}: {e}\n"
            "  the token-minter sidecar may not be running yet.\n"
        )
        return None


# ── tool parsing + execution ────────────────────────────────────────────


_TOOL_LINE = re.compile(
    r"^daalu:(?P<verb>read|write|run)\s+(?P<arg>.+?)\s*$",
    re.MULTILINE,
)


def _extract_tool_request(text: str) -> tuple[tuple[str, str] | None, str]:
    """Find the first tool request in the assistant's response.

    Returns ``((verb, argline), trailing_text)`` or ``(None, text)`` if
    no tool was requested.
    """
    m = _TOOL_LINE.search(text)
    if not m:
        return None, text
    return (m.group("verb"), m.group("arg")), text[m.end():]


def _execute_tool(cfg: _Config, tool: tuple[str, str], tail: str) -> str | None:
    verb, arg = tool
    if verb == "read":
        return _tool_read(cfg, arg.strip())
    if verb == "write":
        return _tool_write(cfg, arg.strip(), tail)
    if verb == "run":
        return _tool_run(cfg, arg.strip())
    return None


def _safe_path(cfg: _Config, rel: str) -> Path | None:
    """Resolve ``rel`` under the workspace root, refusing any escape."""
    p = (cfg.workspace_root / rel).resolve()
    try:
        p.relative_to(cfg.workspace_root)
    except ValueError:
        print(f"\033[31m(refusing path outside workspace: {rel})\033[0m")
        return None
    return p


def _tool_read(cfg: _Config, rel: str) -> str | None:
    p = _safe_path(cfg, rel)
    if p is None or not p.exists():
        return f"(daalu) read failed: no such file: {rel}"
    try:
        text = p.read_text()
    except OSError as e:
        return f"(daalu) read failed: {e}"
    print(f"\033[2m(read {rel}, {len(text)} chars)\033[0m")
    # Truncate large files so we don't blow the context window.
    if len(text) > 16_000:
        text = text[:16_000] + "\n…(truncated)\n"
    return f"Contents of `{rel}`:\n\n```\n{text}\n```"


def _tool_write(cfg: _Config, rel: str, tail: str) -> str | None:
    p = _safe_path(cfg, rel)
    if p is None:
        return f"(daalu) write declined: unsafe path: {rel}"
    # Pull the first fenced block out of `tail`.
    m = re.search(r"```[^\n]*\n([\s\S]*?)```", tail)
    if not m:
        return f"(daalu) write failed: no fenced code block after daalu:write {rel}"
    new_content = m.group(1)

    before = p.read_text() if p.exists() else ""
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=3,
        )
    )
    print("\n\033[33m── proposed write ──\033[0m")
    print(diff or "(file is identical)")
    if before and diff == "":
        return f"(daalu) no-op: {rel} would be unchanged"
    if not _confirm(f"apply write to {rel}?"):
        return f"(daalu) user declined to write {rel}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new_content)
    print(f"\033[32m✓ wrote {rel} ({len(new_content)} chars)\033[0m")
    return f"(daalu) wrote `{rel}` ({len(new_content)} chars)"


def _tool_run(cfg: _Config, command: str) -> str | None:
    print(f"\n\033[33m── proposed run ──\033[0m\n  $ {command}")
    if not _confirm("run this command?"):
        return f"(daalu) user declined to run `{command}`"
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cfg.workspace_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return f"(daalu) command timed out after 60s: `{command}`"
    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    out = out[-4000:]  # cap so the context doesn't blow up
    head = f"exit {proc.returncode}"
    print(f"\033[2m({head}, {len(out)} chars captured)\033[0m")
    return f"(daalu) ran `{command}` ({head}):\n\n```\n{out}\n```"


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"\033[1m{prompt} [y/N] \033[0m").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def _print_assistant(text: str) -> None:
    print()
    print(text)
    print()


# Re-exported for shlex test parity — not used internally.
__all__ = ["run_chat"]
_ = shlex  # keep import in case future tools want word-splitting

"""The terminal coding agent — Claude-Code-style REPL.

Mirrors the in-pod ``daalu chat`` (workspace_controller/chat_repl.py) so both
surfaces share one prompt + tool convention, but here:

* the **workspace root is the current working directory** — any local repo;
* inference goes through the public coding gateway via :class:`DaaluClient`
  (the user's PAT), not an in-cluster service-token.

Three tools, one approval per side-effect:

  ``daalu:read <path>``    print the file + forward it to the model (no prompt)
  ``daalu:write <path>``   show a diff, prompt y/N, then write
  ``daalu:run <command>``  prompt y/N, run in the project root, feed output back

The tools are prompt-driven text pragmas (not OpenAI function-calling) so they
work against any chat-completion model the gateway serves.
"""

from __future__ import annotations

import difflib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from daalu_cli.client import ApiError, DaaluClient

_SYSTEM_PROMPT = """\
You are Daalu, a coding assistant running as a terminal REPL in the user's \
project directory. The user is editing code; help them.

You can request three actions by emitting them on their own lines, exactly as \
written below. The CLI parses these and asks the user for permission on \
writes/runs.

  daalu:read <path>
      The CLI replies with the file contents on the next turn.

  daalu:write <path>
  ```<language>
  <new file contents>
  ```
      Followed immediately by a fenced code block. The CLI shows a diff and \
      prompts y/N before writing.

  daalu:run <shell command>
      Runs from the project root with a 60-second timeout. The CLI prompts y/N \
      first. Output is fed back to you.

Rules:
- Emit at most one tool request per turn. Wait for the result.
- Paths are relative to the project root; never write outside it.
- Never propose destructive commands (rm -rf, etc.) without an explicit ask.
- When the user asks a question without asking for code, just answer.

Be terse. The terminal is narrow.
"""

_MAX_TOOL_CALLS = 20
_TOOL_LINE = re.compile(
    r"^daalu:(?P<verb>read|write|run)\s+(?P<arg>.+?)\s*$", re.MULTILINE
)


@dataclass
class _Msg:
    role: str
    content: str


@dataclass
class _Session:
    root: Path
    client: DaaluClient
    model: str
    max_tokens: int = 2048
    history: list[_Msg] = field(default_factory=list)


def run(*, root: Path, client: DaaluClient, model: str) -> int:
    """Top-level REPL. Returns a process exit code."""
    sess = _Session(
        root=root.resolve(),
        client=client,
        model=model,
        history=[_Msg("system", _SYSTEM_PROMPT)],
    )
    _banner(sess)
    while True:
        try:
            line = input("\033[1;36mdaalu›\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return 0
        if line == "/reset":
            sess.history = [sess.history[0]]
            print("(history cleared)")
            continue
        if line == "/help":
            _help()
            continue
        sess.history.append(_Msg("user", line))
        _drive_turn(sess)


# ── presentation ────────────────────────────────────────────────────────────


def _banner(sess: _Session) -> None:
    print("\n\033[1;32mdaalu\033[0m — coding agent (terminal)")
    print(f"project:  {sess.root}")
    print(f"host:     {sess.client.host}")
    print(f"model:    {sess.model}")
    print("commands: /reset  /help  /quit\n")


def _help() -> None:
    print(
        "  /quit    end the session\n"
        "  /reset   clear conversation history\n"
        "  /help    this message\n"
    )


# ── one turn + tool loop ─────────────────────────────────────────────────────


def _drive_turn(sess: _Session) -> None:
    for _ in range(_MAX_TOOL_CALLS):
        text = _complete(sess)
        if text is None:
            return
        sess.history.append(_Msg("assistant", text))
        _print_assistant(text)
        tool, tail = _extract_tool(text)
        if tool is None:
            return
        result = _execute(sess, tool, tail)
        if result is None:
            return
        sess.history.append(_Msg("user", result))
    # Tool-call budget exhausted. Don't vanish silently: ask the model to stop
    # using tools and answer with what it has already gathered.
    sess.history.append(
        _Msg(
            "user",
            "(tool-call budget reached — stop using tools and answer now "
            "with what you've gathered.)",
        )
    )
    final = _complete(sess)
    if final:
        sess.history.append(_Msg("assistant", final))
        _print_assistant(final)
    else:
        print(
            "\033[2m(reached tool-call limit; type a follow-up to keep going)"
            "\033[0m"
        )


def _complete(sess: _Session) -> str | None:
    body = {
        "model": sess.model,
        "max_tokens": sess.max_tokens,
        "messages": [{"role": m.role, "content": m.content} for m in sess.history],
    }
    try:
        data = sess.client.chat_completion(body)
    except ApiError as e:
        if e.status == 401:
            sys.stderr.write("session expired — run `daalu login` again.\n")
        elif e.status == 403:
            sys.stderr.write("coding is not enabled for your tenant.\n")
        elif e.status == 429:
            sys.stderr.write("rate limited / over quota — try again shortly.\n")
        else:
            sys.stderr.write(f"inference error ({e.status}): {e.detail}\n")
        return None
    except Exception as e:  # noqa: BLE001 — network/transport
        sys.stderr.write(f"could not reach inference: {e}\n")
        return None
    choice = (data.get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or ""


# ── tool parsing + execution ─────────────────────────────────────────────────


def _extract_tool(text: str):
    m = _TOOL_LINE.search(text)
    if not m:
        return None, text
    return (m.group("verb"), m.group("arg").strip()), text[m.end() :]


def _execute(sess: _Session, tool, tail: str) -> str | None:
    verb, arg = tool
    if verb == "read":
        return _read(sess, arg)
    if verb == "write":
        return _write(sess, arg, tail)
    if verb == "run":
        return _run(sess, arg)
    return None


def _safe_path(sess: _Session, rel: str) -> Path | None:
    p = (sess.root / rel).resolve()
    try:
        p.relative_to(sess.root)
    except ValueError:
        print(f"\033[31m(refusing path outside project: {rel})\033[0m")
        return None
    return p


def _read(sess: _Session, rel: str) -> str | None:
    p = _safe_path(sess, rel)
    if p is None or not p.exists():
        return f"(daalu) read failed: no such file: {rel}"
    try:
        text = p.read_text()
    except OSError as e:
        return f"(daalu) read failed: {e}"
    print(f"\033[2m(read {rel}, {len(text)} chars)\033[0m")
    if len(text) > 16_000:
        text = text[:16_000] + "\n…(truncated)\n"
    return f"Contents of `{rel}`:\n\n```\n{text}\n```"


def _write(sess: _Session, rel: str, tail: str) -> str | None:
    p = _safe_path(sess, rel)
    if p is None:
        return f"(daalu) write declined: unsafe path: {rel}"
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
    print(_colour_diff(diff) or "(file is identical)")
    if before and diff == "":
        return f"(daalu) no-op: {rel} would be unchanged"
    if not _confirm(f"apply write to {rel}?"):
        return f"(daalu) user declined to write {rel}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new_content)
    print(f"\033[32m✓ wrote {rel} ({len(new_content)} chars)\033[0m")
    return f"(daalu) wrote `{rel}` ({len(new_content)} chars)"


def _run(sess: _Session, command: str) -> str | None:
    print(f"\n\033[33m── proposed run ──\033[0m\n  $ {command}")
    if not _confirm("run this command?"):
        return f"(daalu) user declined to run `{command}`"
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(sess.root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return f"(daalu) command timed out after 60s: `{command}`"
    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    out = out[-4000:]
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


def _colour_diff(diff: str) -> str:
    out = []
    for ln in diff.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            out.append(f"\033[32m{ln}\033[0m")
        elif ln.startswith("-") and not ln.startswith("---"):
            out.append(f"\033[31m{ln}\033[0m")
        else:
            out.append(ln)
    return "\n".join(out)


def _print_assistant(text: str) -> None:
    print()
    print(text)
    print()

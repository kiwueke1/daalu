# Daalu Coding Agent — VS Code extension

In-IDE coding agent for the Daalu **Coding Assistant** workspace.
(Extension id stays `daalu.daalu-assistant`; the user-facing name is
**Daalu Coding Agent**.)

* The chat lives in a webview with its own activity-bar icon. On
  startup it best-effort docks itself in the **secondary side bar**
  (the panel on the right) so the editor area stays for code; the user
  can close it, or run **Daalu Coding Agent: Move Chat to Secondary
  Side Bar** / drag the icon if their VS Code build lacks the move
  command. VS Code has no declarative API to default a custom view to
  the right — see `extension.ts`.
* Prompts go to the Daalu **inference-gateway** (or, today, directly to
  the local-GPU coder vLLM over the WireGuard tunnel — see
  `workspace_controller/k8s.py::_fqdn_gateway_url`), which routes per
  the tenant's tier (local / sovereign / daalu-hosted). The cloud tier
  is *never* used for `purpose='coding'` — see `core/llm.py`.

## Agentic file tools

The agent starts with **no** knowledge of the workspace — the model
only sees what's in the prompt. It gathers context itself via a bounded
**ReAct loop** (`chatProvider.runAgentLoop`): the model emits read-only
directives, the extension runs them, feeds the results back, and
re-prompts until the model answers.

| Directive | Action | Side effect |
|-----------|--------|-------------|
| `daalu:list <dir>` | list one level of a workspace dir (`.` = root) | none (auto-run) |
| `daalu:read <file>` | read a workspace file (byte-capped) | none (auto-run) |

Reads/lists are **auto-run** (read-only, sandboxed to the workspace
root) and surfaced in the chat as a "🔧 inspected workspace" card so
the basis for each answer is auditable. The deployed vLLM isn't
launched with OpenAI auto-tool-choice, so this is a text protocol
rather than native `tool_calls`.

Guardrails (all configurable):
* `daalu.maxToolIterations` (default 6) — max tool rounds per question.
* `daalu.maxReadBytes` (default 8000) — per-file read cap; the coder
  model's context window is small (8192 tokens on Qwen2.5-Coder), so
  large files come back truncated.
* History is trimmed to a char budget before each call so multi-round
  tool output can't overflow the context window.
* Path sandbox (`tools.assertSafePath`) rejects anything outside the
  workspace; per-round call cap (8) and read de-dup prevent loops.

**Edits stay user-approved:** the model proposes a write with a fenced
block whose first line is `// daalu:write <path>` → the UI renders
**Apply** (opens a diff first). Shell commands appear as `$ <cmd>`
lines with a **Run** button (a "Daalu" terminal). The agent never
mutates the workspace on its own.

You can also **📎 Attach** a file manually to seed context without
asking the agent to discover it.

## Building locally

```bash
cd extensions/daalu-assistant
npm install
npm run build           # tsc → dist/
npm run package         # vsce → daalu-assistant.vsix
```

The container build (see `deploy/docker/code-server-with-daalu/`)
runs the `package` step and `code-server --install-extension`s the
resulting `.vsix` so workspace pods start with the agent preinstalled.

## Configuration

Settings: `daalu.gatewayUrl`, `daalu.tokenFile`, `daalu.model`,
`daalu.maxTokens`, plus the agent knobs `daalu.toolsEnabled`,
`daalu.maxToolIterations`, `daalu.maxReadBytes`, `daalu.autoOpenRight`.
In the workspace pod the controller injects the gateway/model env (see
`workspace_controller/k8s.py::_container_env`); locally you can set
them in your VS Code settings.json.

## Wire shape

```
Browser (code-server with this ext)
  │
  │  POST /v1/chat/completions
  │  Authorization: Bearer <service-token JWT>
  ▼
inference-gateway (in daalu-hub)   [or direct WG route to the coder vLLM today]
  │  verify JWT, quota, rate-limit, meter
  ▼
daalu-vllm-pool   OR   workload-cluster vLLM via tunnel (SOVEREIGN)
```

The service-token JWT is minted by a sidecar in the workspace pod
(`token-minter`) and written to a shared emptyDir at
`/var/run/daalu/tokens/service.jwt`; the extension reads it before
every request.

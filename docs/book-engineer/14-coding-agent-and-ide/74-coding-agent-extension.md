# 74. The Daalu Coding Agent extension — internals

This chapter documents the **Daalu Assistant** VS Code extension — the agent
that runs inside each workspace pod — and the serving-side wiring that lets
it use **native OpenAI function-calling** against a self-hosted model.

It supersedes the extension description in
[35](35-coding-assistant.md), which describes the
earlier single-shot design (text-pragma parsing). The extension is now a
bounded tool-calling **ReAct loop**.

> **Why it changed.** The original loop was tuned for a 14B model in an 8 K
> window, where text pragmas were a pragmatic fit. The RTX 6000 Ada (48 GB)
> serves Qwen2.5-Coder-32B and the agentic **Qwen3-Coder-30B-A3B** at long
> context — capable enough that the *loop*, not the model, became the
> ceiling. Native tool-calling is the unlock.

## Layout

```
extensions/daalu-assistant/
  src/
    extension.ts        — activation + wiring
    chatProvider.ts     — the agent loop, tool specs, system prompt
    inferenceClient.ts  — OpenAI chat-completions client (tools + tool_calls)
    tools.ts            — tool implementations (read-only + human-approved)
  webview/
    chat.js / chat.css  — vanilla-JS chat UI (tool cards, Apply/Run)
  package.json          — contributes + settings
```

`extension.ts` is just wiring: it constructs the `InferenceClient`, `Tools`,
and `ChatViewProvider`, and registers the sidebar webview plus the
`daalu.openChat` / `daalu.reload` commands.

## The two classes of tool

The agent exposes six tools (`TOOL_SPECS` in `chatProvider.ts`), split by
whether they mutate the workspace:

| Tool | Class | Behaviour |
|---|---|---|
| `read_file` | read-only | executes immediately, result fed back |
| `list_dir` | read-only | one directory level, dirs first |
| `search_files` | read-only | bounded substring grep (`path:line: text`) |
| `repo_map` | read-only | file tree + top-level symbols (orientation) |
| `write_file` | **mutating** | surfaced as an **Apply** card; never auto-written |
| `run_command` | **mutating** | surfaced as a **Run** card; never auto-run |

`READONLY_TOOLS` (`chatProvider.ts`) is the set that runs inside the loop.
The mutating two are the **human-in-the-loop gate** — a deliberate product
stance, not a missing feature. The model *proposes*; the user *commits*.

## The agent loop

`runAgentLoop` (`chatProvider.ts`) is the core. Each iteration:

1. `client.complete(history, TOOL_SPECS)` — one chat-completions round with
   `tool_choice:"auto"`.
2. Push the assistant turn verbatim (content **and** any `tool_calls`) so
   the OpenAI protocol stays valid for the follow-up tool messages.
3. If there were **no** tool calls → the model answered in natural language;
   render it and stop.
4. Otherwise run each tool call via `handleToolCall`, and push a `role:"tool"`
   result for **every** call (including the mutating ones, with a "pending
   approval" note) — vLLM rejects a follow-up completion if any `tool_call_id`
   is left unanswered.
5. If any call was mutating → stop the auto-loop and tell the user to
   Apply/Run. This prevents the model from reasoning on top of a change the
   human hasn't approved.
6. Loop is bounded by `daalu.maxToolIterations` (default 8); on exhaustion a
   final tool-free completion is forced so the turn always ends with an
   answer.

`handleToolCall` (`chatProvider.ts`) parses the JSON arguments, posts a
compact activity card for read-only tools (`{type:'tool'}`) or a structured
proposal for mutating ones (`{type:'propose-write'|'propose-run'}`), and
returns the string fed back to the model. Invalid JSON or a tool error is
returned to the model as an `error: …` tool result rather than throwing — the
model can recover.

The system prompt (`SYSTEM_PROMPT` in `chatProvider.ts`) tells the model it
starts with **no** workspace knowledge and must use tools first, to pass the
full new file contents to `write_file`, and that it has no public-internet or
remote-push access.

## The inference client

`inferenceClient.ts` speaks OpenAI `/v1/chat/completions` to the
inference-gateway (or, today, directly to vLLM over the tunnel). Key points:

* `complete(messages, tools?)` adds `tools` + `tool_choice:"auto"` only when
  tools are passed; the budget-exhausted final turn calls it **without**
  tools to force prose.
* The richer message model (`ChatMessage`) carries `tool_calls` on assistant
  turns and `tool_call_id` + `name` on `role:"tool"` results;
  `CompletionResult` surfaces `toolCalls` and `finishReason`.
* Auth is the rotating service-token JWT read from
  `/var/run/daalu/tokens/service.jwt` (the token-minter sidecar, chapter 74),
  cached 30 s; a `401` busts the cache for a retry.
* **Streaming is intentionally not enabled yet** — interleaving SSE deltas
  with tool-call accumulation is its own piece of work (see fast-follows).

## The tools implementation

`tools.ts` is the single auditable surface for anything touching the FS or a
process. Every path is resolved through `resolveInsideWorkspace` /
`assertSafePath`, which reject `..` escapes — the model can emit anything, so
we defend at the call site, not in the prompt. Read tools are byte/entry
capped (`readForTool`, `listDir`, `searchFiles`).

`repoMap` is an **Aider-inspired, lightweight repo map**: a depth-bounded
file tree where each code file is annotated with its top-level symbols
(`def`/`class`/`func`/`export const`…) extracted by regex. It is *not*
tree-sitter + PageRank ranking (that's a fast-follow) — but it gives the
model orientation in an unfamiliar repo for one cheap call, which is the
biggest single quality lever now that context is large.

### Diff-at-propose-time (PR #89)

The review moment moved. Originally a proposed write surfaced as a bare code
block with an Apply button, and `applyWrite` opened an untitled-document
preview *after* the fact — you couldn't see what would change, or where,
before approving. Now, **the moment the model proposes a write**, the
extension opens a real side-by-side red/green diff (current ↔ proposed) via
`vscode.diff`. Both sides are served as **read-only virtual documents** under
a `daalu-diff:` `TextDocumentContentProvider`
(`extensions/daalu-assistant/src/tools.ts:51-60`; URIs shaped
`daalu-diff:/<side>/<seq>/<relpath>`, `tools.ts:217`), so there are no stray
editable "untitled" tabs and nothing touches disk until Apply. The diff URI
keeps the file's basename so syntax highlighting is correct, and focus stays
on the chat so you can review and click Apply.

`applyWrite` therefore now just **commits the bytes** via `workspace.fs` — the
review happened at propose time; the old after-the-fact preview is removed.
`runInTerminal` sends to a persistent "Daalu" terminal. Both are only ever
reached *after* the user clicks Apply/Run in the webview.

## The webview

`webview/chat.js` renders three new message kinds from the host:
`tool` (a compact one-line activity card with a per-tool icon),
`propose-write` (an **Apply** card whose `<pre>` holds the full proposed
content), and `propose-run` (a **Run** card). Clicking Apply/Run posts back
to the host, which calls `Tools.applyWrite` / `runInTerminal`. The CSP stays
strict (nonce'd script, no inline).

## Settings

`package.json` adds: `daalu.toolsEnabled` (default true — turn the whole
loop off for plain chat), `daalu.maxToolIterations` (8),
`daalu.maxReadBytes` (16000), and `daalu.maxSearchResults` (60), alongside
the existing `gatewayUrl` / `tokenFile` / `model` / `maxTokens`.

## Serving side: making tool-calling actually work

Native tool-calling requires vLLM to parse the model's tool syntax. That is
configured from the **model catalog** so the five-names contract
([73](73-coding-model-catalog-and-serving.md))
extends to the parser:

* `core/model_catalog.py` — each `CodingModel` carries a `tool_call_parser`:
  `hermes` for the Qwen2.5-Coder family, `qwen3_coder` for Qwen3-Coder.
* `gpu_controller/manifests.py` — `_vllm_args` emits
  `--enable-auto-tool-choice --tool-call-parser=<parser>` when the field is
  set; `coder_params_from_catalog` threads it from the catalog and bumps the
  host-RAM envelope for the 48 GB checkpoints.
* `daalu render-coding-model <id>` (`cli.py`) regenerates the manifests.
  The three live bundles are `deploy/k8s/gpu/10-vllm-qwen-coder-14b.yaml`
  (ada-16, hermes), `11-vllm-qwen-coder-32b.yaml` (ada-48, hermes), and
  `12-vllm-qwen3-coder-30b-a3b.yaml` (ada-48, `qwen3_coder`, FP8).

Non-tool chat requests are unaffected by the flags, so enabling tool-calling
is backward-compatible.

### 48 GB sizing & the context/concurrency trade-off

On the 48 GB RTX 6000 Ada the constraint shifts from "model too weak" to
"KV cache is finite". Rules of thumb baked into the catalog:

* **Qwen2.5-Coder-32B (AWQ-INT4)** — weights ~18 GB, leaving ~24 GB for KV
  (~100 K tokens total). Launched at its native 32768 context — enough for
  32 K across a few concurrent workspaces. 131072 via YaRN is possible but
  trades concurrency.
* **Qwen3-Coder-30B-A3B (FP8, MoE)** — ~3 B active params → fast decode and
  the agent-native choice. Native context is huge; we launch at 65536 as a
  practical 48 GB default (KV-bounded at low concurrency).

The GPU is still allocated exclusively to one pod, so picking a coder for the
card is an operator choice (catalog + `coding_models_enabled`), covered in
[36](36-coding-assistant-operator.md) and
[73](73-coding-model-catalog-and-serving.md).

## Fast-follows (intentionally out of scope today)

* **SSE streaming** of the final answer (token-by-token), including
  accumulating `tool_calls` deltas.
* **Full repo-map** with tree-sitter parsing and PageRank symbol ranking.
* **Evaluating Cline** as a drop-in loop: viable now that the model is
  capable, but it needs adapting to Open VSX packaging, the locked-down
  multi-tenant pod, and the Apply/Run approval stance — likely a
  pattern-harvest rather than a wholesale adoption.

## See also

* [75. The workspace platform internals](75-workspace-platform-internals.md)
* [73. Coding-model catalog & multi-model serving](73-coding-model-catalog-and-serving.md)
* [34. The inference-gateway service](../06-gpu-and-llm-serving/34-inference-gateway.md)

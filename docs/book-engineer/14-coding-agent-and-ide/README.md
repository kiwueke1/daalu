# Part XIV — The Coding Agent & Workspace IDE

Everything behind the **Workspace** feature: the per-user browser VS Code
(code-server) the platform provisions, the controller that manages those
pods, the reverse proxy that fronts them, and the **Daalu Coding Agent**
extension that runs inside — wired to the customer's own GPU so code
prompts never leave their perimeter.

These chapters were originally filed under *Part VI — GPU and LLM
Serving* (35, 36, 73). They graduated to their own part as the feature
grew its own controller, image, extension, and runbook. Chapter numbers
are unchanged so existing cross-references still resolve (see the note on
chapter numbers in the [book README](../README.md)).

## Chapters

| # | Chapter | What it covers |
|---|---------|----------------|
| 35 | [Coding-assistant architecture](35-coding-assistant.md) | How browser → daalu-api → workspace-controller → code-server pod → inference path fit together; session lifecycle; the `/ide` reverse proxy; the token-minter sidecar. |
| 36 | [Coding-assistant — operator runbook](36-coding-assistant-operator.md) | Step-by-step bring-up and per-tenant onboarding; the 502 incident and fix; where to look when a step fails. |
| 73 | [Coding-model catalog & multi-model serving](73-coding-model-catalog-and-serving.md) | `core/model_catalog.py` as the single source of truth; how a model pick flows to the right vLLM; adding a model. |
| 74 | [The Daalu Coding Agent extension — internals](74-coding-agent-extension.md) | The VS Code extension itself: the **native function-calling** agent loop, the read-only tools (read/list/search/repo-map) and human-approved write/run, the Aider-style repo-map, and the serving-side tool-calling (vLLM parser, 48 GB models). |
| 75 | [The workspace platform internals](75-workspace-platform-internals.md) | Code-level walkthrough: the workspace-controller reconcile loop + lifecycle state machine, the K8s resource builders, the `/ide` reverse proxy + WebSocket bridge, the token-minter, and the network posture. |
| 76 | [Private-repo authentication](76-private-repo-authentication.md) | Seeding a workspace from a **private** GitHub/GitLab/self-hosted repo with a PAT: the write-only token field, Fernet encrypt-at-rest, the per-workspace git Secret + credential-helper wiring (token never in the manifest or `.git/config`), and the egress/SSH/self-hosted caveats. |
| 77 | [The Daalu CLI — bringing the agent to the laptop](77-daalu-cli.md) | The standalone `daalu` CLI: the `daalu login` device flow (+ express paste-command) that mints a PAT, the public coding gateway that re-establishes the `purpose=coding` guarantee off-cluster, the dependency-light CLI package + agent loop, and distribution via `get.daalu.io` / GitHub Releases / Homebrew / pipx. |

## The one-paragraph mental model

The platform runs a custom **code-server** image
(`ghcr.io/kiwueke1/code-server-with-daalu`) per user, in the isolated
`daalu-workspaces` namespace, created and lifecycled by the
**workspace-controller** (which lives in `daalu-automation` for its DB +
signing-key env). The browser reaches the pod through daalu-api's `/ide`
reverse proxy. Inside the IDE, the **Daalu Coding Agent** extension is the
only AI surface (the upstream Copilot Chat is removed from the image). It
reads workspace files via a bounded agent loop and sends prompts —
carrying a short-lived service-token JWT minted by a sidecar — to a coder
vLLM (`qwen-coder` / `llm-coder`) running on the customer's GPU, reached
over the WireGuard tunnel. The workspace can be seeded from a public *or*
**private** Git repo: a personal-access-token entered at create time is
encrypted at rest and mounted as a git credential helper for the clone
(chapter 76). The same agent is also available **off-cluster**: a
standalone `daalu` CLI the user installs on their laptop, authenticates via
a browser device flow, and runs against any local codebase through a public
coding gateway that re-mints the `purpose='coding'` service-token on their
behalf (chapter 77).

## Related reading

- Customer-facing walkthrough: `docs/user-guide/04-feature-walkthroughs/26-coding-assistant.md`
- Customer-facing CLI how-to: `docs/user-guide/04-feature-walkthroughs/47-daalu-cli-terminal.md`
- The CLI source: `cli/`
- The inference backend: [Part VI, ch. 34 — inference-gateway](../06-gpu-and-llm-serving/34-inference-gateway.md)
- Cluster federation / the tunnel these prompts ride: [Part V](../05-cluster-federation/)
- The extension source: `extensions/daalu-assistant/`

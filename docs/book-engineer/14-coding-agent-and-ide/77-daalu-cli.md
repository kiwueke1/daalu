# 77. The Daalu CLI — bringing the agent to the laptop

Chapters 35–76 describe the coding agent as it lives *inside the cluster*: a
per-user code-server pod, the Daalu Assistant extension, and the in-pod
`daalu chat` REPL, all reaching inference with a short-lived service-token JWT
minted by a sidecar. This chapter is about the second surface — a standalone
`daalu` CLI the user installs on their **own dev machine** (like Claude Code or
the GitHub CLI), logs into from the terminal, and runs against *any* local
codebase. The browser IDE stays; the CLI runs where the user's code already is.

> Customer-facing how-to:
> `docs/user-guide/04-feature-walkthroughs/47-daalu-cli-terminal.md`.

## Why a laptop can't reuse the in-pod path

The in-pod agent's whole trust model is local to the cluster:

- It reads a `service.jwt` from `/var/run/daalu/tokens/`, refreshed every 30 s by
  the token-minter sidecar, signed with `service_token_secret_key` — a key that
  **never leaves the cluster**.
- It posts to `DAALU_GATEWAY_URL`, an in-cluster Service DNS name.

A laptop has neither the signing key nor in-cluster DNS, and we are *not* going
to ship a service-token key to laptops. So the CLI needs two new things: a way
to **authenticate as the user** without a service token, and a **public,
authenticated front door** to inference. Those are the two backend pieces; the
third is the CLI itself, and the fourth is how we distribute it.

## The pieces and where they live

| Piece | Path |
|-------|------|
| Device-auth table | `models/cli_device.py`, migration `0028_cli_device_authorizations` |
| Device-code helpers | `core/cli_auth.py` |
| Device-auth router | `api/routers/cli_auth.py` (`/api/v1/cli/*`) |
| Public coding gateway | `api/routers/coding.py` (`/api/v1/coding/*`), `core/coding_gateway.py` |
| The CLI package | `cli/` (distribution name `daalu-cli`, console script `daalu`) |
| Distribution | `cli/packaging/`, `.github/workflows/cli-release.yml` |
| Browser approval page | `frontend/app/cli/activate/page.tsx` |
| Workspace-page card | `frontend/app/workspace/page.tsx` (`TerminalCard`) |

## 1. Authentication — a device flow that mints a PAT

We did **not** invent a new credential type. The CLI ends up holding a normal
`dpat_` personal-access-token — the same token `/settings → API tokens` mints,
the same one `api/deps.current_user` already accepts. Everything new is just the
*delivery*: how a terminal with no browser session ends up holding one.

`cli_device_authorizations` is a short-lived (10 min), single-use row:

```
device_code_hash   sha256 of the secret the CLI holds (never stored plain)
user_code          short XXXX-XXXX code shown in the browser
status             pending → approved → redeemed   (also denied / expired)
tenant_id/user_id  null until approval binds them
pat_id             the PAT minted at redeem time
client_name        "daalu-cli @ hostname" → becomes the PAT name
```

Two flows, one redeem path (so the CLI has exactly one code path):

**Device flow (`daalu login`).** RFC-8628 in shape:

```
CLI ──POST /cli/device/code {client_name}──▶  create pending row
    ◀── {device_code, user_code, verification_uri_complete, interval} ──
CLI opens the browser → /cli/activate?code=WDJB-MJHT
                          user clicks Authorize → POST /cli/device/approve
                          row.status = approved, tenant_id/user_id stamped
CLI ──poll POST /cli/device/token {device_code}──▶  "authorization_pending"
    … then once approved ──▶ mint PAT, row→redeemed, return {access_token}
```

**Express flow (paste-a-command).** The workspace page calls
`POST /cli/login-command`, which creates a row **already approved** for the
logged-in user and returns a one-time `daalu login --host … --code dvc_live_…`
command. The user pastes it; the CLI skips the browser and redeems directly.
Same table, same `/cli/device/token` redeem — the row just starts at `approved`.

Two design points worth calling out:

- **`/cli/device/code` and `/cli/device/token` are public** (added to
  `settings.auth_public_paths`) — the CLI has no session yet, and the
  `device_code` *is* the secret. Every other `/cli/*` endpoint is
  `current_user`-gated (the browser hits those). See
  `AuthGateMiddleware` in `api/main.py`.
- **Redeem is single-use, transactionally.** `/cli/device/token` mints the PAT
  with `db.flush()` (to populate `pat.id`) and flips the row to `redeemed` in the
  *same commit*. A replayed `device_code` finds `status=redeemed` and is denied —
  a leaked code can never yield a second token. The endpoint always returns 200
  with a `status` discriminator so the CLI's poll loop is a JSON branch, not
  HTTP-status guessing.

## 2. The public coding gateway

`api/routers/coding.py` is the laptop's inference front door — and the place the
sovereignty guarantee is re-established off-cluster.

`POST /api/v1/coding/v1/chat/completions`:

1. `current_user` resolves the caller from their PAT.
2. `_eligible_tenant` (reused from the workspace router) — same three-way check:
   sovereign GPU, daalu-hosted enrolment, or the operator override flag.
3. The requested `model` is validated against `model_catalog` served names
   (defaulting to `coding_model_default`).
4. `core/coding_gateway.resolve_coding_upstream` picks the upstream — **three**
   branches since PR #92 (`core/coding_gateway.py:56-74`):
   - `coding_local_base_url` (env `CODING_LOCAL_BASE_URL`) → a raw vLLM serving
     the *coding* models directly, no auth header. **Top priority**; e.g.
     `http://10.200.0.2:8000/v1`, a qwen-coder stack on a GPU cluster over
     the WG tunnel.
   - else `daalu_hosted_gateway_url` → the in-cluster inference-gateway,
     **with** a freshly minted `purpose=coding` service-token (the gateway does
     quota, metering, pool-resolution, revenue-share);
   - else `llm_local_base_url` → a raw vLLM, no auth header;
   - else 503.
5. Proxy the OpenAI-compatible body, return the JSON verbatim.

> **Why the dedicated first branch exists (PR #92).** When
> `DAALU_HOSTED_GATEWAY_URL` is empty, so the resolver used to fall through to
> `LLM_LOCAL_BASE_URL` — which over the tunnel is the llama **classifier** on
> `:8001` (serves only `meta/llama-3.1-8b-instruct`). Every laptop prompt 404'd
> with "The model `qwen/qwen3-coder-30b-a3b-instruct` does not exist." The
> browser Workspace path was unaffected because the workspace-controller bakes
> the coder URL straight into each code-server pod. `coding_local_base_url` is
> deliberately a *separate* setting from `llm_local_base_url` (classifier) and
> `daalu_hosted_gateway_url` (hosted tier + workspace availability) so wiring
> the coder upstream can't disturb daalu-api's other LLM routing. Port
> convention: **`:8000` = coder, `:8001` = classifier** (both socat-proxied on
> the edge — see [30](../06-gpu-and-llm-serving/30-vllm-deployment.md)).

> **Configured ≠ serving (PR #108).** The coder is a stack nothing in the UI
> deploys, so `/workspace/eligibility` and `/workspace/models` additionally
> probe the resolved upstream's `/v1/models`
> (`core/coding_gateway.py:coding_upstream_healthy`, 30 s TTL cache — failures
> cached too, since an undeployed coder is the steady state being detected).
> No coder serving → **409** with a message naming the actual problem, and
> every model in the picker reports `available: false`. The
> `feature_flags.workspaces_enabled` operator override bypasses the probe. The
> probe hits the models *listing*, not chat/completions, so rendering a form
> never bills an inference.

The trick mirrors `/ide/sessions`' `_controller_call`: **daalu-api mints the
service-token on the user's behalf** after authenticating them with their PAT.
That `purpose=coding` token is what keeps the laptop honest — the inference
router refuses every external tier for that purpose, so a prompt that originated
on someone's MacBook still routes only to sovereign / Daalu-hosted inference.

`GET /api/v1/coding/config` is the CLI's self-configuration after login: the
model list (with `available` mirroring `coding_models_enabled`), the default
served name, and the chat-completions path. The CLI caches the default model in
its credentials file so `daalu` starts instantly.

> **v1 is non-streaming** — it mirrors the single-shot POST the agent loop
> already uses (`workspace_controller/chat_repl.py`). SSE streaming is the
> obvious fast-follow; it's a change to `coding.py` + the CLI's `client.py`, not
> the auth or routing.

## 3. The CLI package

`cli/` is a **standalone** package — distribution name `daalu-cli`, console
script `daalu`. It deliberately does **not** import `daalu_automation`: a laptop
install pulls in nothing but `httpx`, which keeps the PyInstaller binary small
and `pipx install` fast. It talks only to the public HTTP API.

```
cli/daalu_cli/
  __main__.py   argparse dispatch (login/logout/status/models/chat; bare → chat)
  config.py     ~/.daalu/credentials.json (mode 0600); DAALU_HOST/DAALU_TOKEN override
  auth.py       device flow + --code redeem; login/logout/status
  client.py     httpx wrapper (whoami / coding_config / chat_completion)
  agent.py      the agentic REPL
```

The agent loop in `agent.py` is the **same read/write/run prompt convention** as
the in-pod `chat_repl.py` — `daalu:read`, `daalu:write` (diff + y/N),
`daalu:run` (y/N) — lifted into the standalone package and adapted on two axes:
the workspace root is the **current working directory** (any repo), and
inference goes through `DaaluClient` (the user's PAT → public coding gateway)
instead of the in-pod gateway + service-token. We keep the prompt-driven text
pragmas rather than OpenAI function-calling so the loop works against any
chat-completion model the gateway serves. Path containment (`_safe_path`) refuses
any write that escapes the project root.

### The tool-call budget flushes a final answer (PR #93, shipped as 0.1.1)

Both `agent.py` and the in-pod `chat_repl.py` bound the per-turn tool-call loop
with a shared-by-convention `_MAX_TOOL_CALLS` constant (`cli/daalu_cli/agent.py:62`,
`workspace_controller/chat_repl.py:89` — keep them in sync by hand; they are
separate packages on purpose). Originally the cap was 8 and hitting it
**silently returned to the prompt**: the loop executed the last tool, appended
its result, the `for` ended, and control fell back to the REPL with no final
completion — to the user the agent "dropped mid-thought" (e.g. "explain this
codebase" burned every call on file reads and never answered). Server-side
forensics confirmed this was purely client behaviour: coder vLLM had 0
restarts, no OOM, no context-length rejection, and daalu-api returned 200 for
the completions — the client just stopped asking.

The fix: on budget exhaustion, append a "stop using tools and answer now" nudge
and run **one final tool-less completion** so the model summarizes what it
gathered; if even that returns nothing, print an explicit limit notice instead
of a blank prompt. The cap was also raised **8 → 20** (8 was far too low for
legitimate multi-read tasks). Released as **cli-v0.1.1** (PR #94) — a
linux-x64 binary on `daalu-io/cli`.

Credentials precedence: `DAALU_HOST` / `DAALU_TOKEN` env vars (for CI) override
`~/.daalu/credentials.json`. The file is written 0600.

## 4. Distribution — get.daalu.io + GitHub Releases

Three install channels, one artifact source (GitHub Releases):

1. **`curl -fsSL https://get.daalu.io/install.sh | sh`** — the headline. The
   installer (`cli/packaging/install.sh`) detects OS/arch, pulls the matching
   `daalu-<os>-<arch>.tar.gz` from the latest Release, and drops the binary in
   `~/.local/bin` (or `/usr/local/bin`). `get.daalu.io` is a tiny static host
   serving only `install.sh` + `install.ps1` — never the binaries themselves.
2. **Homebrew** — `brew install daalu-io/tap/daalu`, formula in the tap repo
   (`cli/packaging/homebrew/daalu.rb`, tokens filled by CI).
3. **pipx / pip** — `pipx install daalu-cli`, for anyone with Python 3.9+.

The binaries are built by `.github/workflows/cli-release.yml` on a `cli-v*` tag:
a 5-target matrix (linux/macos/windows × x64/arm64) runs
`pyinstaller cli/packaging/daalu.spec`, tars/zips the result with a sha256, and
`softprops/action-gh-release` attaches everything. A gated follow-up job pushes
to PyPI and pokes the Homebrew tap. PyInstaller one-file means the laptop needs
**no Python** — matching the "just works" bar of `claude`. (Validated locally: a
~14 MB single-file `daalu`.)

## How it all fits

```
  laptop                         daalu-api                       in-cluster
  ──────                         ─────────                       ──────────
  daalu login ─▶ /cli/device/code ─▶ cli_device_authorizations
       │ browser ─▶ /cli/activate ─▶ approve (cookie)  ─▶ status=approved
       │ poll /cli/device/token ─────────────────────── ▶ mint PAT (dpat_)
       ▼
  ~/.daalu/credentials.json  {host, token, model}

  daalu (in ~/src/repo) ─▶ POST /api/v1/coding/v1/chat/completions
       Authorization: Bearer dpat_   │ current_user → eligibility
                                      │ mint service-token purpose=coding
                                      ▼
                       inference-gateway / local vLLM (sovereign)
  agent loop: read / write(diff,y/N) / run(y/N)  on the LOCAL tree
```

## Tests

- `tests/test_cli_device_auth.py` — both flows end-to-end through the ASGI app
  (device grant: code → pending → approve → redeem → PAT; express: login-command
  → redeem), single-use enforcement, and the coding gateway (eligibility 403,
  503-no-upstream, a respx-mocked vLLM proxy, default-model fill-in).
- `cli/tests/test_cli.py` — credentials round-trip + 0600, env override, tool
  parsing + path-escape refusal, and `login --code` against a mocked API.

## Operating notes

- **`public_base_url` must be set** for the device flow — it builds the
  `verification_uri` the CLI opens and the `--host` the express command embeds.
  Empty → the CLI falls back to its `DEFAULT_HOST` and the browser link is
  path-relative (fine in dev, wrong in prod). Set it to `https://ops.<domain>`.
- A laptop PAT is a normal PAT: it appears in `/settings → API tokens` named
  after the machine, and revoking it there kills the CLI's access immediately.
- Eligibility is identical to the browser workspace — if the **Workspace** tab is
  available to a tenant, so is the CLI.

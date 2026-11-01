# 99. NV-CM's MCP server and its workflow allow-list

NV-CM ships its own **Model Context Protocol server**: a belt of named tools an
MCP client (Claude Code, an IDE agent) can call to read the fabric's source of
truth, configs and workflow history, and to start a short, explicitly
allow-listed set of diagnostic workflows. This chapter covers what it exposes,
how it authenticates, how the allow-list works, and how (and how far) a
Daalu-deployed environment makes it reachable.

> **Not `daalu mcp`.** This chapter is about **NV-CM's** MCP server — an
> upstream NVIDIA component that runs *inside a tenant's NV-CM environment*.
> Daalu also has **its own, separate MCP server** (`daalu mcp`,
> `src/daalu_automation/core/aiops/mcp.py`), which exposes Daalu's tools and
> routes writes through Daalu's autonomy policy. That one is covered in
> [chapter 90 — MCP server and open specs](../15-ai-ops-maturity/90-mcp-server-and-open-specs.md).
> Different code, different endpoint, different auth, different tools. Don't
> conflate them on screen or in docs.

> **Upstream vs Daalu.** Every tool, the allow-list mechanism, redaction and
> the auth middleware described here are **upstream NV-CM, outside Daalu's
> code**. Daalu's part is narrower: whether the server gets deployed (a
> side-effect of component choice), the workflow RBAC it ends up running
> under, and the fact that the hub tool proxy does *not* front it. Upstream
> references (`nv-config-manager/<path>:<line>`) are pinned to tag **`1.3.0`
> (`7455da6`)**, the commit Daalu vendors; where upstream HEAD (`2dad101`,
> `1.3.1-rc.2`) differs, the text says so.

| Piece | Owner | Path |
|---|---|---|
| Server bootstrap (`/mcp`, streamable HTTP) | upstream | `nv-config-manager/src/nv_config_manager/mcp/main.py:56-91` |
| Tool registration (13 read tools + starters) | upstream | `nv-config-manager/src/nv_config_manager/mcp/tools.py:51-338` |
| Workflow allow-list discovery | upstream | `nv-config-manager/src/nv_config_manager/mcp/workflows.py:64-91` |
| Opt-in flag (default `False`) | upstream | `nv-config-manager/src/nv_config_manager/temporal/common/mixins/metadata.py:31,82-94` |
| Read-only guard, bounding, redaction | upstream | `nv-config-manager/src/nv_config_manager/mcp/clients.py:42-116` |
| Caller-identity auth | upstream | `nv-config-manager/src/nv_config_manager/mcp/auth.py:89-162` |
| Chart `mcp:` block (`enabled: false`) | chart | `deploy/charts/nv-config-manager-1.3.0/values.yaml:1883-1952` |
| Deploy condition (needs `dhcp`) | vendored installer | `src/nv_config_manager_installer/helm_values.py:880-885` |
| Component defaults (`dhcp` off) | Daalu | `src/daalu_automation/config_manager_controller/values.py:100-108` |
| Open workflow RBAC | Daalu | `src/daalu_automation/config_manager_controller/values.py:23-46` |
| Hub tool proxy (no `mcp` route) | Daalu | `src/daalu_automation/api/tool_proxy.py:97-103,175-196` |

---

## 99.1 The shape of the server

`create_mcp_server` builds a FastMCP server named `nvidia-config-manager-mcp`,
stateless, JSON responses, **streamable HTTP at `/mcp`**
(`mcp/main.py:63-77`), with unauthenticated `/healthcheck` and `/metrics`
routes and optional OAuth discovery metadata (`main.py:79-88`). Its
self-description to clients is the design in one sentence: "Read-only NVIDIA
Config Manager operator tools plus explicitly enabled safe diagnostic
workflow starters" (`main.py:65-71`).

In the chart it is one Deployment + ClusterIP Service on port **9000**, named
by the chart's component helper (`templates/mcp.yaml:65,98-108`; with Daalu's
`fullnameOverride` that is `cm-<tenant hex12>-mcp`), and two gateway hosts:
`mcp.<baseHostname>` (OIDC+JWT, browser) and `svc-mcp.<baseHostname>`
(JWT-only) (`values.yaml:1888-1893`).

## 99.2 When a Daalu environment has one

Three layers decide it:

1. The chart default is **`mcp.enabled: false`**, kept off so the chart works
   with pre-1.3.0 images that lack the entry point (`values.yaml:1884-1887`).
2. The vendored installer (the Deployer path, on in prod — chapter 23 §23.3)
   sets `mcp.enabled` to **`nautobot and temporal and config_store and dhcp`**
   (`src/nv_config_manager_installer/helm_values.py:880-885`). The DHCP
   dependency is there because one read tool, `get_dhcp_config`, calls the
   DHCP API (`mcp/tools.py:215-220`, `clients.py:285-290`).
3. Daalu's default "Lean" profile has **`dhcp: False`**
   (`config_manager_controller/values.py:100-108`), and Daalu's own values
   renderer (the legacy `HelmRunner` path) never sets `mcp` at all.

So **a default Daalu environment has no MCP server.** Creating the environment
with the DHCP component on (which also maps to the Deployer's `dhcp` service,
`deployer_config.py:51-58`) is what turns it on. If the tool list is empty or
short, check `mcp.enabled` in the release values first.

## 99.3 The tool belt

### Thirteen read tools

All registered in `register_tools` (`mcp/tools.py:51-303`):

| Job | Tools |
|---|---|
| **Find it** | `search_devices` (hostname / serial / MAC / ID / free text, `:75-131`), `get_device_id` (exactly one match or an ambiguity list, `:133-155`) |
| **Model it** | `list_nautobot_types`, `get_nautobot_type` (GraphQL schema introspection, `:162-213`), `query_nautobot` (arbitrary read-only GraphQL, `:157-160`) |
| **Read it** | `list_device_configs`, `get_device_config`, `get_config_versions`, `get_config_diff` (Config Store, `:222-272`), `get_dhcp_config` (`:215-220`) |
| **Watch it** | `list_workflows`, `get_workflow_detail` (Workflow API, `:274-303`) |
| Meta | `list_related_mcp_servers` (points at NVIDIA's public docs MCP server, `:54-73`) |

`query_nautobot` plus the two introspection tools are what let a model
discover the schema and write its own GraphQL, which is the "model it" beat.

### Workflow starters: the allow-list

After the read tools, `register_tools` loops over `discover_mcp_workflows()`
and registers one tool per result (`tools.py:305-306`). Discovery
(`mcp/workflows.py:64-91`) walks every registered NGC and hello-world
workflow and keeps a workflow only if **all** of these hold:

- it uses `WorkflowMetadataMixin`;
- its metadata is complete — name, description, input class, API endpoint
  (`metadata.py:86-94`);
- **`workflow_mcp_enabled` is `True`**, a class attribute that defaults to
  `False` (`metadata.py:31`, getter `:82-84`).

Each starter is named `run_<last path segment>` (`workflows.py:113-115`),
takes the workflow's own Pydantic input schema, fills `trigger="API"` and
nullable fields with `None` (`workflows.py:94-110`), starts the workflow
through the Workflow API with the caller's token, and returns the workflow
ID and a `workflow_ui_href` link to the run (`tools.py:309-338`).

**This is an allow-list, not a deny-list.** A new workflow is invisible to
agents unless its author adds the flag. The rejected alternative — exposing
everything with a deny-list of dangerous workflows — fails open: the next
deploy-shaped workflow someone writes would be on the belt by default.

At **1.3.0** eight workflows carry the flag:

| Tool | Workflow | Defined at |
|---|---|---|
| `run_backup` | Configuration Backup | `temporal/ngc/workflows/backup.py:101` |
| `run_site_cable_validation` | Site Cable Validation | `cable_validation.py:185` |
| `run_device_cable_validation` | Device Cable Validation | `cable_validation.py:484` |
| `run_port_lldp_info` | Port LLDP Info | `lldp.py:73` |
| `run_connected_host_metadata` | Connected Host Metadata | `connected_host.py:117` |
| `run_cumulus_hardware_validation` | Cumulus Hardware Validation | `cumulus_hardware_validation.py:225` |
| `run_infiniband_cable_validation` | InfiniBand Cable Validation | `infiniband_cable_validation.py:95` |
| `run_infiniband_get_unhealthy_ports` | InfiniBand Get Unhealthy Ports | `infiniband_get_unhealthy_ports.py:66` |

That's **13 + 8 = 21 tools at the 1.3.0 tag**. Upstream HEAD adds a ninth,
`run_site_backup` (Site Configuration Backup, `site_backup.py:120-130` at
HEAD), for **22**, and attaches a site-scope guidance prompt to any starter
whose input has the site-level filter fields — it tells the model to resolve a
named device first, report how many devices the filter matches, and get
explicit confirmation before starting a site-wide run; `status` defaults to
`Active`/`Provisioned` (HEAD `mcp/workflows.py:33-34`, `:154-158`). Which
count you see depends on the images: Daalu's
`deploy/scripts/config-manager/build-push-images.sh` builds from whatever
`SRC_REPO` has checked out and tags it `1.3.0` without checking out that tag
(`build-push-images.sh:34-35,52-57`). Recount on the day.

### What's deliberately not on the belt

1.3.0 registers **29** NGC workflows (`temporal/ngc/workflows/__init__.py:72-102`;
31 at HEAD). The ones that change devices are not flagged, and they carry
human-approval stages in their own definitions:

| Workflow | Approval stage | Where |
|---|---|---|
| Configuration Deploy | `perform_configuration_diff` | `deploy.py:121-123` |
| Switch OS Upgrade | `approve_upgrade` | `os_upgrade.py:97-99` |
| Device Password Rotation | `perform_configuration_diff` | `device_password_rotation.py:103-105` |
| Multi-device deploy | `review_shared_diff` | `multi_deploy.py:153-155` |
| InfiniBand MLNX-OS upgrade | `approve_upgrade` | `infiniband_mlnx_os_upgrade.py:197-199` |
| BMC factory reset | `approve_factory_reset` | `bmc.py:194-196` |

None of the eight MCP-enabled workflows defines an approval stage. Daalu's
own deploy path approves `perform_configuration_diff` programmatically after a
human approved the change in Daalu (`core/configmgr/client.py:31-34`,
`core/device/config_manager.py:192-208`; chapter 21 §21.3); MCP has no tool
that reaches that stage.

## 99.4 What "read-only" and "safe" mean, precisely

- **GraphQL is query-only.** `reject_non_readonly_graphql` rejects any text
  containing `mutation` or `subscription` with *"Only read-only GraphQL query
  operations are allowed."*, and anything not starting with `query` or `{`
  (`mcp/clients.py:95-101`), before the call is made (`clients.py:104-116`).
  It is a keyword check on the query text, enforced server-side regardless of
  what the prompt says.
- **REST is GET-only.** `search_devices` uses `nautobot_rest_get`
  (`clients.py:119-130`); no tool issues a Nautobot write.
- **Responses are bounded.** Limits are clamped to 100 (`clients.py:46,57`);
  any response over `maxResponseBytes` (chart default 100 000,
  `values.yaml:1945`) comes back as a truncated preview with
  `truncated: true` (`clients.py:81-92`).
- **Secrets are masked by pattern.** Every response passes through
  `redact_data`: dict keys matching
  `password|passwd|secret|token|api[_-]?key|private[_-]?key` have their values
  replaced with `<redacted>`, and text lines like `… password … : value` /
  `= value` are cut after the separator (`clients.py:42-45,62-78`). It's a
  regex, not a DLP guarantee, and it exists **only** in the MCP layer: the
  same file in the Config browser is unmasked (chapter 97 §97.4).
- **"Safe" workflows still act.** Every starter logs into devices, and
  `run_backup` writes a new backup version to the Config Store (chapter 97).
  "Safe" means no config is pushed to a device, not "no side effects".

## 99.5 Identity: the server acts as the caller

- `ServiceAuthMiddleware` requires an authenticated identity on every path
  except health, metrics, OAuth metadata and `OPTIONS`
  (`mcp/auth.py:107-141`).
- `RequestAuthMiddleware` captures the inbound `Authorization` header per
  request (`auth.py:89-104`). Calls to Config Store, Workflow API and DHCP
  forward **that bearer** and raise if there isn't one; the code comment is
  explicit that it must not fall back to a service identity
  (`auth.py:149-162`; chart `mcp.client`, `values.yaml:1940-1945`).
- Nautobot calls use `nautobot.authMode` (`values.yaml:1946-1952`): `auto`
  forwards the user's JWT to a bundled Nautobot and uses a read-only token for
  an external one (`clients.py:320-340`).

Consequences on a Daalu environment:

- Workflow history and starters are filtered by NV-CM's workflow RBAC for the
  caller's roles. Daalu installs the chart's **open** RBAC — every workflow
  readable and executable by `all` — because the hub already gates who
  reaches the environment (`config_manager_controller/values.py:23-34`,
  `deploy/charts/nv-config-manager-1.3.0/values-rbac-open.yaml`). On a Daalu
  environment, therefore, **the allow-list is the effective restriction on
  what an agent can start**, not RBAC.
- Daalu's autonomy policy (chapter 82) does not see these calls: no Daalu
  code sits between an MCP client and NV-CM's server.

## 99.6 Getting a client connected to a Daalu environment

**The hub does not front NV-CM's MCP server.** The tool proxy recognises only
`config-store`, `nautobot`, `render`, `workflow` and `temporal` host prefixes
plus the bare config-browser host (`api/tool_proxy.py:97-103,190-196`), and
it only accepts single-label hosts under the cmtools zone
(`tool_proxy.py:188-189`). The chart's MCP hosts are the dotted
`mcp.<base>` / `svc-mcp.<base>`, and Daalu's overlay gives flat hostnames
only to nautobot, render, config-store, temporal, ZTP and DHCP
(`deployer_runner.py:140-158`). Even a request that did reach the proxy would
have its `Authorization` header dropped and replaced with a hub-minted one
(`tool_proxy.py:116-127,350-380`) — the right behaviour for a browser, the
wrong one for an MCP client presenting its own token.

What works from the code as it stands:

- **Direct to the Service** (port-forward or in-cluster), with a bearer the
  NV-CM environment accepts — a Keycloak token whose audience matches NV-CM's
  `oidc.audiences` (`keycloak_token_audience`, default `nv-config-manager`,
  chapter 23 §23.5):

  ```bash
  kubectl -n <env-namespace> port-forward svc/<release>-mcp 9000:9000
  claude mcp add --transport http nv-config-manager http://localhost:9000/mcp \
    --header "Authorization: Bearer $TOKEN"
  ```

  A per-user token keeps the "acts as you" property; the hub's
  `daalu-hub-nvcm` service identity would make every call the service
  account (chapter 69 covers the per-user token exchange).
- **Client-native OAuth** (upstream's documented path: `/auth/discovery`
  returns the client ID and `services.mcp` URL) needs a public OIDC client and
  an MCP host the client can reach over HTTPS. Daalu configures the tenant
  gateway bearer-only with `oidc.interactiveRedirect=false` (chapter 23
  §23.8) and does not route the MCP host. Treat this path as unproven on a
  Daalu environment until it has been tested end to end.

What the creator should verify before recording: `mcp.enabled` is true in
the release; the tool count (21 or 22, §99.3); the token is fresh (403
*requires SSO authentication* means expired); and every answer shows a tool
call underneath it.

---

## 99.7 NV-CM's server vs Daalu's, side by side

| | NV-CM MCP server (this chapter) | `daalu mcp` (chapter 90) |
|---|---|---|
| Code | upstream `nv_config_manager/mcp/` | `src/daalu_automation/core/aiops/mcp.py` |
| Runs | inside one tenant's NV-CM environment | Daalu (see chapter 90) |
| Scope | fabric SoT, switch configs, NV-CM workflows | Daalu's own tools (see chapter 90) |
| Write control | no write tools; workflow allow-list | autonomy policy (chapter 82) |
| Auth | caller's Keycloak bearer forwarded to NV-CM APIs | see chapter 90 |

---

## Related chapters

- [Chapter 90 — MCP server and open specs](../15-ai-ops-maturity/90-mcp-server-and-open-specs.md): Daalu's own MCP server.
- [Chapter 82 — Autonomy policy](../15-ai-ops-maturity/82-autonomy-policy.md): the policy-file version of "what may an agent do", for Kubernetes writes.
- [Chapter 97 — Config backups and diffs](97-config-backups-and-diffs.md): what `run_backup` does.
- [Chapter 98 — Cable validation and ZTP](98-cable-validation-and-ztp.md): what the cable and LLDP starters do.
- [Chapter 23 — NV-CM internals](23-nvcm-internals-and-operator.md): components, Deployer, settings.
- [Chapter 68 — NV-CM tool SSO through the hub](../13-auth-integrations/68-nvcm-tool-sso-through-the-hub.md) and [chapter 69 — Per-user token-exchange](../13-auth-integrations/69-per-user-token-exchange.md): how browsers reach NV-CM, and why that path doesn't carry MCP.
- Upstream docs: `nv-config-manager/docs/overview/mcp.mdx` (setup, OAuth, security notes).

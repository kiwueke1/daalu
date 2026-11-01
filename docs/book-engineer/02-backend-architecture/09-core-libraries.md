# 9. Core libraries — kube_tools, LLM router, billing

`src/daalu_automation/core/` houses cross-cutting libraries
that don't fit any single module. This chapter is a quick
guided tour of the most important ones.

---

## `core/auth.py`

Covered in detail in Chapter 5. The public surface:

- `issue_token(user_id, tenant_id, is_admin)` — issues a JWT.
- `decode_token(token)` — verifies and returns the payload.
- `hash_password(plain)` / `verify_password(plain, hashed)`.
- `generate_pat()` / `hash_pat(token)` / `looks_like_pat(token)`.
- `_require_executor_scope(payload)` — used by write tools.
- `TokenError` — raised on decode failure.

---

## `core/bootstrap.py`

Application-startup work:

- `ensure_default_tenant()` — creates the tenant
  `00000000-0000-0000-0000-000000000000` if it doesn't
  exist. Used by webhook senders that pre-date a real
  tenant.

---

## `core/kube_tools.py`

The tool catalog that the LLM Assistant has access to. Each
tool is a callable + a `ToolSpec` describing it:

```python
class ToolSpec(BaseModel):
    name: str
    description: str
    args_schema: dict          # JSON schema
    write: bool                # if True, gated on executor scope
    cluster_required: bool     # if True, needs a federated cluster

@dataclass
class Tool:
    spec: ToolSpec
    func: Callable
```

The catalog registers, per tool:

```python
TOOLS = {
    "query_prometheus": Tool(
        spec=ToolSpec(
            name="query_prometheus",
            description="Run a PromQL query against a tenant's Prometheus.",
            args_schema={ ... },
            write=False,
            cluster_required=False,
        ),
        func=_query_prometheus,
    ),
    "restart_pod": Tool(
        spec=ToolSpec(
            name="restart_pod",
            description="Restart a Kubernetes pod by name.",
            args_schema={ ... },
            write=True,
            cluster_required=True,
        ),
        func=_restart_pod,
    ),
    # ...
}
```

> **Current shape (2026-11).** The sketch above predates the alert chat.
> In the code, `ToolSpec` (`core/kube_tools.py:47`) is
> `{name, description, input_schema, handler, requires_approval}`, the
> registry is `kube_tools.TOOLS`, and calls go through
> `execute_tool(name, input, tenant_id=, cluster_name=)`. The read tier
> includes **`recent_changes`** (`core/kube_tools.py:1104`): Deployment
> rollouts with image/env diffs, Deployment events and Argo CD syncs for a
> namespace or cluster ([ch. 86](../15-ai-ops-maturity/86-change-context-and-previews.md));
> it is also in the copilot's allowlist (`api/routers/copilot.py:63`).

Read-only tools are called directly. A write is anything
`tool_requires_approval(name, input)` (`core/kube_tools.py:1505`) says
needs approval: the five static write tools plus non-`GET`
`call_external_api`. In the alert chat a write is never executed on the
call; it goes through `validate_write_input` and then the tenant's
**autonomy policy** (`core/aiops/policy.py:decide`), which returns
`deny` (the call is answered with a tool error and nothing is queued),
`approve` (a pending `AlertAction` for N distinct approvers, the pre-AOMM
behaviour and the default with no saved policy) or `auto` (the action is
created approved and executed and verified in the background). See
[ch. 82](../15-ai-ops-maturity/82-autonomy-policy.md) and
[ch. 83](../15-ai-ops-maturity/83-guardrails-and-auto-execution.md).

Each tool's function does its own argument validation,
permission check (tenant scoping), and calls the appropriate
adapter.

---

## `core/llm_router.py`

The router decides where each inference call goes. Public
surface:

```python
async def route(request: InferenceRequest) -> InferenceResponse:
    if not _local_online(request.tenant_id):
        return await _route_cloud(request)
    if request.modality != "text":
        return await _route_cloud(request)
    if request.tokens_in > _local_max_context(request.tenant_id):
        return await _route_cloud(request)
    if _local_queue_depth(request.tenant_id) > MAX_QUEUE_DEPTH:
        return await _route_cloud(request)
    override = _tenant_override(request.tenant_id, request.tier)
    if override == "cloud":
        return await _route_cloud(request)
    return await _route_local(request)
```

Local routing reaches the customer's vLLM pod through the
federation tunnel:

```python
async def _route_local(request):
    endpoint = await _local_endpoint(request.tenant_id)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{endpoint}/v1/chat/completions",
            json=request.to_openai_format(),
        )
    return InferenceResponse.from_openai(r.json())
```

Cloud routing picks among configured providers based on
tier, current rate-limit status, and tenant preference.

The router emits two metrics:

- `llm_inference_total{destination=local|cloud, tier=...}` —
  counter.
- `llm_inference_latency_seconds{destination=..., tier=...}` —
  histogram.

Useful when investigating "why is the Assistant slow" — you
can see in Prom whether it's local stalling or cloud
spilling.

---

## `core/preferences.py`

The default user-preferences shape:

```python
DEFAULT_PREFS = {
    "theme": "system",
    "density": "comfortable",
    "accent": "indigo",
    "sidebar_collapsed": False,
    "default_landing": "home",
    "notifications": {
        "slack": True,
        "email": False,
        "browser_push": True,
    },
    "briefing_time": "08:00",
    "timezone": None,  # falls back to tenant default
}
```

The frontend reads from `users.preferences` (JSONB column)
and merges with DEFAULT_PREFS for any missing keys. New keys
can be added without a migration — the frontend just expects
defaults until users actively change them.

---

## `core/billing/`

The billing subsystem:

- `core/billing/skus.py` — the SKU catalog, with per-tier
  pricing for cloud providers.
- `core/billing/aggregation.py` — daily/monthly spend
  rollups.
- `core/billing/anomaly.py` — cost-anomaly detection logic.
- `core/billing/usage.py` — per-tenant token meter for cloud
  LLM calls.

The billing tables (`spend`, `usage_events`) are in
`models/billing.py`.

The `usage_events` table is what backs the "calls and savings"
chart on the Billing page. Each LLM call writes one row:

```python
class UsageEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "usage_events"
    tier: Mapped[str]            # 'local' or 'cloud'
    sku: Mapped[str]             # 'classifier', 'chat', 'chat-large', 'embed'
    provider: Mapped[str]        # 'local', 'anthropic', 'openai', ...
    tokens_in: Mapped[int]
    tokens_out: Mapped[int]
    cost_usd: Mapped[Decimal]    # 0 for local, computed for cloud
```

The billing rollup job runs hourly and updates the `spend`
table; the Billing page reads from `spend` for fast queries.

---

## `core/reconciler/`

The SoT reconciler:

- `core/reconciler/runner.py` — the per-device reconciliation
  algorithm.
- `core/reconciler/diff.py` — config diffing, with vendor-
  specific normalization rules.
- `core/reconciler/proposer.py` — writes the `kind=drift`
  change proposal.

Covered in detail in Chapter 14.

---

## `core/sot_executor.py`

The executor's main loop. Subscribes to the `executor` Celery
queue, processes `daalu.sot.execute_approved` tasks. Each
task:

1. Loads the proposal from the database.
2. Verifies it's still `approved` (race protection).
3. Calls the relevant adapter's write method.
4. Updates the proposal to `applied` or `error`.
5. Writes an audit log row.

The executor's identity is enforced by Celery routing
(it only subscribes to the executor queue) plus the
JWT-scope gate inside each write tool.

---

## `core/eventbus.py`

A simple in-process pub/sub for cross-module weak coupling:

```python
@on("event.created")
async def maybe_fire_alert(event: Event):
    rules = await load_matching_rules(event)
    for rule in rules:
        if rule.matches(event):
            await fire_alert(rule, event)
```

Used by `alerts` to subscribe to events from `infra`,
`integrations`, etc. without cyclic imports.

---

## `core/secrets.py`

Per-tenant encrypted secret storage:

```python
def encrypt_for_tenant(plaintext: bytes, tenant_id: UUID) -> bytes:
    key = _tenant_key(tenant_id)
    return AESGCM(key).encrypt(nonce, plaintext, aad=tenant_id.bytes)
```

Backed by a per-tenant DEK that's wrapped by an
environment-provided KEK. The KEK is a long-lived secret in
the operator app's Kubernetes Secret; rotating it requires
re-wrapping every DEK.

Integration credentials, device passwords, and webhook
secrets all use this.

---

## Modules added June–July 2026 (index)

`core/` grew a cluster of new libraries; each has an owning chapter — this
is just the map so you don't grep blind:

| Module | What it is | Chapter |
|---|---|---|
| `core/kube_console.py` | Read-only kubectl runner: fixed command catalog + generic content-negotiated GET executor (no kubectl shell-out) | 78 |
| `core/observability_console.py` | Metrics/logs console catalogs + executors (PromQL/LogQL panels, `Accept-Encoding: identity` through the edge proxy) | 78 |
| `core/grafana.py` | Grafana Integration resolution + dashboard list/get/create + panel-JSON → render-spec translation | 40 |
| `core/tenant_metrics.py` | PromQL/LogQL execution against the *tenant's* stores + `expand_template_vars` | 40 |
| `core/prometheus.py:client_for_tenant` | Tenant-vs-hub Prometheus resolution (proxy-aware; cache keyed on store) | 80 §80.4 |
| `core/topology/` | Fabric graph: `service.py` (compose), `lldp.py` (SSH sweep), `models.py` | 79 |
| `core/cluster_proxy.py:open_connect_socket` | Raw-TCP-over-HTTP-CONNECT for SSH through the edge | 79 §79.3 |
| `core/gpu/health.py` | Shared cached OpenAI-endpoint probe (card + billing pill) | 80 |
| `core/coding_gateway.py` | Coding-upstream resolution + serving-health probe | 77 |
| `core/cli_auth.py` + `api/routers/cli_auth.py` | CLI device-auth flow (mints `dpat_` PATs) | 77 |
| `core/signup.py` | Self-signup: isolated tenant + inactive user + verification token | 5 |
| `core/notify.py` | Outbound SMTP (any relay, e.g. Resend) for invites/verification | 5 |
| `core/model_catalog.py` | The coding-model catalog (pure data, cross-service) | 73 |
| `core/aiops/` | AI-Ops Maturity Model: autonomy policy, audit chain, investigations, runbooks, change context, previews, metrics, maturity, MCP server | 81–90 |
| `api/device_terminal.py` | WS bridge browser xterm ↔ asyncssh PTY on a fabric device | 79 §79.5 |

---

## What's next

Part III starts the frontend deep dive. Chapter 10 — the
Next.js app router layout.

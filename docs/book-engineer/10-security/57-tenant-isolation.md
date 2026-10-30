# 57. Tenant isolation guarantees

This is the formal contract. It's what you give to a
security reviewer when they ask "how do you ensure my data
doesn't leak to another customer."

---

## The claim

> No user of tenant A can read or write any data belonging
> to tenant B, except through Daalu engineer access which is
> logged.

The rest of this chapter is how that's enforced.

---

## Mechanism 1: every domain model is tenant-scoped

The `TenantScopedMixin` in `models/_mixins.py` requires
every domain row to have a `tenant_id` column:

```python
class TenantScopedMixin:
    tenant_id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
```

The mixin is applied to **every** model that holds customer
data: users, alerts, events, change_proposals,
cluster_tunnels, devices, integrations, audit_logs,
PATs, feedback, usage_events, etc.

Tables that are not tenant-scoped:

- `tenants` itself.
- A few platform-level metadata tables (e.g., `platform_audit_logs`).

These are admin-only by other means.

---

## Mechanism 2: every API endpoint scopes by current_user.tenant_id

The pattern in every router:

```python
async def list_alerts(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    stmt = select(Alert).where(Alert.tenant_id == user.tenant_id)
    ...
```

No endpoint accepts a `tenant_id` query parameter; the tenant
is always derived from the authenticated user.

If you're writing a new endpoint, **always** scope by
`user.tenant_id`. There is no way to write an endpoint that
crosses tenants without it being obvious in code review.

---

## Mechanism 3: 404, not 403, on cross-tenant lookup

When a user tries to read another tenant's resource by ID:

```python
async def get_alert(alert_id: UUID, ...):
    alert = await db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(404)
    if alert.tenant_id != user.tenant_id:
        raise HTTPException(404)   # NOT 403 — don't leak existence
    return alert
```

A 403 would leak the existence of the resource. 404 is the
right answer.

---

## Mechanism 4: integration credentials are isolated

Each integration's credentials are encrypted with a per-tenant
DEK derived from the tenant_id + the global KEK. Even if a
malicious user obtained another tenant's encrypted credential
blob, they couldn't decrypt without the right tenant_id (and
the KEK).

---

## Mechanism 5: LLM context is tenant-scoped

When the Assistant calls a tool, the tool's implementation
checks tenant scope:

```python
async def query_prometheus(query: str, ctx: ToolContext):
    if ctx.user.tenant_id != ctx.cluster.tenant_id:
        raise PermissionError("cross-tenant tool call")
    ...
```

The Assistant cannot "trick" itself into seeing another
tenant — the tool runtime validates.

---

## Mechanism 6: federation tunnels are per-tenant

WireGuard subnets are per-tenant. A tenant's clusters can
only see their own subnet. The hub's routing table physically
prevents tenant A's traffic from reaching tenant B's pods.

---

## Mechanism 7: Daalu engineer access is logged

When an engineer needs to read tenant data for support, they
use `daalu admin shell --tenant-id <id>`. The command:

- Records in platform_audit_logs.
- Sets a session marker that all subsequent queries log
  with elevated visibility.

There is no way for an engineer to "silently" query a tenant
— every interactive shell records its session.

---

## What this contract does NOT guarantee

To be honest:

- **It's not a hardware-isolation guarantee.** Daalu runs in
  a shared k8s cluster with shared Postgres. A sufficiently
  motivated attacker with full disk/memory access could read
  across tenants.
- **It's not a defense against engineer collusion.** A Daalu
  engineer who's willing to be discovered later can read
  data. The audit log is the deterrent, not the prevention.
- **It's not a defense against a successful API
  authentication bypass.** If you forge a JWT, you're acting
  as that user. The tenant boundary is meaningful only with
  correct auth on top.

For Enterprise customers who need stronger guarantees:

- **Single-tenant deployment** — a dedicated cluster, no
  shared Postgres. On the roadmap.
- **HSM-backed KEK** — KEK lives in a hardware security
  module. On the roadmap.

---

## Test coverage

We have integration tests that:

- Create two tenants.
- Try to access one's data from the other's API token.
- Assert 404.

Run on every CI build under `tests/integration/test_tenant_
isolation.py`. The test must pass for the build to merge.

---

## What's next

Chapter 58 — secrets management at the lifecycle level.

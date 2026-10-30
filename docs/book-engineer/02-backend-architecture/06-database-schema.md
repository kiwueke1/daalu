# 6. Database schema and migrations

This chapter is a guided tour of every table, the mixins that
shape them, and the migration discipline.

The schema is the spine of the application. Most platform
features are "a new table plus a new router."

---

## Mixins

`src/daalu_automation/models/_mixins.py`. Three mixins almost
every domain model inherits:

### UUIDPrimaryKeyMixin

```python
class UUIDPrimaryKeyMixin:
    id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid.uuid4,
    )
```

Every row has a UUIDv4 primary key. We don't use serial
integers because:

- They leak counts in URLs.
- They make sharding impossible.
- They make UUIDs in API responses inconsistent.

### TenantScopedMixin

```python
class TenantScopedMixin:
    tenant_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
```

Every domain row carries its tenant. The index is critical —
every query filters by tenant_id.

### TimestampMixin

```python
class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

For audit and ordering.

---

## Core tables

### `tenants`

```python
class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenants"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

The root of the multi-tenancy model. Every other table's
`tenant_id` FKs to this.

### `users`

```python
class User(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    briefing_modules: Mapped[str] = mapped_column(String(1024), default="infra")
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
```

`is_superuser` is the platform-team identity — different from
`is_admin` (which is tenant-admin).

`preferences` is JSONB so the frontend can evolve UI prefs
without migrations. The default shape lives at
`core/preferences.DEFAULT_PREFS`.

### `personal_access_tokens`

```python
class PersonalAccessToken(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "personal_access_tokens"
    user_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(255))
    prefix: Mapped[str] = mapped_column(String(12), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

`prefix` is the first 12 chars of the cleartext. Shown in the
UI list (so users can identify which token is which) without
keeping the full secret around.

`token_hash` is sha256-hex of the cleartext.

Unique index on `token_hash` — collisions are vanishingly
rare but we'd rather fail at create-time than silently let
two PATs share a hash.

### `integrations`

The configured connection to an external system:

```python
class Integration(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "integrations"
    type: Mapped[str]            # 'aws', 'gcp', 'slack', 'nautobot', …
    name: Mapped[str]
    config: Mapped[dict]         # JSONB, type-specific shape
    credentials_encrypted: Mapped[bytes]  # opaque encrypted blob
    status: Mapped[IntegrationStatus]    # 'ok', 'warning', 'error'
    last_probed_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
```

The `config` is type-specific JSON; the `credentials_
encrypted` blob is per-type as well.

### `events`

```python
class Event(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "events"
    source: Mapped[str]          # 'aws/acme-prod', 'webhook/x', etc.
    kind: Mapped[str]            # 'cloud.resource.created', etc.
    severity: Mapped[EventSeverity]
    labels: Mapped[dict]         # JSONB
    payload: Mapped[dict]        # JSONB
```

Partitioned on `created_at` daily; old partitions are dropped
automatically by the retention job.

### `alerts`

```python
class Alert(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "alerts"
    title: Mapped[str]
    severity: Mapped[AlertSeverity]
    status: Mapped[AlertStatus]  # 'firing', 'resolved'
    labels: Mapped[dict]         # JSONB
    started_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    assignee_id: Mapped[_uuid.UUID | None]
    rule_id: Mapped[_uuid.UUID | None]
    explanation: Mapped[str | None]  # filled by the explainer agent
```

### `change_proposals`

```python
class ChangeProposal(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "change_proposals"
    title: Mapped[str]
    kind: Mapped[str]            # 'drift', 'ai-suggested', 'manual', 'workflow'
    target: Mapped[dict]         # JSONB describing the target
    plan: Mapped[dict]           # JSONB describing the planned action
    status: Mapped[ChangeProposalStatus]  # proposed, approved, applied, denied, error
    proposed_by_user_id: Mapped[_uuid.UUID | None]
    proposed_by_agent: Mapped[str | None]
    approved_by_user_id: Mapped[_uuid.UUID | None]
    approved_at: Mapped[datetime | None]
    applied_at: Mapped[datetime | None]
    result: Mapped[dict | None]  # executor's output
```

The four-eyes constraint: `proposed_by_user_id` and
`approved_by_user_id` must differ. Enforced at the API layer
(the approve endpoint refuses to set approved_by to the
proposer).

### `cluster_tunnels`

```python
class ClusterTunnel(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "cluster_tunnels"
    name: Mapped[str]
    tunnel_ip: Mapped[str]
    wireguard_pubkey: Mapped[str | None]
    bootstrap_token_hash: Mapped[str | None]
    bootstrap_token_expires_at: Mapped[datetime | None]
    last_handshake_at: Mapped[datetime | None]
    status: Mapped[ClusterTunnelStatus]
```

One row per federated cluster.

### `feedback`

```python
class Feedback(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "feedback"
    user_id: Mapped[_uuid.UUID | None]
    user_email: Mapped[str | None]
    category: Mapped[str]
    message: Mapped[str]
    page_url: Mapped[str]
    user_agent: Mapped[str]
```

Append-only. Read by the platform team via the superuser
endpoint at `GET /feedback`.

---

## Domain tables

The remaining tables live under specific modules:

- `infra` module: `Incident`, `Service`.
- SoT: `Device`, `DeviceInterface`, `Ip`, `Site`.
- Recommendations: `Recommendation`.

Each is in its own file under `src/daalu_automation/models/`.

---

## Alembic migrations

`migrations/versions/`. Numbered, sequential, no skipping.

Naming convention: `<number>_<snake_case_title>.py`.

Each migration:

```python
revision = "0007_user_settings_pats_feedback"
down_revision = "0006_billing"

def upgrade() -> None:
    op.add_column("users", sa.Column("preferences", JSONB, ...))
    op.create_table("personal_access_tokens", ...)
    op.create_table("feedback", ...)

def downgrade() -> None:
    op.drop_table("feedback")
    op.drop_table("personal_access_tokens")
    op.drop_column("users", "preferences")
```

Downgrades are required for every migration. Even if you
never expect to run them in production, they're useful in
dev and they keep us honest about additive-only patterns
where possible.

### Running migrations

In production via the `runAlembicInitContainer` Helm value —
the daalu-api Deployment has an init container that runs
`alembic upgrade head` before the API starts. This is the
canonical migration path.

In dev: `daalu db upgrade` (a CLI wrapper).

### Patterns

Several patterns we've settled on:

- **Add columns NOT NULL with a default** to avoid table
  rewrites on large tables.
- **Add JSONB columns** when the shape is volatile. We pay
  the JSONB query cost happily for the ability to evolve
  without migration.
- **Never rename tables in a single migration.** Add the new
  one, dual-write, switch reads, drop the old one — across
  multiple deploys.
- **Indexes:** always include on `tenant_id` and any
  frequently-filtered column. Composite indexes for queries
  with multiple predicates.

Chapter 65 has the full set of migration patterns and
anti-patterns.

### Recent migrations (June–July 2026) and the dual-head lesson

The chain through `migrations/versions/` currently ends at:

| Migration | Adds | Chapter |
|---|---|---|
| `0026_workspace_git_token` | workspace private-repo auth | 76 |
| `0027_self_signup_email_verification` | `users.email_verified_at` + `email_verification_tokens` | 5 |
| `0028_cli_device_authorizations` | `cli_device_authorizations` — **also a merge migration** | 77 |
| `0029_workflow_run_alert_steps` | agent workflow runs | 40 |
| `0030_config_manager_environments` | `name`/`slug`/`is_primary` on `config_manager_tenants`, partial unique index, backfill | 23 §23.9 |
| `0031_config_manager_gateway_url` | `gateway_internal_url` on the row (additive, nullable, seeded for primaries) | 23 §23.9 |

The **0028 dual-head story** is the discipline lesson: 0026 (workspace git
token) and 0027 (self-signup) were developed on parallel branches, both
chaining off 0025 — leaving alembic with **two heads**, which
`alembic upgrade head` refuses. 0028 is a *merge* migration whose
`down_revision` names both. If you develop migrations on parallel branches,
check `alembic heads` before merging; a merge migration after the fact is
fine, but a prod deploy meeting two heads is an outage
(`daalu-api`'s init container fails and the rollout wedges). Note also that
CI runs no Postgres and never invokes alembic — constraint-heavy migrations
like 0030 must be verified by hand against a scratch database (its four
invariants were).

One CI footnote: gitleaks' generic-api-key rule false-positives on
`NNNN_slug` revision identifiers when a merge migration names two revisions
on one line — the shape is allowlisted in the gitleaks config (commit
d382f9f), so don't "fix" a secret-scan failure there by renaming revisions.

---

## What's next

Chapter 7 — the Celery worker pool.

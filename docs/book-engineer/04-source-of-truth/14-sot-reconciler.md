# 14. Reconciler architecture

The Source of Truth reconciler is the agent that compares
intended device config (in Nautobot) with live device state
(on the box itself) and writes change proposals when they
diverge. It runs continuously.

Code lives in `src/daalu_automation/core/reconciler/`.

---

## Cycle

The reconciler's main loop runs as a Celery task fired by
Beat:

```python
# beat_schedule.py
"reconcile-every-5min": {
    "task": "daalu.sot.reconcile_all",
    "schedule": crontab(minute="*/5"),
},
```

The `reconcile_all` task:

1. Enumerates eligible tenants (those with a connected
   Nautobot integration).
2. For each tenant, lists devices the reconciler is
   configured to watch.
3. Fans out one `reconcile_device(device_id)` task per device,
   subject to a concurrency limit.

Per-device intervals are configurable; the top-level
`reconcile_all` runs every 5 minutes and skips devices
that have been reconciled within their own interval.

---

## Per-device reconciliation

`core/reconciler/runner.py`:

```python
async def reconcile_device(device_id: UUID) -> ReconcileResult:
    device = await load_device(device_id)
    if device.reconcile_disabled:
        return ReconcileResult.skipped("disabled")

    # 1. Pull intended config from Nautobot
    intended = await fetch_intended_config(device)

    # 2. Pull live config from device via its driver
    live = await fetch_live_config(device)

    # 3. Compute structured diff with normalization
    diff = compute_diff(intended, live, device.vendor)

    # 4. Persist the reconcile result
    await record_reconcile_run(device, intended, live, diff)

    # 5. If diff is non-empty, write a proposal (if none open)
    if diff.is_empty():
        return ReconcileResult.in_sync()
    if await has_open_proposal(device):
        return ReconcileResult.drift("existing-open")
    await write_drift_proposal(device, diff)
    return ReconcileResult.drift("new")
```

The "no drift-on-drift" guarantee: if a device already has an
open drift proposal, the reconciler won't write another.

---

## Diff and normalization

`core/reconciler/diff.py` is where most of the per-vendor
complexity lives.

Two layers:

1. **Lexical diff** — line-by-line, with whitespace and
   comment normalization.
2. **Structural diff** — for vendors where we have a
   parser (Cisco IOS, Arista EOS to a partial degree),
   we parse into structured objects and diff those. Catches
   reordering that's lexically different but semantically
   the same.

Default rules:

- Strip trailing whitespace.
- Collapse runs of blank lines.
- Strip comments matching `^!\s*` (Cisco-style).
- Ignore timestamps in `! Last configuration change at …`
  lines.

Per-tenant rules can override defaults via
*Settings → Reconciliation → Normalization*.

---

## Device drivers

Connecting to a real device is the messy part. We use a small
adapter layer:

```python
class DeviceDriver(Protocol):
    async def fetch_running_config(self, device: Device) -> str: ...
    async def apply_config(self, device: Device, config: str) -> ApplyResult: ...
```

Implementations:

- `IOSDriver` — Cisco IOS / IOS-XE. NAPALM under the hood.
- `NXOSDriver` — Cisco NX-OS.
- `EOSDriver` — Arista. EAPI.
- `JunosDriver` — Juniper.
- `SROSDriver` — Nokia.

Drivers are selected by `device.vendor` plus `device.model`
keys.

Authentication credentials come from one of:

- Nautobot's secrets store (preferred — single source).
- Daalu's per-tenant device credentials (fallback).

Stored encrypted via `core/secrets.py`.

---

## Concurrency and rate limiting

Default global concurrency limit: 10 simultaneous device
operations. The limit is per-tenant; large fleets across many
tenants run independently.

Per-device backoff: if a device fails three times in a row,
back off exponentially up to 1 hour before retrying.

---

## What gets recorded

Every reconcile run writes a `reconcile_runs` row:

```python
class ReconcileRun(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "reconcile_runs"
    device_id: Mapped[UUID]
    status: Mapped[str]              # 'in_sync', 'drift', 'error', 'skipped'
    diff_lines_added: Mapped[int]
    diff_lines_removed: Mapped[int]
    duration_ms: Mapped[int]
    error: Mapped[str | None]
    proposal_id: Mapped[UUID | None]
```

This backs the *Routine runs* tab on the Operations page
(Chapter 18 of the user guide).

---

## Failure modes and recovery

Common failures:

- **Device unreachable.** Recorded as `error` with the
  network error in `error`. Backoff applies.
- **Auth failure.** Recorded; an alert can be configured to
  fire on auth failures.
- **Diff parse failure.** Rare; the reconciler degrades to
  lexical-only diff and logs the parse error.
- **Nautobot unavailable.** All devices' reconciles fail
  this cycle; resume on the next.

The whole reconciler is designed to be robustly restartable.
It never holds state in memory across runs.

---

## What's next

Chapter 15 — the Nautobot adapter specifically.

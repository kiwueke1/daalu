# 82. Autonomy policy as code

The autonomy policy is a per-tenant YAML document that decides, for every
**write** the agent wants to make, whether the write is denied, waits for
human approval, or runs on its own. It is the practice that turns Level 2
(every change approved) into Level 3 (proven, low-risk changes run
automatically), and its guardrails are what make Level 5 safe.

Code: `src/daalu_automation/core/aiops/policy.py` (schema, `decide`,
persistence), `src/daalu_automation/api/routers/aiops_policy.py` (API).
Guardrails and automatic execution are chapter 83.

---

## The document

```yaml
version: 1
investigate_automatically: true     # start triage on every new alert (chapter 84)
defaults:
  mode: approve                     # deny | approve | auto | auto_after_trust
  approvers: 1                      # 1..5 distinct humans
guardrails:
  max_auto_actions_per_hour: 10     # null = no cap
  circuit_breaker:                  # null = no breaker
    cooldown_minutes: 60
  freeze_windows:
    - days: [sat, sun]
      start: "00:00"
      end: "23:59"
      tz: UTC
rules:
  - id: no-writes-in-kube-system
    match: {namespaces: [kube-system]}
    mode: deny
  - id: dev-restarts
    match: {tools: [restart_deployment, delete_pod], clusters: ["dev-*"]}
    mode: auto
  - id: scale-earned
    match: {tools: [scale_deployment]}
    mode: auto_after_trust
    trust: {min_successes: 10, max_failure_rate: 0.05, window_days: 30}
  - id: prod-rollback
    match: {tools: [rollout_undo], namespaces: ["prod-*", "default"]}
    mode: approve
    approvers: 2
```

The schema is a set of strict Pydantic models (`extra="forbid"`, so a typo
is an error rather than a silently ignored key):

| Model | Line | Fields |
|---|---|---|
| `PolicyDoc` | `core/aiops/policy.py:134` | `version: 1`, `investigate_automatically` (default `false`), `defaults`, `guardrails`, `rules` (ids must be unique, `:147`) |
| `Defaults` | `:85` | `mode` (default `approve`), `approvers` (1–5, default 1) |
| `Rule` | `:70` | `id` (1–128 chars), `description`, `match`, `mode`, `approvers` (1–5), `trust`, `allow_external_auto` (default `false`) |
| `Match` | `:52` | `tools`, `clusters`, `namespaces`, `severities`: lists of `fnmatch` globs; an omitted key matches everything |
| `Trust` | `:64` | `min_successes` (default 10), `max_failure_rate` (default 0.05), `window_days` (default 30, max 365) |
| `Guardrails` | `:128` | `max_auto_actions_per_hour`, `circuit_breaker`, `freeze_windows` |
| `CircuitBreaker` | `:90` | `cooldown_minutes` (default 60) |
| `FreezeWindow` | `:94` | `days` (`mon`..`sun`, normalised to three lowercase letters), `start`/`end` (`HH:MM`), `tz` (any `zoneinfo` name) |

`severities` is not in the design doc's example; it matches the alert's
severity (`info`, `warning`, `critical`). `policy_json_schema()`
(`core/aiops/policy.py:199`) publishes the same model as
`spec/autonomy-policy.schema.json` (chapter 90), so the published schema and
the validator can't drift.

`parse_policy` (`core/aiops/policy.py:180`) turns YAML into a `PolicyDoc`
and flattens Pydantic errors into one `PolicyError` message of
`loc: msg; loc: msg` pairs, which the API returns as a 422. Empty YAML
parses to the defaults.

---

## Writes only

The policy is consulted only for calls that `kube_tools.tool_requires_approval`
says are writes (`core/kube_tools.py:1505`): the five static write tools
(`rollout_undo`, `scale_deployment`, `restart_deployment`, `delete_pod`,
`patch_resource`, `core/kube_tools.py:1327`) and `call_external_api` with any
method other than `GET`. Read tools always run.

We considered making reads policy-governed too ("never read Secrets in
prod") and decided against it for v0.1. The read tools already can't
mutate, the cluster's RBAC already scopes what the remediator
ServiceAccount can see, and a policy that could deny reads would let a
tenant accidentally blind the investigation and then blame the model for a
wrong root cause. Reads are recorded instead: every read the agent makes
writes a `tool.read` audit event (`api/routers/alert_chat.py:1103`).

---

## `decide()`

Two functions share the work: one gathers the facts, the other makes the
call.

- **`decide_for_tenant`** (`core/aiops/policy.py:482`) is the one the rest
  of the code calls: the alert chat (`api/routers/alert_chat.py:974`) and
  the MCP server (`core/aiops/mcp.py:453`). It reads from the database
  everything the decision depends on, then passes it to `decide`.
- **`decide`** (`:295`) makes the decision. It reads nothing else: no
  database, no clock, no globals. It looks only at its three arguments, so
  the same arguments always give the same `Decision`.

```python
decision = decide(policy, WriteRequest(tool, cluster, namespace, severity, external), ctx)
# -> Decision(mode, rule_id, reason, approvers, configured_mode)
```

| Argument | What it is |
|---|---|
| `policy` | The tenant's parsed `PolicyDoc` (`load_policy`, `:374`). |
| `WriteRequest` (`:211`) | The write being asked for: tool, cluster, namespace, the alert's severity, and `external` (proposed by an outside agent over MCP). |
| `PolicyContext` (`:234`) | What's true for the tenant right now, built by `build_context` (`:464`). Its fields are below. |

| `PolicyContext` field | Used for | Where `decide_for_tenant` gets it |
|---|---|---|
| `now` | Freeze windows | The current UTC time. |
| `kill_switch` | Pausing all automatic writes | The tenant's `aiops_tenant_state` row (`get_state`, `:407`). |
| `breaker` | Circuit breakers: rule id → when its cooldown ends | The same row. |
| `auto_actions_last_hour` | The hourly cap | A count of the tenant's actions run automatically in the last hour (`auto_actions_since`, `:446`), plus `pending_auto`: writes already decided `auto` earlier in the same model turn but not saved yet. |
| `trust` | `auto_after_trust`: rule id → verified successes and failures | `trust_stats` (`:418`), loaded only when the matched rule's mode is `auto_after_trust`. |

The point of the split is testing. A test can build the context by hand
and check the answer, with no database to set up:

```python
# tests/test_aiops_policy.py
req = pol.WriteRequest(tool="restart_deployment", cluster="dev-a", namespace="app")
assert pol.decide(doc, req, ctx(kill_switch=True)).mode == "approve"
assert pol.decide(doc, req, ctx(now=SATURDAY)).mode == "approve"  # freeze window
```

That is how `tests/test_aiops_policy.py` checks every step of the order
below. It also tells you where to look for a bug: a wrong decision for the
given facts is in `decide`; wrong facts (a miscounted cap, stale trust
numbers) are in `build_context` and the helpers it calls.

The order, exactly as coded:

1. **Match.** `matching_rule` (`:268`) walks `rules` top to bottom; the
   first rule whose every present `match` key globs the write wins
   (`fnmatch.fnmatchcase`, so globs are case-sensitive). No match uses
   `defaults` and `rule_id` is `""`.
2. **`deny`** → `Decision(mode="deny")` (`:308`). Nothing below can relax it.
3. **`approve`** → `Decision(mode="approve", approvers=N)` (`:310`). The
   guardrails below only ever turn `auto` into `approve`; they never apply
   to a write that already needs a human.
4. **`auto_after_trust`** (`:314`): `approve` until the rule has
   `min_successes` verified successes and a failure rate at or below
   `max_failure_rate` in its window (chapter 83); otherwise continue as
   `auto`.
5. **Kill switch** on → `approve` (`:330`).
6. **External agent** (`WriteRequest.external`, an MCP proposal) and the
   rule doesn't set `allow_external_auto` → `approve` (`:332`, chapter 90).
7. **Circuit breaker** for this rule (or `__defaults__`) tripped and not
   expired → `approve` (`:335`).
8. **Freeze window** active → `approve` (`:347`).
9. **Hourly cap** reached → `approve` (`:350`).
10. Otherwise `auto` (`:354`).

`Decision.mode` is the *effective* mode; `configured_mode` is what the rule
said before trust and guardrails. Every `approve` and `auto` decision carries
a human-readable `reason` ("rule 'scale-earned' has earned 3/10 verified
successes; approval required until then", "kill switch is on: all automatic
actions paused", ...) that is stored on the action and shown on the approve
card.

The design doc lists the kill switch first. In the code it comes after the
mode and trust resolution; the outcome is identical because the kill switch
can only turn `auto` into `approve`, and steps 2–3 have already returned for
everything else.

`write_request_for` (`:507`) builds the `WriteRequest` from a tool call:
the namespace comes from `tool_input["namespace"]`, the cluster from the
alert (`_cluster_for_alert`, `api/routers/alert_chat.py:370`), the severity
from the alert.

---

## Where `decide` is called

| Caller | Line | `external` |
|---|---|---|
| The alert-chat agent loop | `api/routers/alert_chat.py:970` | `False` |
| MCP `propose_action` | `core/aiops/mcp.py:453` | `True` |
| `POST /aiops/policy/simulate` | `api/routers/aiops_policy.py:153` | from the request |

In the agent loop (`_assistant_turn_loop`, `api/routers/alert_chat.py:887`)
each write in the model's reply is first validated
(`kube_tools.validate_write_input`), then decided. The effective mode, rule,
reason and approver count are copied onto the `AlertAction`
(`api/routers/alert_chat.py:1059`); `auto` creates it already `approved`
with `auto_executed=True` (chapter 83).

### Why `deny` bounces the write back to the model

A denied write is answered with a tool error, not dropped
(`api/routers/alert_chat.py:977`):

```
error: denied by the autonomy policy (rule 'no-writes-in-kube-system' denies
patch_resource). Do not retry this action: propose a different remediation,
or tell the operator which manual steps to run.
```

No `AlertAction` is created, so no approve card appears; an
`action.denied` audit event records the attempt. The error reuses the
invalid-write bounce path (`write_errors`, bounded by
`_MAX_INVALID_WRITE_RETRIES = 2`, `api/routers/alert_chat.py:772`): while
retries remain, *all* writes in that turn are bounced so the plan is
re-proposed as one piece.

The alternatives were to show the denied write as a greyed-out card, or to
drop it silently. A greyed card trains operators to ignore cards and gives
them nothing to do; a silent drop leaves the model believing its fix is
queued, so its write-up describes a plan that will never happen. Bouncing
keeps the model honest: it has to find a permitted fix or hand the operator
manual steps, and the transcript shows why.

---

## Storage

| Table | Model | Notes |
|---|---|---|
| `autonomy_policies` | `AutonomyPolicy`, `models/aiops.py:43` | One row per saved version: `version`, `yaml`, `note`, `updated_by`. Unique `(tenant_id, version)`. The highest version is active. |
| `aiops_tenant_state` | `AIOpsTenantState`, `models/aiops.py:55` | One row per tenant (`tenant_id` is the PK): `kill_switch`, `kill_switch_by`, `kill_switch_at`, `breaker` (JSON `rule_id → ISO until`). Created lazily by `get_state` (`core/aiops/policy.py:407`). |

`save_policy` (`core/aiops/policy.py:386`) validates, then inserts
`version = current + 1`. Versions are never updated or deleted; "restore"
in the UI loads an old version's YAML into the editor and saving it creates
a new version. We store the YAML text rather than the parsed JSON so that
comments and ordering, which is how people explain policy to each other,
survive.

`load_policy` (`:374`) returns the built-in `DEFAULT_POLICY_YAML` (`:158`)
when no row exists **and** when the stored YAML no longer parses (e.g. after
a schema change). Failing safe to "approve everything" is the only
acceptable failure mode: a policy that can't be read must never be read as
permissive.

### Why the default is the pre-AOMM behaviour

```yaml
version: 1
investigate_automatically: false
defaults: {mode: approve, approvers: 1}
guardrails: {max_auto_actions_per_hour: 10, circuit_breaker: {cooldown_minutes: 60}, freeze_windows: []}
rules: []
```

Every write needs one approval and nothing investigates on its own, which
is exactly what the alert chat did before this branch. Migration 0034 also
backfills existing `alert_actions` with `policy_mode=''`,
`approvals_required=1`, `actor='agent'`. Upgrading is a no-op for every
tenant until an admin saves a policy.

---

## API

All under `/api/v1/aiops`, in `api/routers/aiops_policy.py`:

| Method | Path | Line | Who | Notes |
|---|---|---|---|---|
| GET | `/policy` | `:80` | any user | `PolicyOut`: `yaml`, `version` (0 = built-in default), `updated_at`, `note`, `parsed`, kill switch fields, `breaker`, and `json_schema` with `?schema=true`. |
| PUT | `/policy` | `:91` | admin | Body `{yaml, note}`. 422 with the flattened validation message. Writes `policy.updated` (data includes the full YAML). |
| GET | `/policy/history` | `:111` | any user | Last 100 versions with `updated_by` email. |
| POST | `/policy/simulate` | `:134` | any user | `{tool, input, cluster, severity, external, yaml?}`; with `yaml` it tests a draft. Returns the decision plus `context` (kill switch, auto actions last hour, trust stats). Read-only. |
| POST | `/policy/kill-switch` | `:169` | anyone ON, admin OFF | Chapter 83. |
| POST | `/policy/breaker/reset?rule_id=` | `:193` | admin | Chapter 83. Not in the design doc's API table. |
| GET | `/trust` | `:213` | any user | Per-rule track record (chapter 83). |

"Admin" is `user.is_admin or user.is_superuser`. The simulator is open to
everyone on purpose: non-admins should be able to test a proposed policy
before asking an admin to save it. The UI (`frontend/app/autonomy/page.tsx`)
does exactly that: the editor is editable for everyone, "Save new version"
is disabled for non-admins, and "Test a write" runs against the draft.

---

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Save returns 422 `rules.2.mode: Input should be ...` | Schema violation | The message names the path; the Autonomy page shows it under the editor. |
| A rule never matches | Globs are case-sensitive, and the cluster is the alert's `cluster` metadata (or `labels.cluster`), not the integration's display name. An alert with no cluster tag matches as `""`, which only `*` (or an omitted `clusters` key) matches | Run "Test a write" with the exact cluster/namespace. |
| Everything is `approve` after an upgrade | Stored YAML no longer validates, `load_policy` fell back to the default | `GET /aiops/policy` still returns the stored YAML; re-save a valid version. |
| Agent keeps proposing the same denied write | It used up the bounce budget | After two bounces the denied write is still answered with the error but other writes in the turn are queued; check the transcript. |

---

## Testing

* `tests/test_aiops_policy.py` — the decision table: default is Level 2,
  first match wins, deny, auto, multi-approver, `auto_after_trust`
  thresholds, guardrails demote `auto` but never relax `deny`, external
  downgrade, freeze window across midnight, invalid policies rejected, the
  published JSON Schema.
* `tests/test_aiops_flow.py::test_deny_never_creates_an_action` — the
  agent-loop integration.
* `tests/test_specs.py` — `spec/autonomy-policy.schema.json` matches the
  model.

The DB-backed tests need the Postgres test database from `tests/conftest.py`
(`DAALU_TEST_DATABASE_URL`).

# 86. Change context and previews

Two small features that make the agent's reads and writes more legible.
`recent_changes` answers the first question of most incidents, "what
changed?", in one read call (Level 1). The **preview** tells the approver
what a proposed write will actually do before they click (Level 2/3): "roll
back from revision 7 to 6: image `nginx:bad` → `nginx:1.27-alpine`, all 3
pods replaced".

Code: `core/aiops/change_context.py`, `core/aiops/preview.py`,
`core/kube_tools.py` (tool registration).

---

## `recent_changes`

A read tool registered in `kube_tools.TOOLS` (`core/kube_tools.py:1104`)
with handler `_recent_changes` (`:531`), inputs `namespace` (omit for the
whole cluster) and `hours` (default 24, clamped to 1–336). Because it is in
the read tier it auto-executes in the alert chat, is offered to external
agents through `run_read_tool` and the dedicated `recent_changes` MCP tool,
and is in the copilot's allowlist and per-cluster tool set
(`api/routers/copilot.py:63`, `:75`). The alert-chat system prompt tells the
model to "check recent_changes early: most incidents follow a change"
(`api/routers/alert_chat.py:76`).

`change_context.recent_changes` (`core/aiops/change_context.py:179`) uses
`kube_tools._tenant_kube_or_default`, so it reaches the tenant's cluster
through the federation exactly like the other kube tools, and runs three
reads concurrently:

| Source | Function | What it produces |
|---|---|---|
| ReplicaSets (namespaced or all) | `_rollout_lines`, `:80` | One line per Deployment revision created in the window. The first revision shows its images; each later one shows what differs from the previous revision: image per container, plus env/args/command via `kube_tools._template_settings` (`_diff`, `:63`, at most six differences). The newest revision is marked `[current]` with its ready count. |
| Events with `involvedObject.kind=Deployment` | `_event_lines`, `:111` | Every Deployment event in the window (`ScalingReplicaSet` and anything else the controller reports), with repeat counts. |
| Argo CD `Application`s | `_argo_apps`, `:159`; `_argo_lines`, `:128` | `status.history` entries (`deployedAt`, revision, repo/chart, target revision) and the last `operationState`, filtered to Applications whose destination namespace is the one asked about. |

Output is plain text, newest first, at most 40 lines (`_MAX_LINES`) with a
"N older entries omitted" line, then `note:` lines for anything that could
not be read. Each source degrades independently: a 404 on the Argo CD CRD
means "not installed" (silent), a 403 becomes an RBAC note, and a failure on
ReplicaSets or events becomes a note while the other sources still answer.

### Why derive it from ReplicaSets instead of an event store

The obvious design is to record every deploy as it happens. We didn't,
because Daalu does not see every deploy (most come from CI or Argo CD), and
the cluster already keeps the history: each Deployment revision is a
ReplicaSet with a `deployment.kubernetes.io/revision` annotation and a
creation time. Reading it at question time costs one list call and needs no
agent, webhook or retention policy. The limit is
`revisionHistoryLimit` (10 by default): older revisions are gone. Argo CD
history fills the gap for GitOps-managed apps.

---

## Previews

`preview_write(tool, tool_input, *, tenant_id, cluster)`
(`core/aiops/preview.py:271`) reads the target object and describes what
approving the write would do. It returns

```json
{"summary": "...", "blast_radius": {"pods": 3, "deployments": 1}, "details": {...}}
```

or `{}` when there is nothing to preview. It never raises: every exception,
and a 25 s timeout (`_TIMEOUT_S`), returns `{}` and logs
`aiops.preview.unavailable`. The alert chat wraps it again in
`_preview_write` (`api/routers/alert_chat.py:485`) for the same reason.

| Tool | Previewer | What it reads | Summary |
|---|---|---|---|
| `scale_deployment` | `_scale`, `:58` | the Deployment | current → target replicas, pods added/removed, "goes fully offline" at 0 |
| `restart_deployment` | `_restart`, `:79` | Deployment + pods by selector | pods recycled; Recreate (brief downtime) vs RollingUpdate |
| `rollout_undo` | `_undo`, `:93` | Deployment + ReplicaSets | current vs target revision (`to_revision` or previous), image change or setting changes, all pods replaced; "will be refused" with fewer than two revisions; "will fail" for an unknown `to_revision` |
| `delete_pod` | `_delete_pod`, `:143` | the pod, its owner, the owning Deployment | whether a controller recreates it (`ReplicaSet`, `StatefulSet`, `DaemonSet`, `Job`, `ReplicationController`) or it is gone for good |
| `patch_resource` | `_patch`, `:201` | before + a **server-side dry run** (`dry_run="All"`) | changed key paths (noise such as `resourceVersion`, `managedFields`, `status` filtered); pods replaced when the pod template changes; a note that ConfigMap/Secret changes need a restart; "Server-side dry run REJECTED" with the API server's reason |

`patch_resource` previews support Deployment, StatefulSet, DaemonSet,
ReplicaSet, Pod, ConfigMap, Service and Secret. Changed Secret keys are
reported as `data.<redacted>` so a preview never puts secret material in
the database or the UI.

### Where previews go

In the agent loop every write gets a preview before its `AlertAction` is
stored (`api/routers/alert_chat.py:1067`); MCP `propose_action` does the
same (`core/aiops/mcp.py:504`). It is stored in `AlertAction.preview` and
rendered by `ActionPolicyInfo`
(`frontend/components/aiops/action-policy.tsx`) as the "Preview:" line with
the blast radius.

### Design decisions

* **A server-side dry run, not a client-side diff, for patches.** Admission
  webhooks, defaulting and validation only happen in the API server. A
  client-side merge would show changes the server would reject or miss
  fields the server would default.
* **Previews inform; they don't gate.** A preview that says the dry run was
  rejected does not stop the write or change the policy decision. Making the
  preview a gate would turn a best-effort read (which can time out, or hit
  RBAC limits the executor doesn't have) into a reason to block remediation.
  The approver sees the rejection; an `auto` write runs and fails
  verification, which trips the breaker (chapter 83).
* **Computed at proposal time.** The preview describes the cluster when the
  write was proposed. If an approval comes an hour later, the state may have
  moved; the run page shows what actually happened.

---

## Testing

`tests/test_aiops_change_context.py`: image change newest first,
cluster-wide with Argo CD, graceful degradation, the tool registration, and
each previewer (`rollout_undo`, scale/restart/delete, patch via server dry
run, never raises). All with a fake kube client; no cluster needed.

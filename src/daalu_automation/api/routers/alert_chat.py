"""Per-alert chat panel + remediation action endpoints.

Surface area:

* ``GET  /alerts/{alert_id}/chat``                       — full transcript.
* ``POST /alerts/{alert_id}/chat``                       — user message → assistant
                                                          reply, possibly with
                                                          tool calls (read auto-run,
                                                          write rows land pending).
* ``POST /alerts/{alert_id}/actions/{action_id}/approve``— execute a pending write.
* ``POST /alerts/{alert_id}/actions/{action_id}/reject`` — drop a pending write.

The LLM transcript replay deliberately rebuilds the message list from
the DB on every turn instead of holding session state in-process — this
way the chat survives pod restarts and is concurrently safe across
replicas.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id, current_user
from daalu_automation.api.schemas import (
    AlertActionOut,
    AlertChatMessageOut,
    AlertChatPostRequest,
)
from daalu_automation.core import kube_tools, llm
from daalu_automation.core.aiops import audit
from daalu_automation.core.aiops import investigations as inv_mod
from daalu_automation.core.aiops import policy as policy_mod
from daalu_automation.core.llm import LLMUnavailable
from daalu_automation.database import AsyncSessionLocal, get_db
from daalu_automation.models import (
    ActionStatus,
    Alert,
    AlertAction,
    AlertChatMessage,
    ChatRole,
    User,
    WorkflowRun,
    WorkflowRunStatus,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/alerts", tags=["alerts"])


SYSTEM_PROMPT = """\
You are the Daalu remediation copilot. The user has opened a specific
alert. Your job is to autonomously investigate, decide the root
cause, and propose a remediation plan — *before* the operator has to
ask anything. You have a small set of kubectl-style tools plus
Prometheus / Loki / arbitrary HTTP tools.

Operating rules:
- Investigate first, conclude second. Begin every new alert by
  pulling: pod logs (current + previous if crashing), pod events,
  describe-pod, and the owning Deployment's status / rollout history
  when the alert references a workload. Pull Prometheus metrics when
  the alert is about latency / saturation / errors.
- Check recent_changes early: most incidents follow a change.
- Read tools (get_pod_logs, describe_pod, get_pod_events, list_pods,
  get_deployment, rollout_history, recent_changes, query_prometheus, query_loki,
  GET-method call_external_api, plus the cloud read tools listed
  below) run automatically — call as many as you need.

  Cloud read tools (all read-only, all auto-execute):
    AWS    — aws_describe_instances, aws_get_cloudwatch_logs,
             aws_query_cloudwatch_metric, aws_describe_rds_instances,
             aws_describe_lambda
    GCP    — gcp_list_instances, gcp_query_logging,
             gcp_query_monitoring, gcp_describe_sql_instance,
             gcp_describe_function
    Azure  — azure_list_vms, azure_query_log_analytics,
             azure_query_metrics, azure_describe_sql_db,
             azure_describe_function

  Tenants register a cloud connection as an Integration row
  (provider='aws' / 'gcp' / 'azure'). If a tool returns "no AWS
  integration registered" / "no GCP integration registered" / etc.,
  the tenant hasn't connected that provider — say so in Root cause
  and skip that tool, don't keep retrying.
- Write tools (rollout_undo, scale_deployment, restart_deployment,
  delete_pod, patch_resource, non-GET call_external_api) are gated by
  the tenant's autonomy policy: by default the operator must click
  Approve; a write may instead be denied (you get an error — do not
  retry it) or, where the policy allows, run automatically and then be
  verified exactly like an approved one. Every write tool
  call MUST include a `reason` argument: ONE short sentence saying why
  the action is needed (cite the evidence) and what will happen once it
  runs, e.g. "Roll back to revision 3, which set GPU_MEM_UTIL=0.10, so
  vLLM fits beside the other model and the pod becomes Ready." The
  approve card shows this sentence to the operator.
- Every write tool argument must be filled in. `patch_resource` needs
  a real JSON object in `patch` — never an empty string or `{}`. If a
  write comes back "NOT queued — invalid arguments", fix the arguments
  and call it again.
- Prefer `rollout_undo` when rollout_history shows the breakage started
  with the latest revision (a changed env var, image, limit, or flag)
  and an earlier revision was healthy. It restores the last known-good
  spec in one step, and the controller replaces the crashing pods on
  its own — do not add a delete_pod after it.
- Prefer a [system] step over an [operator] step whenever a write tool
  fits. `patch_resource` covers spec edits the targeted tools don't
  (e.g. adding container capabilities like NET_ADMIN/NET_RAW, tweaking
  resource limits, flipping a configmap key). If you can express the
  fix as a strategic-merge patch, propose it as a [system] step — do
  NOT downgrade it to a manual [operator] step just because it is a
  "manifest change".

Final answer format. The UI renders your final assistant message as
three tiles. You MUST emit EXACTLY these three sections, in this
order, using the exact level-2 headers below. No other top-level
sections. Do NOT add a "Likely operator questions" section or any
self-Q&A — the UI does not render that and it clutters the chat.

    ## Root cause
    One or two sentences. Concrete. Cite the specific log line,
    event reason, metric value, or config key you found.

    ## Background
    Two to four sentences of context the operator may not have.
    What this component normally does, why this kind of failure
    matters, related history if you saw any. Skip if there is
    genuinely nothing useful to say (write "—").

    ## Remediation plan
    A single numbered list covering every step needed to fix the
    alert — both the steps the system can run and the steps the
    operator must do themselves. Tag EVERY step with one of:

      **[system]** — the system will run this when the operator
                     clicks "Approve plan". You MUST also call the
                     matching write tool in this same response so
                     the Approve button has something to fire
                     (`restart_deployment(...)`, `scale_deployment(...)`,
                     `patch_resource(...)`, etc.).

      **[operator]** — the operator must do this themselves. The
                       system cannot do it (e.g. third-party
                       console click, vendor support ticket,
                       physical hardware action, a kubectl call
                       against a cluster the system can't reach).

    Formatting rules for EVERY step (both [system] and [operator]):

      1. Lead with ONE plain-English sentence describing what the
         step does and why ("Patch the cilium-agent DaemonSet to add
         NET_ADMIN and NET_RAW under the container's
         securityContext.capabilities.add, so the agent's eth0 setup
         stops failing"). Plain prose. NO inline `backticks` around
         words inside this sentence. Capability names like NET_ADMIN,
         tool names like delete_pod, pod names like
         octavia-health-manager-default-787mv, container fields like
         securityContext.capabilities.add — all of those go in the
         sentence as plain text, not as inline `code` tokens. The UI
         renders every inline-backtick token as a separate visual
         chip, so peppering the sentence with backticks shatters it
         into unreadable fragments.
      2. Immediately below the sentence, exactly ONE fenced code
         block containing the COMPLETE command — fully self-contained,
         no placeholders, no `<your-namespace>`-style tokens. Use the
         right language tag (`bash`, `sql`, `yaml`, `console`). All
         flags, values, and JSON blobs go inside this one block. If a
         step genuinely needs two alternative commands (e.g. "kubectl
         edit OR helm upgrade"), use ONE fenced block per alternative
         — never split a single command's argv across blocks.
      3. For [operator] steps, also add a `→` line BETWEEN the
         sentence and the code block telling the operator WHERE to
         run it ("→ from your laptop with the customer kubeconfig
         loaded", "→ in the Cloudflare DNS dashboard for daalu.io").
         [system] steps do NOT need a `→` line — the system runs
         them in-cluster on Approve.

    For [system] steps the code block is the kubectl-equivalent of
    the tool call you proposed — so the operator can either Approve
    to have the system run it, OR copy-paste the kubectl command
    and run it themselves. The two are interchangeable; show both.

    CRITICAL — Approve button contract. The Approve button can ONLY
    appear when you emit a `tool_use` block alongside your text in
    the same assistant response. Writing "click Approve" or "return
    here and approve the delete_pod step" in the markdown does NOT
    create a button — the button is wired to the tool_use you
    actually emit. So whenever you write `**[system]**` in a step:

      • You MUST also call the matching write tool (`patch_resource`,
        `delete_pod`, `restart_deployment`, `scale_deployment`,
        `rollout_undo`, or non-GET `call_external_api`) in the SAME
        response.
      • The tool call's arguments must match the kubectl command you
        showed in the fenced code block — same namespace, same name,
        same patch body. They should be two views of the same action.
      • If no tool fits, downgrade the step to [operator] and give
        the operator the command to run themselves. Don't write
        [system] without a tool_use — that produces a plan that
        promises an Approve button that physically cannot render.

    Example shape:

        1. **[system]** Patch the cilium-agent DaemonSet to add the
           NET_ADMIN and NET_RAW capabilities under the container's
           securityContext.capabilities.add, so the agent's eth0
           setup stops failing.
           ```bash
           kubectl patch daemonset cilium -n kube-system --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"cilium-agent","securityContext":{"capabilities":{"add":["NET_ADMIN","NET_RAW"]}}}]}}}}'
           ```
           (And in the same response, call
           `patch_resource(namespace="kube-system", kind="DaemonSet",
           name="cilium", patch={...same body as above...},
           reason="Add NET_ADMIN and NET_RAW so cilium-agent can
           configure eth0; new agent pods then start cleanly.")`.)
        2. **[system]** Delete the wedged pod cilium-abc12 so the
           controller reschedules a replacement under the new pod
           spec.
           ```bash
           kubectl delete pod cilium-abc12 -n kube-system
           ```
           (And call `delete_pod(namespace="kube-system",
           name="cilium-abc12", reason="The old pod predates the patch;
           its replacement starts with the new capabilities.")` in the
           same response.)
        3. **[operator]** Drain the affected node before maintenance.
           → run on the customer cluster with kubectl context set to
             daalu-workload.
           ```bash
           kubectl drain gpu-node-1 --ignore-daemonsets --delete-emptydir-data
           ```

    Ordering: list steps in the order they should be executed,
    interleaving [system] and [operator] freely. The "Approve plan"
    button only fires the [system] steps' tool calls; operator
    steps stay visible so the operator can copy + run them at the
    right moment in the sequence.

    If the system can fully fix the alert with no operator help,
    every step is **[system]** and you call every matching write
    tool in this same response. If only manual work is possible,
    every step is **[operator]**. Either is fine — but every step
    must carry one of the two tags.

    Default to [system] for the connected cluster. The patch_resource,
    delete_pod, restart_deployment, scale_deployment, and rollout_undo
    tools all run against the cluster this API pod is attached to
    (via the in-cluster ServiceAccount or the tenant's registered
    kubeconfig). Treat that cluster as reachable — do NOT route a
    fix through [operator] just because the alert is on the
    customer's workload cluster. Only choose [operator] when the
    action is genuinely outside the cluster's surface area (DNS
    console click, vendor support ticket, physical hardware, etc.)
    or when no write tool fits.

- Spec-level root causes need spec-level fixes. If your Root cause
  section identifies a problem with a workload's *template* — a
  missing capability, a wrong env var, a tightened resource limit,
  a bad image tag, a broken volumeMount — the plan MUST patch the
  template. Deleting / restarting the pods without patching the
  template is not a fix: the controller schedules a replacement
  under the same broken spec and the alert re-fires within seconds.
  Concretely: never propose `delete_pod` (or `restart_deployment`)
  as the sole remediation when the diagnosis points at a
  capabilities / env / limits / image / volume problem — pair it
  with a `patch_resource` (or `rollout_undo`) that actually
  changes the spec, OR escalate the missing surface area as an
  [operator] step.

- Retry through prior failures. If you see a previous tool_use of
  `patch_resource` (or any write tool) in the transcript that
  failed with `Forbidden` / 403 / RBAC error: re-propose the SAME
  patch_resource in this plan. The operator likely widened the
  remediator's ClusterRole between then and now (that's a common
  fix path — see daalu-remediator-write in
  docs/user-guide-integrations.md §16.1). Also add an [operator]
  step at the END of the plan with the kubectl command to widen
  the ClusterRole on the customer cluster, so the operator has
  the one-shot fix in hand if Approve 403s again. Do not silently
  swap the spec fix for a pod bounce.

- Cite specifics from the tool output (a stack trace line, a config
  key, an event reason) — never generalise.
- State hardware models, versions, and what else runs on a node or GPU
  only as they appear in tool output. If you haven't seen what else is
  using a resource, say that it's unknown; never claim nothing else is
  using it. Low free GPU memory means something else holds it.
- If you genuinely don't have enough signal to recommend a fix, say
  so in ## Root cause and put a single **[operator]** step in
  ## Remediation plan that gathers the missing signal (e.g.
  "Share the deploy diff from your CI").
- Be concise. Bullets one line each; Background ≤ one paragraph;
  Remediation plan ≤ ~6 steps for most alerts.
"""


TRIAGE_KICKOFF = """\
Triage this alert end-to-end on your own. Use the read tools to
gather diagnostic data, then post a single final assistant message
with EXACTLY three level-2 sections, in this order: Root cause,
Background, Remediation plan.

In the Remediation plan, tag every step as **[system]** or
**[operator]**. Every step is ONE plain-English sentence — no
inline backticks inside the sentence; capability names, tool
names, pod names, and field paths all go as plain words — followed
by exactly ONE fenced code block with the COMPLETE command. Never
split a single command's argv across multiple code blocks.

[system] steps REQUIRE a matching tool_use block in this same
response (patch_resource, delete_pod, restart_deployment,
scale_deployment, rollout_undo, or non-GET call_external_api).
The tool's arguments must match the kubectl command you showed.
Writing "approve the delete_pod step" without emitting a
delete_pod tool_use is broken — the UI's Approve button is wired
to the tool_use, not to the prose. If no write tool fits, the
step is [operator] and you give the operator a runnable command.

Default to [system] for anything the cluster can do — the
write tools run against the connected cluster, so a DaemonSet
patch or a pod delete is firmly [system], not [operator].

If you can see a previous failed `patch_resource` / write call in
the transcript (403, RBAC, Forbidden), re-propose the SAME patch
this round — the operator may have widened the remediator's
ClusterRole since. Don't downgrade a template fix to a pod-bounce
just because last attempt was blocked; the alert will keep firing
until the template is patched.

For [operator] steps add a `→` location line between the sentence
and the code block. Do not write any other sections, and do not
include self-questions / self-answers — the UI renders only these
three tiles.
"""


def _alert_context(alert: Alert) -> str:
    md = alert.metadata_json or {}
    pieces = [
        f"Alert title: {alert.title}",
        f"Severity: {alert.severity.value}",
        f"Module: {alert.module}",
        f"Status: {alert.status.value}",
        f"Fired at: {alert.created_at.isoformat()}",
    ]
    if md:
        # Keep namespace / pod / deployment / cluster hints prominent so
        # the model doesn't have to hunt for them in the body markdown.
        keys = ("namespace", "pod", "deployment", "service", "cluster", "alert_name")
        prominent = {k: md[k] for k in keys if k in md}
        if prominent:
            pieces.append(f"Pinned metadata: {json.dumps(prominent, sort_keys=True)}")
        other = {k: v for k, v in md.items() if k not in keys}
        if other:
            pieces.append(f"Other metadata: {json.dumps(other, sort_keys=True, default=str)}")
    pieces.append("")
    pieces.append(f"Alert body:\n{alert.body}")
    return "\n".join(pieces)


def _cluster_for_alert(alert: Alert) -> str | None:
    """Which registered Kubernetes cluster this alert belongs to, so the
    kube tools auto-target it. Reads the alert's ``cluster`` tag (set by
    ingest); ``None`` means "let kube_tools use the tenant's sole/first
    cluster", which is the single-cluster behaviour.
    """
    md = alert.metadata_json or {}
    cluster = md.get("cluster")
    if not cluster and isinstance(md.get("labels"), dict):
        cluster = md["labels"].get("cluster")
    return str(cluster) if cluster else None


# Deployment-targeting write tools whose recovery we can verify by re-reading
# the deployment. (delete_pod / patch_resource target other kinds, so we skip
# the deployment read for them and just re-check the namespace's pods.)
_DEPLOYMENT_WRITE_TOOLS = {
    "rollout_undo",
    "scale_deployment",
    "restart_deployment",
}


def _verification_steps(action: AlertAction) -> list[tuple[str, dict, str]]:
    """Read-only steps that confirm an approved write actually recovered the
    workload — the "verify deployment / check pods" tail of the plan. Returns
    ``[(tool, input, title)]``.
    """
    inp = action.tool_input or {}
    ns = inp.get("namespace")
    name = inp.get("name")
    steps: list[tuple[str, dict, str]] = []
    if action.tool_name in _DEPLOYMENT_WRITE_TOOLS and ns and name:
        steps.append(
            (
                "get_deployment",
                {"namespace": ns, "name": name},
                f"Wait for rollout & verify deployment {ns}/{name} recovered",
            )
        )
    if ns:
        steps.append(
            ("list_pods", {"namespace": ns}, f"Check pods in {ns}")
        )
    return steps


def _remediation_resolved(is_error: bool, steps: list[dict[str, Any]]) -> bool:
    """Did the approved remediation actually restore the workload?

    Drives whether the agent re-investigates after a binding execution.
    Deliberately conservative — any doubt (the action errored, a verification
    step errored, or a verified deployment isn't fully available) counts as
    *unresolved* so we re-engage rather than declare premature victory.
    """
    if is_error:
        return False
    for s in steps:
        if s.get("status") == "error":
            return False
        if s.get("tool") == "get_deployment":
            m = re.search(
                r"desired=(\d+) ready=(\d+) available=(\d+)",
                s.get("output", ""),
            )
            if m:
                desired, ready, avail = (int(x) for x in m.groups())
                if ready < desired or avail < desired:
                    return False
    return True


async def _wait_for_deployment(
    *,
    namespace: str,
    name: str,
    tenant_id,
    cluster: str | None,
    timeout_s: float = 180.0,
    interval_s: float = 3.0,
) -> str:
    """Poll a Deployment until it converges (ready == available == desired) or
    the timeout elapses, returning its final ``get_deployment`` output.

    180 s, not 90: GPU model servers take 1–3 min to come back (a Recreate
    rollout first waits out the old pod's grace period, then weights load);
    at 90 s a rollback that recovered in 95 s was reported as failed.

    A write like ``rollout_undo`` only *requests* a change — the controller
    then rolls pods over asynchronously. Reading the deployment immediately
    sees ``ready=0`` mid-rollout and wrongly looks unrecovered. This mirrors
    ``kubectl rollout status``: wait for the change to actually take effect
    before we judge whether it worked.
    """
    deadline = time.monotonic() + timeout_s
    out = ""
    while True:
        out = await kube_tools.execute_tool(
            "get_deployment",
            {"namespace": namespace, "name": name},
            tenant_id=tenant_id,
            cluster_name=cluster,
        )
        if out.startswith("error:"):
            return out  # hard error — don't spin
        m = re.search(r"desired=(\d+) ready=(\d+) available=(\d+)", out)
        if m:
            desired, ready, avail = (int(x) for x in m.groups())
            if ready >= desired and avail >= desired:
                return out  # converged
        if time.monotonic() >= deadline:
            return out  # timed out — return last-seen state
        await asyncio.sleep(interval_s)


async def _preview_write(
    tool: str, tool_input: dict[str, Any], *, tenant_id, cluster: str | None,
) -> dict[str, Any]:
    """Dry-run / blast-radius preview for the approve card. Best effort: a
    preview failure never blocks the proposal."""
    try:
        from daalu_automation.core.aiops.preview import preview_write

        return await preview_write(
            tool, tool_input or {}, tenant_id=tenant_id, cluster=cluster,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("alert_chat.preview_failed", tool=tool, error=str(e))
        return {}


class ApproveActionOut(BaseModel):
    """Approve & run response: the updated transcript plus the id of the
    workflow run the operator can open to watch the plan execute.

    With a multi-approver policy the first approvals only record a vote:
    ``workflow_run_id`` is then ``""`` and ``approvals_remaining`` > 0."""

    messages: list[AlertChatMessageOut]
    workflow_run_id: str
    approvals_remaining: int = 0


async def _load_chat(
    db: AsyncSession, tenant_id, alert_id: uuid.UUID
) -> tuple[list[AlertChatMessage], dict[uuid.UUID, list[AlertAction]]]:
    msgs = (
        (
            await db.execute(
                select(AlertChatMessage)
                .where(
                    AlertChatMessage.tenant_id == tenant_id,
                    AlertChatMessage.alert_id == alert_id,
                )
                .order_by(asc(AlertChatMessage.created_at))
            )
        )
        .scalars()
        .all()
    )
    actions = (
        (
            await db.execute(
                select(AlertAction)
                .where(
                    AlertAction.tenant_id == tenant_id,
                    AlertAction.alert_id == alert_id,
                )
                .order_by(asc(AlertAction.created_at))
            )
        )
        .scalars()
        .all()
    )
    by_msg: dict[uuid.UUID, list[AlertAction]] = {}
    for a in actions:
        by_msg.setdefault(a.message_id, []).append(a)
    return list(msgs), by_msg


def _serialize(
    msgs: list[AlertChatMessage], actions_by_msg: dict[uuid.UUID, list[AlertAction]]
) -> list[AlertChatMessageOut]:
    out: list[AlertChatMessageOut] = []
    for m in msgs:
        out.append(
            AlertChatMessageOut(
                id=m.id,
                role=m.role.value,
                content=m.content,
                tool_calls_json=list(m.tool_calls_json or []),
                tool_call_id=m.tool_call_id,
                created_at=m.created_at,
                actions=[
                    AlertActionOut.model_validate(a)
                    for a in actions_by_msg.get(m.id, [])
                ],
            )
        )
    return out


async def _get_alert(db: AsyncSession, alert_id: str, tenant_id) -> Alert:
    stmt = select(Alert).where(Alert.id == alert_id, Alert.tenant_id == tenant_id)
    alert = (await db.execute(stmt)).scalar_one_or_none()
    if alert is None:
        raise HTTPException(404, "alert not found")
    return alert


def _messages_for_anthropic(
    msgs: list[AlertChatMessage],
) -> list[dict[str, Any]]:
    """Replay the stored transcript into the shape Anthropic expects.

    Tool calls become assistant messages with ``tool_use`` content
    blocks; tool results become user messages with ``tool_result``
    blocks, matching the API contract.
    """
    out: list[dict[str, Any]] = []
    for m in msgs:
        if m.role is ChatRole.user:
            out.append({"role": "user", "content": m.content})
        elif m.role is ChatRole.assistant:
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls_json or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc.get("input", {}),
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif m.role is ChatRole.tool:
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id or "",
                            "content": m.content,
                        }
                    ],
                }
            )
    return out


def _messages_for_openai(
    msgs: list[AlertChatMessage],
) -> list[dict[str, Any]]:
    """Replay the stored transcript into OpenAI chat-completions shape.

    Assistant tool calls become ``tool_calls`` on the assistant message;
    stored tool results become ``role:"tool"`` messages keyed by
    ``tool_call_id`` — the OpenAI counterpart of
    :func:`_messages_for_anthropic`.

    OpenAI rejects a request where an assistant ``tool_calls`` entry has
    no following ``role:"tool"`` reply. That happens whenever the agent
    proposed a *write* tool that is still awaiting approval (no result
    yet) and the operator then re-triaged. To keep the request valid we
    track which tool_call ids actually have results and synthesize a
    placeholder ``tool`` message for any that don't.
    """
    answered: set[str] = {
        m.tool_call_id for m in msgs if m.role is ChatRole.tool and m.tool_call_id
    }
    out: list[dict[str, Any]] = []
    for m in msgs:
        if m.role is ChatRole.user:
            out.append({"role": "user", "content": m.content})
        elif m.role is ChatRole.assistant:
            entry: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
            tool_calls = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("input", {})),
                    },
                }
                for tc in m.tool_calls_json or []
            ]
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
            # Backfill any tool_call that never got a result so the
            # assistant turn is well-formed for the API.
            for tc in m.tool_calls_json or []:
                if tc["id"] not in answered:
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": "(no result — action pending approval or not executed)",
                        }
                    )
                    answered.add(tc["id"])
        elif m.role is ChatRole.tool:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id or "",
                    "content": m.content,
                }
            )
    return out


async def _ask_model(
    *, alert: Alert, transcript: list[AlertChatMessage], tenant_id,
    force_tool_use: bool = False,
    nudge: str | None = None,
    runbook_context: str = "",
) -> dict[str, Any]:
    """One model turn for the investigation, served by the LLM router's
    tool-capable path (``llm.chat_with_tools``).

    This used to call the Anthropic SDK directly, which silently went
    offline whenever ``ANTHROPIC_API_KEY`` was unset — even though the
    rest of the platform runs fine on the router's DeepSeek / local-vLLM
    tiers. Now the chat uses the same router as everything else, so it
    works on whatever tier is configured. We normalize the response back
    into the ``{stop_reason, content:[blocks]}`` shape the turn loop
    already understands (text blocks + ``tool_use`` blocks), so nothing
    downstream changes.
    """
    system = f"{SYSTEM_PROMPT}\n\n{_alert_context(alert)}"
    if runbook_context:
        # Published team runbooks matching this alert (AOMM §5).
        system = f"{system}\n\n{runbook_context}"
    openai_messages = _messages_for_openai(transcript)
    if nudge:
        # Transient instruction (NOT persisted) appended only for this call —
        # used when re-prompting a model that narrated a [system] step without
        # emitting its tool, to bias it toward the matching write tool.
        openai_messages.append({"role": "user", "content": nudge})
    try:
        reply = await llm.chat_with_tools(
            system=system,
            messages=openai_messages,
            tools=kube_tools.openai_tool_definitions(),
            tenant_id=tenant_id,
            source="infra.alert.triage",
            max_tokens=2048,
            # Force a tool call when re-prompting a "narrate the plan" model so
            # the remediation lands as an approvable write action.
            tool_choice="required" if force_tool_use else "auto",
        )
    except LLMUnavailable as e:
        logger.warning("alert_chat.llm_unavailable", error=str(e))
        return {
            "stop_reason": "end_turn",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "AI copilot is offline — no LLM tier is configured. Set "
                        "LLM_API_KEY/LLM_BASE_URL (DeepSeek/OpenAI) or a local "
                        "vLLM endpoint to enable remediation chat. The "
                        "Acknowledge / Resolve buttons still work."
                    ),
                }
            ],
        }

    blocks: list[dict[str, Any]] = []
    if reply.get("text"):
        blocks.append({"type": "text", "text": reply["text"]})
    for tc in reply.get("tool_calls", []):
        blocks.append(
            {
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc.get("arguments", {}),
            }
        )
    # The turn loop continues while the model is still calling tools and
    # stops on a plain text turn — map finish accordingly.
    has_tools = bool(reply.get("tool_calls"))
    return {
        "stop_reason": "tool_use" if has_tools else "end_turn",
        "content": blocks,
    }


# When the model narrates a [system] fix without emitting its tool, we
# re-prompt with the tool forced — at most this many times, so a model that
# genuinely can't pick a tool can't spin the loop.
_MAX_FORCED_FIX_RETRIES = 2
# Bounded re-prompts when a proposed write has malformed arguments (e.g.
# patch_resource with an empty patch). After this many, the valid writes
# are queued and the invalid ones are dropped.
_MAX_INVALID_WRITE_RETRIES = 2
# Models often queue the write with a line of prose and no Root cause /
# Background / Remediation plan message (always so after a bounce), which
# leaves the tiles empty. Ask once for the write-up.
_SUMMARY_NUDGE = (
    "Your action(s) are now queued for operator approval: {actions}. Do not "
    "call any more tools. Post your final answer now, with exactly the three "
    "sections: ## Root cause, ## Background, ## Remediation plan."
)
# "## Remediation plan", or the bare / bold title on its own line, which the
# tiles also accept.
_REMEDIATION_HEADING_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?remediation plan\b", re.IGNORECASE | re.MULTILINE
)
_SIBLING_NOT_QUEUED = (
    "{name} was NOT queued: another action in this plan had invalid "
    "arguments. Re-propose the whole plan with every action corrected."
)

_REMEDIATION_NUDGE = (
    "You proposed a **[system]** remediation step but did not call its tool. "
    "Execute it now by calling the matching write tool (e.g. rollout_undo, "
    "scale_deployment, restart_deployment, delete_pod, patch_resource) with the "
    "exact arguments from your plan. Respond with the tool call, not prose."
)


def _proposes_system_step(text: str) -> bool:
    """True if the assistant narrated a ``[system]`` step — one it is meant to
    execute via a write tool. If it did so but emitted no tool call this turn,
    it described the fix instead of acting, and the caller re-prompts with the
    tool forced so the write becomes an approvable action."""
    return "[system]" in (text or "").lower()


# Automatic (policy ``auto``) remediations run as background tasks after the
# turn that proposed them is committed. Strong refs so they aren't GC'd.
_AUTO_TASKS: set[asyncio.Task] = set()


async def drain_auto_tasks() -> None:
    """Await every in-flight automatic remediation (tests, graceful shutdown)."""
    while _AUTO_TASKS:
        await asyncio.gather(*list(_AUTO_TASKS), return_exceptions=True)


async def _start_auto_actions(*, db: AsyncSession, tenant_id, alert: Alert,
                              action_ids: list[uuid.UUID]) -> None:
    """Open a workflow run for each policy-approved (``auto``) action and run
    it through the same execute-then-verify path a human approval uses."""
    for action_id in action_ids:
        action = await db.get(AlertAction, action_id)
        if action is None:
            continue
        run = WorkflowRun(
            tenant_id=tenant_id,
            workflow_name=f"Auto-remediate: {alert.title}",
            module="remediation",
            status=WorkflowRunStatus.running,
            started_at=datetime.now(tz=timezone.utc),
            alert_id=alert.id,
            input_payload={
                "tool": action.tool_name,
                "input": action.tool_input or {},
                "policy_rule": action.policy_rule,
                "auto": True,
            },
            steps=[],
        )
        db.add(run)
        await db.commit()
        task = asyncio.create_task(
            _run_approved_action(
                tenant_id=tenant_id, alert_id=alert.id,
                action_id=action.id, run_id=run.id,
            )
        )
        _AUTO_TASKS.add(task)
        task.add_done_callback(_AUTO_TASKS.discard)


async def _runbook_context(db: AsyncSession, *, tenant_id, alert: Alert, investigation) -> str:
    """Prompt block for the published runbooks matching this alert, and their
    ids recorded on the investigation. Failures never break triage."""
    try:
        from daalu_automation.core.aiops import runbooks as runbooks_mod

        async with db.begin_nested():
            matched = await runbooks_mod.match_for_alert(db, tenant_id, alert)
        if not matched:
            return ""
        ids = [str(rb.id) for rb in matched]
        known = list(investigation.runbook_ids or [])
        if any(i not in known for i in ids):
            investigation.runbook_ids = known + [i for i in ids if i not in known]
        return runbooks_mod.render_for_prompt(matched)
    except Exception as e:  # noqa: BLE001
        logger.warning("alert_chat.runbook_match_failed", error=str(e))
        return ""


async def _record_assistant_turn(
    *,
    db: AsyncSession,
    tenant_id,
    alert: Alert,
) -> None:
    """Run the agent loop (:func:`_assistant_turn_loop`), then start any
    writes the autonomy policy allowed to run automatically."""
    auto_ids: list[uuid.UUID] = []
    await _assistant_turn_loop(db=db, tenant_id=tenant_id, alert=alert, auto_ids=auto_ids)
    if auto_ids:
        await _start_auto_actions(db=db, tenant_id=tenant_id, alert=alert, action_ids=auto_ids)


async def _assistant_turn_loop(
    *,
    db: AsyncSession,
    tenant_id,
    alert: Alert,
    auto_ids: list[uuid.UUID],
) -> None:
    """Loop: ask the model, persist assistant blocks, auto-run read tools,
    persist tool results, ask again. Stops when the model returns
    ``end_turn`` or proposes a write tool (which waits for approval).

    The first turn (no tool results yet) and any turn that narrates a
    ``[system]`` fix without calling its tool are re-prompted with the tool
    forced (bounded by ``_MAX_FORCED_FIX_RETRIES``), so remediation reliably
    lands as an approvable action even on models that prefer prose.
    """
    forced_fix_retries = 0  # bounded re-prompts when the model narrates a
                            # [system] fix without emitting its tool call
    invalid_write_retries = 0  # bounded re-prompts for malformed write args
    summary_seen = False       # a three-section final answer was posted
    summary_nudge: str | None = None  # set when asking for that answer
    force_next = False      # force a tool call on the next iteration
    investigation = await inv_mod.ensure(db, tenant_id=tenant_id, alert_id=alert.id)
    runbook_context = await _runbook_context(
        db, tenant_id=tenant_id, alert=alert, investigation=investigation,
    )
    cluster = _cluster_for_alert(alert)
    severity = alert.severity.value if alert.severity is not None else ""
    for _ in range(12):  # hard cap to avoid runaway loops
        # Re-read the transcript fresh each iteration so any tool
        # results we just persisted are included.
        msgs, _ = await _load_chat(db, tenant_id, alert.id)
        # Force a tool call until the model has actually gathered evidence
        # (no tool result yet), and again whenever the previous turn narrated
        # a [system] fix without calling its tool.
        first_turn = not any(m.role is ChatRole.tool for m in msgs)
        force = first_turn or force_next
        nudge = _REMEDIATION_NUDGE if force_next else summary_nudge
        asked_for_summary = summary_nudge is not None
        force_next = False
        summary_nudge = None
        reply = await _ask_model(
            alert=alert, transcript=msgs, tenant_id=tenant_id,
            force_tool_use=force, nudge=nudge, runbook_context=runbook_context,
        )

        text_parts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        for block in reply["content"]:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_uses.append(
                    {
                        "id": block["id"],
                        "name": block["name"],
                        "input": block.get("input", {}),
                    }
                )

        if asked_for_summary:
            # The plan is already queued; this turn is only the write-up.
            # Drop any tool calls so nothing is queued or run twice.
            tool_uses = []

        # Validate every write before anything is queued: a malformed write
        # goes back to the model as a tool error instead of becoming an
        # approve card that can only fail. While retries remain, the whole
        # turn's writes are bounced so the plan is re-proposed as one piece
        # (queuing a lone delete_pod without its patch would be a worse plan).
        write_errors: dict[str, str] = {}
        write_ids: set[str] = set()
        decisions: dict[str, policy_mod.Decision] = {}
        auto_this_turn = 0  # auto decisions not yet inserted (hourly cap)
        for tc in tool_uses:
            if kube_tools.tool_requires_approval(tc["name"], tc["input"] or {}):
                write_ids.add(tc["id"])
                tc["input"], err = kube_tools.validate_write_input(
                    tc["name"], tc["input"] or {}
                )
                if err:
                    write_errors[tc["id"]] = err
                    continue
                # Autonomy policy (AOMM L3/L5): deny / approve / auto.
                decision = await policy_mod.decide_for_tenant(
                    db, tenant_id,
                    policy_mod.write_request_for(
                        tc["name"], tc["input"], cluster=cluster, severity=severity,
                    ),
                    pending_auto=auto_this_turn,
                )
                decisions[tc["id"]] = decision
                if decision.mode == "auto":
                    auto_this_turn += 1
                if decision.mode == "deny":
                    write_errors[tc["id"]] = (
                        f"denied by the autonomy policy ({decision.reason}). Do not "
                        "retry this action: propose a different remediation, or "
                        "tell the operator which manual steps to run."
                    )
                    await audit.record(
                        db, tenant_id, "action.denied", actor="agent",
                        subject=str(alert.id),
                        data={"tool": tc["name"], "input": tc["input"],
                              "decision": decision.as_dict()},
                    )
        bounce_writes = (
            bool(write_errors) and invalid_write_retries < _MAX_INVALID_WRITE_RETRIES
        )
        if write_errors:
            invalid_write_retries += 1
            logger.warning(
                "alert_chat.invalid_write_args",
                alert_id=str(alert.id),
                errors=list(write_errors.values()),
                bounced=bounce_writes,
            )

        narration = "\n".join(p for p in text_parts if p).strip()
        summary_seen = summary_seen or bool(_REMEDIATION_HEADING_RE.search(narration))
        inv_mod.note_answer(investigation, narration, model=str(reply.get("model") or ""))
        assistant_msg = AlertChatMessage(
            tenant_id=tenant_id,
            alert_id=alert.id,
            role=ChatRole.assistant,
            content=narration,
            tool_calls_json=tool_uses,
        )
        db.add(assistant_msg)
        await db.flush()  # populate id

        if not tool_uses:
            # Narrated a [system] fix without calling its tool — re-prompt
            # (bounded) with the tool forced so the write lands as an
            # approvable action instead of dead-ending as prose.
            if (
                not asked_for_summary
                and _proposes_system_step(narration)
                and forced_fix_retries < _MAX_FORCED_FIX_RETRIES
            ):
                forced_fix_retries += 1
                force_next = True
                await db.commit()
                continue
            await db.commit()
            return

        any_pending_write = False
        for tc in tool_uses:
            requires_approval = tc["id"] in write_ids
            if requires_approval and (bounce_writes or tc["id"] in write_errors):
                # Not queued: answer the call with the error so the model
                # sees it next iteration. No AlertAction, so no approve card.
                db.add(
                    AlertChatMessage(
                        tenant_id=tenant_id,
                        alert_id=alert.id,
                        role=ChatRole.tool,
                        content="error: " + write_errors.get(
                            tc["id"], _SIBLING_NOT_QUEUED.format(name=tc["name"])
                        ),
                        tool_call_id=tc["id"],
                    )
                )
                await db.flush()
                continue
            action = AlertAction(
                tenant_id=tenant_id,
                alert_id=alert.id,
                message_id=assistant_msg.id,
                tool_call_id=tc["id"],
                tool_name=tc["name"],
                tool_input=tc["input"],
                requires_approval=requires_approval,
                status=ActionStatus.pending,
            )
            if requires_approval:
                decision = decisions.get(tc["id"])
                if decision is not None:
                    action.policy_mode = decision.mode
                    action.policy_rule = decision.rule_id
                    action.policy_reason = decision.reason
                    action.approvals_required = decision.approvers
                action.approvals = []
                action.preview = await _preview_write(
                    tc["name"], tc["input"], tenant_id=tenant_id, cluster=cluster,
                )
                if decision is not None and decision.mode == "auto":
                    action.status = ActionStatus.approved
                    action.auto_executed = True
                    action.approved_at = datetime.now(tz=timezone.utc)
                    inv_mod.bump(investigation, "actions_auto")
                inv_mod.bump(investigation, "actions_proposed")
            db.add(action)
            await db.flush()

            if requires_approval:
                await audit.record(
                    db, tenant_id, "action.proposed", actor="agent",
                    subject=str(alert.id),
                    data={
                        "action_id": str(action.id), "tool": action.tool_name,
                        "input": action.tool_input, "policy_mode": action.policy_mode,
                        "policy_rule": action.policy_rule,
                        "approvals_required": action.approvals_required,
                    },
                )
                if action.auto_executed:
                    auto_ids.append(action.id)
                any_pending_write = True
                continue

            # Read tool — execute now and persist the tool_result.
            output = await kube_tools.execute_tool(
                tc["name"], tc["input"], tenant_id=tenant_id,
                cluster_name=_cluster_for_alert(alert),
            )
            action.status = ActionStatus.executed
            action.executed_at = datetime.now(tz=timezone.utc)
            action.result_output = output
            await audit.record(
                db, tenant_id, "tool.read", actor="agent", subject=str(alert.id),
                data={"tool": tc["name"], "input": tc["input"],
                      "error": output.startswith("error:")},
            )
            tool_msg = AlertChatMessage(
                tenant_id=tenant_id,
                alert_id=alert.id,
                role=ChatRole.tool,
                content=output,
                tool_call_id=tc["id"],
            )
            db.add(tool_msg)
            await db.flush()

        await db.commit()

        if bounce_writes:
            continue  # let the model correct the plan
        if any_pending_write and not summary_seen:
            queued = ", ".join(
                tc["name"] for tc in tool_uses if tc["id"] in write_ids
            )
            summary_nudge = _SUMMARY_NUDGE.format(actions=queued)
            continue
        if any_pending_write or reply.get("stop_reason") == "end_turn":
            return
    logger.warning("alert_chat.loop_cap_hit", alert_id=str(alert.id))


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("/{alert_id}/chat", response_model=list[AlertChatMessageOut])
async def get_chat(
    alert_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    alert = await _get_alert(db, alert_id, tenant_id)
    msgs, actions_by_msg = await _load_chat(db, tenant_id, alert.id)
    return _serialize(msgs, actions_by_msg)


@router.post("/{alert_id}/chat", response_model=list[AlertChatMessageOut])
async def post_chat(
    alert_id: str,
    req: AlertChatPostRequest,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user: User = Depends(current_user),
):
    alert = await _get_alert(db, alert_id, tenant_id)
    db.add(
        AlertChatMessage(
            tenant_id=tenant_id,
            alert_id=alert.id,
            role=ChatRole.user,
            content=req.content,
        )
    )
    await db.commit()
    await _record_assistant_turn(db=db, tenant_id=tenant_id, alert=alert)
    msgs, actions_by_msg = await _load_chat(db, tenant_id, alert.id)
    return _serialize(msgs, actions_by_msg)


@router.post("/{alert_id}/triage", response_model=list[AlertChatMessageOut])
async def trigger_triage(
    alert_id: str,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user: User = Depends(current_user),
):
    """Kick off an autonomous triage pass.

    Default behavior is idempotent: if the alert already has any
    assistant message, this is a no-op and we just return the current
    transcript. That keeps the detail page's auto-trigger from
    re-spending a model turn every time you reload the tab.

    Pass ``?force=true`` to always append a fresh kickoff message and
    run a new pass — this is what the "Re-triage" button uses when the
    operator explicitly wants a do-over.
    """
    alert = await _get_alert(db, alert_id, tenant_id)

    should_kickoff = force
    if not should_kickoff:
        existing = (
            await db.execute(
                select(AlertChatMessage)
                .where(
                    AlertChatMessage.tenant_id == tenant_id,
                    AlertChatMessage.alert_id == alert.id,
                    AlertChatMessage.role == ChatRole.assistant,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        should_kickoff = existing is None

    if should_kickoff:
        db.add(
            AlertChatMessage(
                tenant_id=tenant_id,
                alert_id=alert.id,
                role=ChatRole.user,
                content=TRIAGE_KICKOFF,
            )
        )
        await db.commit()
        await _record_assistant_turn(db=db, tenant_id=tenant_id, alert=alert)

    msgs, actions_by_msg = await _load_chat(db, tenant_id, alert.id)
    return _serialize(msgs, actions_by_msg)


async def auto_investigate(tenant_id, alert_id) -> None:
    """Start a triage pass without HTTP — what ``trigger_triage`` does, for
    the agent host when the autonomy policy sets
    ``investigate_automatically: true``. Idempotent: a no-op when the alert
    already has an assistant message. Never raises."""
    try:
        tid = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
        async with AsyncSessionLocal() as db:
            alert = await _get_alert(db, str(alert_id), tid)
            existing = (
                await db.execute(
                    select(AlertChatMessage.id)
                    .where(
                        AlertChatMessage.tenant_id == tid,
                        AlertChatMessage.alert_id == alert.id,
                        AlertChatMessage.role == ChatRole.assistant,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return
            db.add(
                AlertChatMessage(
                    tenant_id=tid,
                    alert_id=alert.id,
                    role=ChatRole.user,
                    content=TRIAGE_KICKOFF,
                )
            )
            await db.commit()
            await _record_assistant_turn(db=db, tenant_id=tid, alert=alert)
    except Exception as e:  # noqa: BLE001
        logger.warning("alert_chat.auto_investigate_failed", alert_id=str(alert_id), error=str(e))


@router.post(
    "/{alert_id}/actions/{action_id}/approve",
    response_model=ApproveActionOut,
)
async def approve_action(
    alert_id: str,
    action_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    """Approve a pending write and start its remediation workflow run.

    Returns as soon as the run exists, with its id, so the UI can open the
    run page straight away. The write and its verification (which waits for
    the rollout, often a minute or more) run in the background and fill in
    the run's steps as they go; the run page polls while it's running.
    """
    alert = await _get_alert(db, alert_id, tenant_id)
    action = (
        await db.execute(
            select(AlertAction)
            .where(
                AlertAction.id == action_id,
                AlertAction.alert_id == alert.id,
                AlertAction.tenant_id == tenant_id,
            )
            # Row lock: two people approving at once must not lose a vote or
            # start the action twice.
            .with_for_update()
        )
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(404, "action not found")
    if action.status is not ActionStatus.pending:
        raise HTTPException(409, f"action is {action.status.value}, not pending")
    if action.actor == f"mcp:{user.email}":
        raise HTTPException(
            403, "you proposed this action through MCP; someone else must approve it"
        )

    # Multi-approver policy: record this user's vote; run only once enough
    # distinct people have approved.
    approvals = list(action.approvals or [])
    if any(a.get("user_id") == str(user.id) for a in approvals):
        raise HTTPException(409, "you have already approved this action")
    now = datetime.now(tz=timezone.utc)
    approvals.append({"user_id": str(user.id), "email": user.email, "at": now.isoformat()})
    action.approvals = approvals
    required = max(1, int(action.approvals_required or 1))
    if len(approvals) < required:
        await audit.record(
            db, tenant_id, "action.approval_recorded", actor=user.email,
            subject=str(alert.id),
            data={"action_id": str(action.id), "tool": action.tool_name,
                  "approvals": len(approvals), "required": required},
        )
        await db.commit()
        msgs, actions_by_msg = await _load_chat(db, tenant_id, alert.id)
        return ApproveActionOut(
            messages=_serialize(msgs, actions_by_msg),
            workflow_run_id="",
            approvals_remaining=required - len(approvals),
        )

    action.status = ActionStatus.approved
    action.approved_by = user.id
    action.approved_at = now
    investigation = await inv_mod.ensure(db, tenant_id=tenant_id, alert_id=alert.id)
    inv_mod.bump(investigation, "actions_approved")
    await audit.record(
        db, tenant_id, "action.approved", actor=user.email, subject=str(alert.id),
        data={"action_id": str(action.id), "tool": action.tool_name,
              "input": action.tool_input,
              "approvers": [a.get("email") for a in approvals]},
    )

    # Record this remediation as a Workflow run so the operator can open it and
    # watch the plan execute step by step, and so it shows on the Workflows
    # page linked back to this alert.
    run = WorkflowRun(
        tenant_id=tenant_id,
        workflow_name=f"Remediate: {alert.title}",
        module="remediation",
        status=WorkflowRunStatus.running,
        started_at=datetime.now(tz=timezone.utc),
        alert_id=alert.id,
        input_payload={
            "tool": action.tool_name,
            "input": action.tool_input or {},
        },
        steps=[],
    )
    db.add(run)
    await db.commit()
    background_tasks.add_task(
        _run_approved_action,
        tenant_id=tenant_id,
        alert_id=alert.id,
        action_id=action.id,
        run_id=run.id,
    )

    msgs, actions_by_msg = await _load_chat(db, tenant_id, alert.id)
    return ApproveActionOut(
        messages=_serialize(msgs, actions_by_msg),
        workflow_run_id=str(run.id),
    )


async def _record_outcome(
    db: AsyncSession, *, tenant_id, alert: Alert, action: AlertAction,
    output: str, is_error: bool, resolved: bool,
) -> None:
    """Learning loop (AOMM L4) + guardrails (L5) after a write is verified:
    store the outcome on the action and investigation, write the audit trail,
    trip the circuit breaker when an automatic action didn't fix things, and
    draft a runbook from a verified fix."""
    action.verified_resolved = resolved
    investigation = await inv_mod.ensure(db, tenant_id=tenant_id, alert_id=alert.id)
    inv_mod.note_outcome(investigation, resolved=resolved, auto=action.auto_executed)
    await audit.record(
        db, tenant_id,
        "action.auto_executed" if action.auto_executed else "action.executed",
        actor=f"policy:{action.policy_rule or 'defaults'}" if action.auto_executed else "executor",
        subject=str(alert.id),
        data={"action_id": str(action.id), "tool": action.tool_name,
              "input": action.tool_input, "error": is_error,
              "output": (output or "")[:2000]},
    )
    await audit.record(
        db, tenant_id, "action.verified", actor="verifier", subject=str(alert.id),
        data={"action_id": str(action.id), "resolved": resolved},
    )
    if action.auto_executed and not resolved:
        until = await policy_mod.trip_breaker(db, tenant_id, action.policy_rule)
        if until is not None:
            await audit.record(
                db, tenant_id, "policy.breaker_tripped", actor="verifier",
                subject=action.policy_rule or "defaults",
                data={"action_id": str(action.id), "until": until.isoformat()},
            )
    await db.commit()
    if resolved:
        try:
            from daalu_automation.core.aiops import runbooks as runbooks_mod

            await runbooks_mod.maybe_draft_from_investigation(
                db, tenant_id=tenant_id, alert=alert, investigation=investigation,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("alert_chat.runbook_draft_failed", error=str(e))


async def _run_approved_action(
    *, tenant_id, alert_id: uuid.UUID, action_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    """Execute an approved write and verify it, recording each step on the run.

    Runs after the approve request has returned (FastAPI background task), in
    its own session. Any crash marks the run failed rather than leaving it
    "running" forever.
    """
    async with AsyncSessionLocal() as db:
        alert = await db.get(Alert, alert_id)
        action = await db.get(AlertAction, action_id)
        run = await db.get(WorkflowRun, run_id)
        if alert is None or action is None or run is None:
            logger.warning("alert_chat.approved_action_missing", run_id=str(run_id))
            return
        cluster = _cluster_for_alert(alert)
        try:
            # Step 1 — the approved write itself, run EXACTLY as approved (binding).
            output = await kube_tools.execute_tool(
                action.tool_name, action.tool_input, tenant_id=tenant_id,
                cluster_name=cluster,
            )
            is_error = output.startswith("error:")
            action.status = ActionStatus.failed if is_error else ActionStatus.executed
            action.executed_at = datetime.now(tz=timezone.utc)
            if is_error:
                action.result_error = output
            else:
                action.result_output = output

            db.add(
                AlertChatMessage(
                    tenant_id=tenant_id,
                    alert_id=alert.id,
                    role=ChatRole.tool,
                    content=output,
                    tool_call_id=action.tool_call_id,
                )
            )

            steps: list[dict[str, Any]] = [
                {
                    "order": 1,
                    "kind": "action",
                    "title": f"Run {action.tool_name}",
                    "tool": action.tool_name,
                    "input": action.tool_input or {},
                    "output": output,
                    "status": "error" if is_error else "ok",
                }
            ]
            # Save as we go: the run page polls and shows each step as it lands.
            run.steps = list(steps)
            await db.commit()

            # Steps 2..N — verification reads that confirm the workload recovered.
            # The deployment check waits for the rollout to converge first, so we judge
            # the settled state rather than a mid-rollout snapshot.
            order = 2
            for tool, tinput, title in _verification_steps(action):
                if tool == "get_deployment":
                    vout = await _wait_for_deployment(
                        namespace=tinput["namespace"], name=tinput["name"],
                        tenant_id=tenant_id, cluster=cluster,
                    )
                else:
                    vout = await kube_tools.execute_tool(
                        tool, tinput, tenant_id=tenant_id, cluster_name=cluster
                    )
                verr = vout.startswith("error:")
                steps.append({
                    "order": order, "kind": "verify", "title": title,
                    "tool": tool, "input": tinput, "output": vout,
                    "status": "error" if verr else "ok",
                })
                run.steps = list(steps)
                await db.commit()
                order += 1

            # The approved action ran exactly as approved (binding); now judge from the
            # verification whether it actually fixed the problem.
            resolved = _remediation_resolved(is_error, steps)
            await _record_outcome(db, tenant_id=tenant_id, alert=alert, action=action,
                                  output=output, is_error=is_error, resolved=resolved)
            run.steps = steps
            run.status = (
                WorkflowRunStatus.succeeded if resolved else WorkflowRunStatus.failed
            )
            run.finished_at = datetime.now(tz=timezone.utc)
            run.output_payload = {
                "primary_status": "error" if is_error else "ok",
                "resolved": resolved,
            }
            await db.commit()

            # Binding contract: the workflow executed EXACTLY the approved tool call
            # (verbatim) plus deterministic verification of that same resource. The
            # agent never auto-ran anything else.
            #
            # If verification shows it worked, we stop here. If it did NOT resolve the
            # issue, hand back to the agent to investigate again and propose the next
            # step — which is only ever a *proposal*: a new remediation it suggests
            # becomes another pending approval (nothing runs without the operator), or
            # it tells the operator the manual steps to run. So re-engaging never
            # breaks the "nothing executes without approval" guarantee.
            if not resolved:
                db.add(
                    AlertChatMessage(
                        tenant_id=tenant_id,
                        alert_id=alert.id,
                        role=ChatRole.user,
                        content=(
                            "The approved remediation executed but verification shows "
                            "the issue is NOT resolved. Investigate what's still wrong, "
                            "then either propose another remediation action (it goes "
                            "through the autonomy policy, so it may need my approval) or, if it can't be "
                            "done with your tools, tell me exactly which manual steps "
                            "to run."
                        ),
                    )
                )
                await db.commit()
                await _record_assistant_turn(db=db, tenant_id=tenant_id, alert=alert)

        except Exception as e:  # noqa: BLE001
            logger.exception("alert_chat.approved_action_failed", run_id=str(run_id))
            await db.rollback()
            run = await db.get(WorkflowRun, run_id)
            action = await db.get(AlertAction, action_id)
            if run is not None and run.status is WorkflowRunStatus.running:
                run.status = WorkflowRunStatus.failed
                run.finished_at = datetime.now(tz=timezone.utc)
                run.output_payload = {"error": f"{type(e).__name__}: {e}"}
            if action is not None and action.status is ActionStatus.approved:
                action.status = ActionStatus.failed
                action.result_error = f"error: {type(e).__name__}: {e}"
            await db.commit()
            # A crash is a failed outcome: it must count against trust and trip
            # the breaker like any other failed automatic fix.
            if action is not None and action.verified_resolved is None:
                try:
                    alert = await db.get(Alert, alert_id)
                    if alert is not None:
                        await _record_outcome(
                            db, tenant_id=tenant_id, alert=alert, action=action,
                            output=action.result_error, is_error=True, resolved=False,
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("alert_chat.crash_outcome_failed", run_id=str(run_id))


@router.post(
    "/{alert_id}/actions/{action_id}/reject",
    response_model=list[AlertChatMessageOut],
)
async def reject_action(
    alert_id: str,
    action_id: str,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user: User = Depends(current_user),
):
    alert = await _get_alert(db, alert_id, tenant_id)
    action = (
        await db.execute(
            select(AlertAction).where(
                AlertAction.id == action_id,
                AlertAction.alert_id == alert.id,
                AlertAction.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(404, "action not found")
    if action.status is not ActionStatus.pending:
        raise HTTPException(409, f"action is {action.status.value}, not pending")

    action.status = ActionStatus.rejected
    action.approved_by = user.id
    action.approved_at = datetime.now(tz=timezone.utc)
    investigation = await inv_mod.ensure(db, tenant_id=tenant_id, alert_id=alert.id)
    inv_mod.bump(investigation, "actions_rejected")
    await audit.record(
        db, tenant_id, "action.rejected", actor=user.email, subject=str(alert.id),
        data={"action_id": str(action.id), "tool": action.tool_name,
              "input": action.tool_input},
    )
    rejection_note = (
        f"rejected by {user.email}: tool {action.tool_name} not executed"
    )
    db.add(
        AlertChatMessage(
            tenant_id=tenant_id,
            alert_id=alert.id,
            role=ChatRole.tool,
            content=rejection_note,
            tool_call_id=action.tool_call_id,
        )
    )
    await db.commit()
    await _record_assistant_turn(db=db, tenant_id=tenant_id, alert=alert)
    msgs, actions_by_msg = await _load_chat(db, tenant_id, alert.id)
    return _serialize(msgs, actions_by_msg)

"""AI command bar / copilot endpoint.

Powers the persistent ChatGPT-style assistant in the right panel and
the CMD+K command bar at the top of every page.

**Tool-use.** The copilot answers questions about the tenant's real
infrastructure by calling read tools — the device inventory in the
source of truth, the observed LLDP topology, pods and metrics — rather
than reasoning over a prose summary. Before this, its whole world was a
24-hour window of events and alerts, so a perfectly reasonable question
like "how many switches do I have?" had no grounding at all and the
model either hedged or invented. Onboarding a datacenter now actually
changes what the copilot knows.

**Read-only, by allowlist.** ``_COPILOT_TOOLS`` names the tools this
surface may call, and everything else in the registry is unreachable
from here. The command bar has no approval UI — unlike the alert chat,
which renders gated write actions as cards an operator confirms — so a
mutating tool invoked from here would execute with nothing in the way.
The allowlist is deliberately a name list rather than a
``requires_approval`` filter: a future read tool is inert until someone
adds it here, which is the failure direction we want.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_tenant_id
from daalu_automation.api.schemas import CommandRequest, CommandResponse
from daalu_automation.core import kube_tools
from daalu_automation.core.llm import LLMUnavailable, chat_with_tools
from daalu_automation.database import get_db
from daalu_automation.models import Alert, ClusterTunnel, Event, Integration

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/copilot", tags=["copilot"])


# Read-only tools the command bar may call. See the module docstring for
# why this is an explicit allowlist and not a computed filter.
_COPILOT_TOOLS = frozenset(
    {
        # Source of truth — the inventory a user just onboarded.
        "list_devices",
        "get_device",
        "get_fabric_topology",
        # Kubernetes reads.
        "list_pods",
        "describe_pod",
        "get_pod_events",
        "get_pod_logs",
        "get_deployment",
        # Observability reads.
        "query_prometheus",
        "query_loki",
    }
)

# Tools that read one cluster. Each gets a ``cluster`` argument (an onboarded
# cluster's slug) so the model can't answer about one cluster with another's
# data. Source-of-truth tools are fabric-wide and take none.
_CLUSTER_TOOLS = frozenset(
    {
        "list_pods",
        "describe_pod",
        "get_pod_events",
        "get_pod_logs",
        "get_deployment",
        "query_prometheus",
        "query_loki",
    }
)

# How many times the model may call tools before we make it answer. Each
# round trip is a full LLM call, so this bounds both latency and spend.
# Three is enough for the common shape (list_devices -> get_device ->
# answer) without letting a confused model loop.
_MAX_TOOL_ROUNDS = 3


SYSTEM = """\
You are the operational copilot for Daalu Automation — an AI Operations
Command Center. Users ask short questions about their company's live
operational state and infrastructure.

You have tools that read the user's REAL infrastructure. Use them:

- `list_devices` — the device inventory in the source of truth. Call this
  for any question about what infrastructure exists, how many devices
  there are, or what a device's IP or platform is.
- `get_device` — full detail for one device, by name (e.g. "leaf1").
- `get_fabric_topology` — the cabling graph observed from live LLDP;
  which port connects to which. Use for "what is X connected to".
- `list_pods`, `describe_pod`, `get_pod_events`, `get_pod_logs`,
  `get_deployment` — Kubernetes state.
- `query_prometheus`, `query_loki` — metrics and logs.

Clusters: the user may have several Kubernetes clusters (listed under
CLUSTERS below). Every Kubernetes, metrics or logs tool takes a `cluster`
argument — always set it to one of those slugs.
- If the user names a cluster, use that one.
- Otherwise use the cluster they are VIEWING, when one is given.
- Otherwise, if there is only one cluster, use it; if there are several,
  ask which one (or answer for each, labelled by cluster).
- Always say which cluster an answer is about. Never present one
  cluster's data as another's. If a cluster has no metrics or logs store,
  say so rather than using another cluster's.

Rules:
- NEVER answer a factual question about the user's infrastructure from
  memory or assumption. Call a tool and answer from what it returns.
- If a tool returns an error or empty result, say exactly that. Do not
  paper over it, and do not substitute a plausible-sounding answer.
- Quote real names, IPs and counts from tool output.
- Be concise. These answers render in a small side panel.
- If the source of truth is empty, say so and mention that devices appear
  after an inventory import — do not invent an inventory.
"""


def _tool_definitions() -> list[dict[str, Any]]:
    """OpenAI-shaped definitions for the allowlisted tools only, with a
    ``cluster`` argument added to the per-cluster ones."""
    import copy

    out = []
    for d in kube_tools.openai_tool_definitions():
        name = d.get("function", {}).get("name")
        if name not in _COPILOT_TOOLS:
            continue
        if name in _CLUSTER_TOOLS:
            d = copy.deepcopy(d)
            params = d["function"].setdefault("parameters", {"type": "object"})
            params.setdefault("properties", {})["cluster"] = {
                "type": "string",
                "description": "Slug of the cluster to read (see CLUSTERS).",
            }
        out.append(d)
    return out


class _NoCluster:
    """Why a per-cluster tool call was refused; the text goes to the model."""

    def __init__(self, message: str) -> None:
        self.message = message


def _pick_cluster(
    asked: str | None,
    slugs: list[str],
    mentioned: list[str],
    default: str | None,
) -> str | None | _NoCluster:
    """Which cluster a per-cluster tool call reads.

    Precedence: a cluster the user named in the question, then the cluster
    being viewed (or the only one). The model's own ``cluster`` argument is
    honoured only when the user named it. Several clusters, none named and
    none viewed: refuse, so the model asks instead of guessing.
    """
    if not slugs:
        return None  # legacy: no onboarded clusters, tenant default kubeconfig
    if asked and asked not in slugs:
        return _NoCluster(
            f"error: no cluster named {asked!r}; onboarded clusters: {', '.join(slugs)}."
        )
    if asked and asked in mentioned:
        return asked
    if default:
        return default
    if len(mentioned) == 1:
        return mentioned[0]
    return _NoCluster(
        "error: the user has several clusters "
        f"({', '.join(slugs)}) and didn't say which one. Ask them which "
        "cluster they mean; don't pick one."
    )


async def _clusters_context(
    db: AsyncSession, tenant_id: Any
) -> tuple[list[str], list[str]]:
    """(slugs, prompt lines) for the tenant's onboarded clusters, with which
    stores each one has, so the model can pick a target and say when a
    cluster has no metrics or logs."""
    tunnels = (
        await db.execute(
            select(ClusterTunnel)
            .where(ClusterTunnel.tenant_id == tenant_id)
            .order_by(ClusterTunnel.created_at)
        )
    ).scalars().all()
    linked = (
        await db.execute(
            select(Integration.cluster_tunnel_id, Integration.provider).where(
                Integration.tenant_id == tenant_id,
                Integration.cluster_tunnel_id.is_not(None),
            )
        )
    ).all()
    by_tunnel: dict[Any, set[str]] = {}
    for tid, provider in linked:
        by_tunnel.setdefault(tid, set()).add(provider)
    lines = []
    for t in tunnels:
        have = by_tunnel.get(t.id, set())
        metrics = "yes" if have & {"prometheus", "thanos"} else "none"
        logs = "yes" if have & {"loki", "opensearch"} else "none"
        status = getattr(t.status, "value", t.status)
        lines.append(
            f"- {t.slug} ({t.name}; {status}) — metrics: {metrics}, logs: {logs}"
        )
    return [t.slug for t in tunnels], lines


@router.post("/ask", response_model=CommandResponse)
async def ask(
    req: CommandRequest,
    db: AsyncSession = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
):
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    recent_events = (
        (
            await db.execute(
                select(Event)
                .where(Event.tenant_id == tenant_id, Event.occurred_at >= cutoff)
                .order_by(desc(Event.occurred_at))
                .limit(40)
            )
        )
        .scalars()
        .all()
    )
    open_alerts = (
        (
            await db.execute(
                select(Alert)
                .where(Alert.tenant_id == tenant_id, Alert.status == "open")
                .order_by(desc(Alert.created_at))
                .limit(20)
            )
        )
        .scalars()
        .all()
    )

    # Events and alerts stay in the prompt rather than behind a tool: they
    # are small, bounded, and relevant to most questions, so paying for a
    # round trip to fetch them would only add latency.
    slugs, cluster_lines = await _clusters_context(db, tenant_id)
    requested = getattr(req, "cluster", None)
    viewing = requested if requested in slugs else None
    default_cluster = viewing or (slugs[0] if len(slugs) == 1 else None)
    # The model doesn't get to pick a cluster the user never mentioned:
    # observed live, it answered "what pods are running on this cluster?"
    # from daalu-prod while the user was on daalu-demo's page.
    query_lc = req.query.lower()
    mentioned = [s for s in slugs if s.lower() in query_lc]

    context = "\n".join(
        [
            (
                f"VIEWING: {viewing} — \"this cluster\" means {viewing}"
                if viewing
                else "VIEWING: (no cluster page open)"
            ),
            "CLUSTERS:",
            *(cluster_lines or ["- (none onboarded)"]),
            "",
            "RECENT EVENTS:",
            *(
                f"- [{e.module}/{e.severity.value}] {e.occurred_at:%H:%M} {e.summary}"
                for e in recent_events
            ),
            "",
            "OPEN ALERTS:",
            *(f"- [{a.module}/{a.severity.value}] {a.title}" for a in open_alerts),
        ]
    )

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": f"Operational state:\n{context}\n\nUser question: {req.query}",
        }
    ]

    answer = ""
    tools = _tool_definitions()
    try:
        for _ in range(_MAX_TOOL_ROUNDS):
            reply = await chat_with_tools(
                system=SYSTEM,
                messages=messages,
                tools=tools,
                tenant_id=tenant_id,
                source="copilot.ask",
                max_tokens=1024,
            )
            calls = reply.get("tool_calls") or []
            answer = (reply.get("text") or "").strip()
            if not calls:
                break

            # Echo the assistant turn back with its tool calls, then one
            # `tool` message per call — the shape the chat-completions API
            # requires for the next turn to be well-formed.
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.get("text") or "",
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": json.dumps(c.get("arguments", {})),
                            },
                        }
                        for c in calls
                    ],
                }
            )
            for c in calls:
                name = c["name"]
                if name not in _COPILOT_TOOLS:
                    # Belt and braces: the model was never offered this, so
                    # reaching here means a malformed or hallucinated call.
                    result = f"error: tool {name!r} is not available from the copilot"
                    logger.warning("copilot.tool_not_allowed tool=%s", name)
                else:
                    args = dict(c.get("arguments", {}) or {})
                    cluster = None
                    if name in _CLUSTER_TOOLS:
                        cluster = _pick_cluster(
                            args.pop("cluster", None), slugs, mentioned, default_cluster
                        )
                    if isinstance(cluster, _NoCluster):
                        result = cluster.message
                    else:
                        result = await kube_tools.execute_tool(
                            name,
                            args,
                            tenant_id=tenant_id,
                            cluster_slug=cluster,
                        )
                        if cluster:
                            result = f"[cluster: {cluster}]\n{result}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "content": result,
                    }
                )
        else:
            # Ran out of rounds with the model still calling tools. Ask once
            # more with no tools so the user gets prose instead of silence.
            reply = await chat_with_tools(
                system=SYSTEM,
                messages=[
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "Answer now from what the tools returned. "
                            "Do not call any more tools."
                        ),
                    },
                ],
                tools=[],
                tenant_id=tenant_id,
                source="copilot.ask.final",
                max_tokens=1024,
            )
            answer = (reply.get("text") or "").strip()
    except LLMUnavailable as e:
        logger.warning("copilot.llm_unavailable error=%s", e)
        answer = (
            "AI copilot is offline — no tool-capable LLM tier is configured. "
            "Set LLM_API_KEY/LLM_BASE_URL (DeepSeek/OpenAI-compatible) or a "
            "local vLLM endpoint. Note this path does not use Anthropic: "
            "tool-calling runs on the OpenAI-compatible tiers only. In the "
            "meantime, the devices, events and alerts pages show the same "
            "underlying data."
        )

    if not answer:
        answer = "No answer was produced. Try rephrasing the question."

    return CommandResponse(
        answer=answer,
        references=[
            {"kind": "event", "id": str(e.id), "summary": e.summary}
            for e in recent_events[:5]
        ],
    )

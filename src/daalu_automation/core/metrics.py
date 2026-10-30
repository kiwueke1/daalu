"""Prometheus metrics for the Daalu Automation backend.

Three families:

* **LLM router** — every dispatch attempt records the requested tier,
  the tier that actually served it, success/failure, latency, token
  counts, and the *fallback* counter (when the first attempted tier
  failed and the router fell through to the next).
* **Event bus / Celery** — queue depth, task duration, agent run
  outcomes.
* **Process / runtime** — exposed by the default
  ``prometheus_client.process_collector`` + the FastAPI instrumentator;
  no code in this module.

The collectors live on a package-level registry, so importing this
module is enough to register them. The FastAPI app exposes
``/metrics`` via ``prometheus-fastapi-instrumentator``; Celery
workers expose theirs on ``:9100/metrics`` via
``prometheus_client.start_http_server`` (see ``workers/celery_app.py``).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ── LLM router ────────────────────────────────────────────────────────────
# These names follow the Prometheus convention: snake_case, suffix with
# the unit. Histograms get _seconds / _tokens; counters get _total.

LLM_REQUESTS_TOTAL = Counter(
    "daalu_llm_requests_total",
    "Total LLM completion requests, by *requested* tier and outcome.",
    ["requested_tier", "served_by", "outcome", "model"],
)

LLM_FALLBACKS_TOTAL = Counter(
    "daalu_llm_fallbacks_total",
    "Times the router fell through to a lower-preference tier "
    "(local → external_classifier → external_quality, or similar). "
    "The label is the tier that *failed*, not the one that took over.",
    ["failed_tier", "reason"],
)

LLM_TOKENS_TOTAL = Counter(
    "daalu_llm_tokens_total",
    "Token counts the upstream returned, broken down by direction "
    "(prompt vs completion) and the tier that served the call.",
    ["served_by", "direction", "model"],
)

LLM_COST_USD_TOTAL = Counter(
    "daalu_llm_cost_usd_total",
    "Dollar cost of LLM calls, summed (Prometheus counter — use "
    "rate() for $/sec). Tags match the usage_events row.",
    ["tenant_id", "served_by", "model"],
)

LLM_LATENCY_SECONDS = Histogram(
    "daalu_llm_latency_seconds",
    "End-to-end latency of a successful LLM call, including any "
    "fallback retries inside the router.",
    ["served_by", "model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)

LLM_LOCAL_HEALTHY = Gauge(
    "daalu_llm_local_healthy",
    "Whether the local NIM passed its last health probe. 1 = healthy, "
    "0 = unreachable / 5xx. Updated by core/llm._local_is_healthy.",
)

# ── Event bus / agents / workflows ────────────────────────────────────────

EVENTS_PUBLISHED_TOTAL = Counter(
    "daalu_events_published_total",
    "Events published onto the Redis stream, by module + type.",
    ["module", "type", "severity"],
)

AGENT_RUNS_TOTAL = Counter(
    "daalu_agent_runs_total",
    "Agent invocations, by name + outcome.",
    ["agent", "module", "outcome"],
)

AGENT_RUN_DURATION_SECONDS = Histogram(
    "daalu_agent_run_duration_seconds",
    "Wall time per agent invocation.",
    ["agent", "module"],
    buckets=(0.1, 0.5, 1, 5, 15, 30, 60, 180, 600),
)

WORKFLOW_RUNS_TOTAL = Counter(
    "daalu_workflow_runs_total",
    "Workflow invocations, by name + outcome.",
    ["workflow", "module", "outcome"],
)

# ── Briefings ─────────────────────────────────────────────────────────────

BRIEFING_GENERATED_TOTAL = Counter(
    "daalu_briefings_generated_total",
    "Successful briefing renders, by channel.",
    ["channel"],
)

BRIEFING_GENERATION_SECONDS = Histogram(
    "daalu_briefing_generation_seconds",
    "Time to render one briefing, end to end.",
    ["channel"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600),
)

# ── SoT / executor ────────────────────────────────────────────────────────

CHANGE_PROPOSAL_TRANSITIONS_TOTAL = Counter(
    "daalu_change_proposal_transitions_total",
    "ChangeProposal status transitions (pending → approved → executed/failed). "
    "Drives the SRE view of the device-push pipeline.",
    ["from_status", "to_status", "kind"],
)

EXECUTOR_PUSHES_TOTAL = Counter(
    "daalu_executor_pushes_total",
    "Device config pushes executed, by transport + outcome.",
    ["transport", "outcome"],
)


def export_all_module_collectors() -> None:
    """No-op import hook — call from main.py to ensure this module is loaded."""
    return

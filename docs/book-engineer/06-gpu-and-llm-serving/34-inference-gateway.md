# 34. The inference-gateway service

The inference-gateway is the choke point through which all
daalu-hosted-tier LLM calls flow. It enforces tenant attribution,
per-tenant quotas, rate limits, and metering — all the things vLLM
deliberately doesn't do because vLLM is a single-tenant
runtime.

---

## Where it lives

* Code: ``src/daalu_automation/inference_gateway/``
  * ``app.py`` — FastAPI routes
  * ``quota.py`` — quota + rate-limit logic
  * ``upstream.py`` — vLLM HTTP client
  * ``metering.py`` — ``usage_events`` writer
* Helm chart: ``deploy/helm/inference-gateway/``
* CLI entrypoint: ``daalu inference-gateway``
* K8s Service name (in the hub): ``inference-gateway`` on port 80
  → container port 8080.

---

## The hot path

Step-by-step, what happens for one `POST /v1/chat/completions`:

```
   daalu-api                                    inference-gateway
   ─────────                                    ─────────────────
   complete(purpose='coding', tenant=X)         /v1/chat/completions
   │                                            │
   │  mint_service_token(X, 'coding', 60s)      │  verify_service_token() → claims
   │  POST /v1/chat/completions                 │
   │     bearer: <jwt>                          │
   │     body: { model, messages, max_tokens } ─┤
   │                                            │  load_quota(db, X)
   │                                            │     └─ DaaluHostedQuota row
   │                                            │
   │                                            │  check_monthly_quota(state)
   │                                            │  check_rate_limit(redis, state, 'coding')
   │                                            │     └─ Redis: INCR daalu-hosted:rl:rpm:X:<minute>
   │                                            │
   │                                            │  chat_completion(body)
   │                                            │     └─ POST daalu_hosted_upstream_url
   │                                            │            (vLLM, OpenAI-compatible)
   │                                            │
   │                                            │  record_usage(state, total_tokens=N)
   │                                            │     ├─ UPDATE daalu_hosted_quotas
   │                                            │     │        SET current_period_used += N
   │                                            │     └─ Redis: INCRBY daalu-hosted:rl:tpm:X:...
   │                                            │
   │                                            │  write_usage_event(...)
   │                                            │     └─ INSERT usage_events (tier=local, ...)
   │                                            │
   │  ← 200 OK { choices, usage, model }       ─┤
```

Pre-call work: token verification + quota load + rate-limit check.
Upstream call.
Post-call work: counters + ``usage_events`` row.

The post-call work is **best-effort** — a DB blip after the upstream
call must not break the response. The gateway returns 200 to the
caller even if accounting fails; a reconciliation task
(``workers.tasks.reconcile_quotas`` — TODO) catches drift between
``usage_events`` rows and ``current_period_used``.

---

## Why the auth flow uses a separate signing key

The service-token system in ``core/service_tokens.py`` uses
``settings.service_token_secret_key``, *not* ``settings.secret_key``.

If a user's session JWT were accepted by the gateway, an attacker
who stole a session cookie could call the gateway directly with the
victim's tenant and impersonate them. Separate keys + a distinct
audience claim (``aud="daalu-internal"``) mean a forwarded user
token fails verification.

Both keys must rotate on the same cadence in prod; if either is
empty the dependent service refuses to mint/verify and the
deployment fails fast rather than failing-open.

---

## The two enforcement layers

| Limit                | Where stored                | Window     | Purpose          |
|---------------------|----------------------------|-----------|-----------------|
| `monthly_token_limit` | `daalu_hosted_quotas` row | one month | billing contract |
| `rate_limit_rpm`      | `daalu_hosted_quotas` row | 1 min     | per-tenant fairness |
| `rate_limit_tpm`      | `daalu_hosted_quotas` row | 1 min     | per-tenant fairness |

Both rate limits live on the same row but are enforced via Redis
token buckets keyed by `daalu-hosted:rl:{rpm|tpm}:<tenant_id>:<minute_bucket>`.
The minute bucket changes every 60 s of wall clock; keys naturally
expire (we set a 65 s TTL on first hit) so memory pressure stays
bounded.

The TPM bucket is decremented **after** the call returns — we only
know token counts then. This means a single very large call can put
a tenant 100 % over their TPM for that minute, but the *next* call
that minute sees the limit and gets refused. Good enough for the
fairness use case; not a hard cap on tokens-per-minute.

---

## Overage policies

``daalu_hosted_quotas.overage_policy`` is one of:

* `hard_stop` — the gateway returns 429. The router *does not*
  fall through (the policy_action header tells the router that).
  Used for trial plans where we don't want surprise cloud bills.
* `throttle` — gateway returns 429 with `X-Daalu-Policy-Action:
  throttle`. The router sees the response and slows subsequent
  calls. (MVP: actual throttling is not yet implemented inside the
  gateway. The header is the signal; the router-side slowdown is a
  TODO.)
* `cloud_overflow` — gateway returns 429 with
  `X-Daalu-Policy-Action: cloud_overflow`. The router treats this
  as ``LLMUnavailable``, falls through to the next tier in its
  cascade (typically cloud). This is the friendliest default for
  production plans.

The policy is *enforced* by the gateway and *acted on* by the
router — both sides need to agree on the semantics. See
``core/llm.py::_local_via_gateway`` for the router-side handling.

---

## Operating

Health: `GET /healthz` — returns ``{"status":"ok"}``. K8s probes
hit this.

Metrics: `GET /metrics` — Prometheus, scraped by the same
ServiceMonitor pattern as daalu-api. Key series:

* `daalu_inference_gateway_requests_total{purpose,outcome}` —
  counter by outcome (``ok`` / ``rate_limited`` / ``cloud_overflow``
  / ``upstream_error`` / ``not_enrolled``).
* `daalu_inference_gateway_latency_seconds{purpose}` — histogram.

Per-tenant dashboards (token spend, refusal rate) aggregate from
``usage_events`` rather than gateway counters — keeps cardinality
bounded.

Logs: structlog, JSON. Important keys: ``tenant_id``, ``purpose``,
``outcome``, ``reason`` for refusals. We never log prompt bodies.

---

## Testing

The gateway is meant to be testable without GPUs. Two seams:

* `inference_gateway.upstream.chat_completion` accepts an injected
  httpx mock client. Unit tests cover the quota cascade by stubbing
  this.
* `inference_gateway.quota` is pure logic against an ``AsyncSession``
  and a Redis client. Tests use sqlite in-memory + fakeredis.

Integration tests (under ``tests/integration/inference_gateway/`` —
TODO) spin up a real vLLM via the workload-cluster fixture; CI
skips them unless ``RUN_GPU_TESTS=1``.

---

## Known gaps for production-grade

The MVP path is correct end-to-end but several follow-ups are still
required:

* [ ] Streaming responses (``stream=true``). Quota accounting needs
  per-chunk decrement.
* [ ] ``workers.tasks.reset_monthly_quotas`` Celery-beat task at
  month boundary.
* [ ] ``workers.tasks.reconcile_quotas`` — pull ``usage_events``
  rows that didn't make it into ``current_period_used`` (DB blip
  recovery).
* [ ] Replica-aware dispatcher when ``daalu-vllm-pool`` scales to
  >1 (current code assumes Service load-balances at L4, which loses
  context for continuous batching).
* [ ] Cross-tenant prompt isolation test under load — see design
  doc §10.
* [ ] mTLS between daalu-api and the gateway (currently HS256 JWT
  only; mTLS layered on top via Linkerd or Istio is the planned
  next step).

---

## See also

* Chapter 35 — Coding-assistant architecture (a heavy user of this gateway)
* Chapter 42 — Webhooks (a different external-facing surface; same auth principles)

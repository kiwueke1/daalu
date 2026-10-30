# DaaluApiHighLatency

> p95 latency on daalu-api above 1.5s for 10 minutes.

## Investigate

```bash
# Which endpoints?
histogram_quantile(0.95,
  sum by (le, handler) (rate(http_request_duration_seconds_bucket{job="daalu-api"}[5m]))
)

# Is it tied to LLM latency?
histogram_quantile(0.95, sum by (le, served_by) (rate(daalu_llm_latency_seconds_bucket[5m])))

# DB?
daalu_db_pool_in_use / daalu_db_pool_size
```

## Resolve

* LLM tail latency is the usual culprit — Anthropic Sonnet runs
  1–4s p95 by itself. If a request hits the quality tier and the
  fallback ladder, total can blow past 1.5s. Either accept it,
  switch the caller to a faster model, or move to local.
* DB pool saturation → see `DbPoolNearSaturation`.
* Single slow handler → profile with `py-spy` against one pod.

## Escalate

If p95 is >5s sustained, treat as a `DaaluApiHighErrorRate` (users
abandon and retry; retries amplify the load).

# LlmCostSpike

> LLM cost rate is 3× the weekly average.

## Why it matters

Either traffic is up (good — but make sure billing scales) or one
caller is in a runaway loop (bad — kill it before the Anthropic bill
finishes the month).

## Investigate

```promql
# Top sources by $ spent in the last hour
topk(10, sum by (source) (rate(daalu_llm_cost_usd_total[1h])) * 3600)

# Top tenants
topk(10, sum by (tenant_id) (rate(daalu_llm_cost_usd_total[1h])) * 3600)

# Top served_by tier
sum by (served_by) (rate(daalu_llm_cost_usd_total[1h])) * 3600
```

## Resolve

**One agent in a loop** — `kubectl logs` on that source's worker;
look for a retry storm. Kill the run via the agents UI or
`kubectl -n daalu-automation delete pod -l app=daalu-worker`.

**Anthropic share suddenly dominant** — the local NIM is offline
or fallback-thrashing. Cross-reference with `LocalGpuOffline` /
`ClassifierFallbackRateHigh`.

**Genuine traffic spike** — fine; check the bundle progress on each
affected tenant's `/billing` page and consider raising soft caps.

## Escalate

If the daily projected cost is >2× MRR for a single tenant, that
tenant is on the wrong plan — Sales / Account Management
conversation.

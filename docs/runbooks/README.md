# Runbooks

One file per alert in `deploy/k8s/monitoring/04-alerting-rules.yaml`.
Each alert's `runbook_url` annotation points here, so the on-call
operator sees the page they need on a single click from Slack /
PagerDuty.

| Runbook | Severity | Alert(s) it covers |
|---|---|---|
| [api-high-error-rate](api-high-error-rate.md) | warning | `DaaluApiHighErrorRate` |
| [api-high-latency](api-high-latency.md) | warning | `DaaluApiHighLatency` |
| [api-crashloop](api-crashloop.md) | critical | `DaaluApiPodCrashLooping` |
| [local-gpu-offline](local-gpu-offline.md) | warning | `LocalGpuOffline` |
| [classifier-fallback-rate-high](classifier-fallback-rate-high.md) | warning | `ClassifierFallbackRateHigh` |
| [llm-cost-spike](llm-cost-spike.md) | warning | `LlmCostSpike` |
| [llm-all-tiers-down](llm-all-tiers-down.md) | critical | `LlmAllTiersFailing` |
| [gpu-high-temp](gpu-high-temp.md) | warning | `GpuHighTemperature` |
| [gpu-ecc-errors](gpu-ecc-errors.md) | critical | `GpuEccErrors` |
| [gpu-underutilised](gpu-underutilised.md) | info | `GpuUnderUtilised` |

## Writing a new runbook

Every runbook follows the same shape:

1. **What the alert says** — one sentence quoting the description.
2. **Why it matters** — operator-facing impact.
3. **Investigate** — concrete commands to run, no hand-waving.
4. **Resolve** — the fixes most likely to work, ordered.
5. **Escalate** — who to ping if 1–4 don't.

If you can't fill in step 3 with copy-pasteable commands, the alert is
too vague. Tighten the rule before merging.

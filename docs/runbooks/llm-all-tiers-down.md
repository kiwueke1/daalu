# LlmAllTiersFailing

> Every LLM call is failing across every tier (local + external classifier + Anthropic).

## Why it matters

Critical. No AI feature works. Briefings stop, agent triage stops,
alert explanations stop. The platform reverts to a dashboard.

## Investigate

```bash
# Which tiers are failing?
kubectl -n daalu-automation logs -l app=daalu-api --tail=200 | grep llm_tier_failed

# Are the secrets present?
kubectl -n daalu-automation get secret daalu-automation-secrets -o json \
  | jq '.data | keys'

# Can we reach Anthropic at all from inside a pod?
kubectl -n daalu-automation exec deploy/daalu-api -- \
  curl -s -o /dev/null -w '%{http_code}\n' https://api.anthropic.com

# Local NIM?
kubectl -n daalu-automation exec deploy/daalu-api -- \
  curl -s -o /dev/null -w '%{http_code}\n' http://llm-classifier.daalu.svc/v1/models
```

## Resolve

The three most likely root causes, in order:

1. **Secrets rotated and the new value is wrong** —
   `ANTHROPIC_API_KEY` expired, or the new key has the wrong scope.
   Re-issue from console.anthropic.com, update the Secret, bounce
   `daalu-api`.

2. **Cluster egress broken** — `NetworkPolicy` drift, or the cluster
   ingress / NAT gateway died. From a pod:
   ```bash
   kubectl -n daalu-automation exec deploy/daalu-api -- \
     curl -sv https://api.anthropic.com 2>&1 | head -20
   ```
   If DNS or TCP fails, the issue is k8s networking, not the LLM
   providers.

3. **All three providers in one outage** — extremely unlikely but
   possible (Anthropic + DeepSeek + local GPU). Check status pages.
   If true, no fix — communicate to users that AI features are paused.

## Escalate

This is paged. Wake people. If unresolved in 30 min and a provider is
the issue, post a status update on the company status page.

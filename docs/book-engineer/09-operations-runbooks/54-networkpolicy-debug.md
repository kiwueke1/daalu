# 54. Debugging NetworkPolicies

NetworkPolicies are silent. They drop packets without telling
anyone. This chapter explains how we investigate.

---

## Our NetworkPolicy posture

Every namespace where Daalu runs has:

- A `daalu-default-deny` policy that denies all ingress by
  default.
- Per-app allow policies that explicitly open the required
  traffic.

The chart's `templates/networkpolicy.yaml` defines them.
Allows:

- `daalu-api` ← `daalu-frontend` (any namespace), `kube-
  system`, `monitoring` (port 8000).
- `daalu-frontend` ← `kube-system` (port 3000).
- `daalu-postgres` ← daalu pods (port 5432).
- `daalu-redis` ← daalu pods (port 6379).

`monitoring` ns access to daalu-api was added so Prometheus
can scrape `/metrics`. We bumped into this when daalu-api's
scrape target was timing out — see the conversation that
prompted this whole book.

---

## Symptoms of a NetworkPolicy block

- **Tcp connect hangs** (no RST, no timeout error). Usually
  a NetworkPolicy dropping the SYN.
- **Specifically, "context deadline exceeded" from
  Prometheus** scraping a target it should reach.
- **Service exists but `curl service.namespace.svc` from
  another pod hangs.**

Compare with:

- **RST** (connection refused): no listener on the target
  port. Different problem.
- **HTTP 4xx/5xx**: traffic is getting through; this is an
  app-level problem.

---

## Reproducing the problem

From inside a pod in the suspected-blocked namespace:

```bash
kubectl -n monitoring run debug --rm -it --image=alpine -- sh
# inside the pod:
nc -zv daalu-api.daalu-automation.svc.cluster.local 80
```

If `nc` hangs for 5+ seconds, it's likely NetworkPolicy.

---

## Identifying the policy

List policies in the **target** namespace (NetworkPolicy is
applied at the pod that receives the traffic):

```bash
kubectl -n daalu-automation get netpol
kubectl -n daalu-automation describe netpol daalu-api-ingress
```

Look at the policy's `spec.ingress`. Confirm:

- The `from` selectors match your caller. (Pod label,
  namespace label.)
- The `ports` list includes the target port.

---

## Common mistakes

### Wrong namespace selector

```yaml
- from:
    - namespaceSelector:
        matchLabels:
          name: monitoring   # ← wrong
```

Most CNI plugins set `kubernetes.io/metadata.name` on the
namespace, not `name`. Use:

```yaml
- from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: monitoring
```

### Forgetting that namespaceSelector + podSelector is AND

```yaml
- from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: monitoring
      podSelector:
        matchLabels:
          app: prometheus
```

This means "from pods labeled `app=prometheus` *in* the
monitoring namespace." It does **not** mean "from monitoring
ns OR from prometheus pods." Use separate list entries for
OR.

### Default-deny + missing egress

We default-deny ingress but allow all egress. If your app
needs to be reachable, an ingress rule. If you also want to
default-deny egress, you'll need to add explicit egress
rules for everything (including kube-dns).

We've considered tightening to default-deny egress; the
operational overhead vs the security gain hasn't justified
it yet.

---

## Verifying the fix

After a policy change:

```bash
kubectl -n daalu-automation apply -f networkpolicy.yaml
# wait ~5s for propagation
# retry the connection from the caller
```

Most CNIs apply NetworkPolicy in milliseconds. If your test
still fails 30 seconds later, the policy isn't the issue.

---

## Per-CNI quirks

- **flannel (k3s default):** doesn't implement NetworkPolicy
  at all. Policies are accepted but ineffective. We deploy
  a separate `kube-router` or `calico` for enforcement.
- **calico:** robust, comprehensive. Our preferred CNI on
  bare-metal clusters.
- **cilium:** very capable; has additional features
  (CiliumNetworkPolicy CRD) we don't use.

The cluster's CNI matters for whether your policies actually
do anything. Verify with a test policy that should drop
traffic; if it doesn't, your CNI isn't enforcing.

---

## What's next

Part X starts security. Chapter 55 — the formal threat
model.

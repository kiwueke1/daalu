# NVSentinel — install pointer (observe mode first)

[NVSentinel](https://github.com/NVIDIA/nvsentinel) (NVIDIA, open source) is a
Kubernetes-native **GPU fault auto-remediation** controller. It ingests health
signals — **DCGM** fields/health, **syslog/dmesg** (XID / GSP kernel lines), and
**K8s** node/pod conditions — classifies the fault, and drives an automated
remediation in seconds: cordon → drain → reboot. It is the **BYO-GPU SLA play**
— see [`docs/plans/nvidia-ai-factory/03-nvsentinel-resiliency.md`](../../../../docs/plans/nvidia-ai-factory/03-nvsentinel-resiliency.md).

> **Not promoted to destructive mode yet.** Per doc 03 §4 (risk: an over-eager
> misclassification could cordon/reboot a healthy node serving a tenant) and the
> acceptance criteria, NVSentinel is **piloted in observe / dry-run first** on
> the RTX 2000 Ada dev node. It watches DCGM and *records* what it would do; it
> does **not** cordon/drain/reboot until the GSP-hang → power-cycle hook (§1.1)
> is validated end to end. The hub reads its exported remediation metrics to
> render the AI-factory **Reliability** panel; it never drives remediation.

## Why observe-first matters for our hardware

The canonical failure we want auto-remediated ([[hardware_gpu_fragility]]) — the
RTX 2000 Ada GSP firmware hang — **does not recover from a soft reboot**; only a
**Rufio / BMC hard power-cycle** brings the card back. A vanilla NVSentinel
`reboot` action would "succeed" while the card stays dead. So before enabling
destructive remediation we must:

1. Run in observe mode long enough to calibrate which DCGM faults auto-cordon vs
   page a human (doc 03 acceptance #1).
2. Wire the **GSP-hang → Rufio/BMC power-cycle** escalation (doc 03 §1.1, P2) —
   Daalu already drives Rufio for the workload cluster, so this is a call into
   that path, not new infrastructure. **Until that hook exists, leave
   remediation in observe mode.**
3. Gate re-admission of a remediated node on the `gpu_controller`
   `dcgmi diag -r 2` pass (doc 02 §4.1).

## Prerequisites

- **GPU Operator** already running dcgm-exporter (Daalu runs v25.10.1); DCGM
  series land in `kube-prometheus-stack-prometheus` (doc 02 §0).
- Images mirrored to **Harbor** — NVSentinel is open source; mirror + pin, no
  NGC entitlement ([[foss_inference_pivot]]).

## Install (Helm, observe mode)

```bash
# Mirror first (no NGC entitlement needed):
#   harbor.daalu.io/nvidia/nvsentinel/*  <-  upstream NVSentinel images
helm upgrade --install nvsentinel \
  oci://harbor.daalu.io/nvidia/nvsentinel/nvsentinel \
  --namespace nvsentinel --create-namespace \
  -f deploy/k8s/gpu/09-nvsentinel/10-values.observe.yaml
```

Then apply the ServiceMonitor so the hub can scrape its remediation metrics and
light up the Reliability panel:

```bash
kubectl apply -f deploy/k8s/gpu/09-nvsentinel/20-servicemonitor.yaml
```

Set `nvsentinel_metrics_job` in daalu-automation-secrets to the scrape job name
(default `nvsentinel`) so the AI-factory Reliability panel reports it as active.

## Not auto-deployed

`09-` is deliberately outside the gpu-workloads ArgoCD include-glob
(`{01-,03-,05-,07-}*`), so this never auto-applies. It is a **hand-applied
pilot** until doc 03 P1–P3 are validated. Promote to destructive remediation
(and into ArgoCD) only after the Rufio power-cycle hook and the re-admission
gate are proven on the dev node.

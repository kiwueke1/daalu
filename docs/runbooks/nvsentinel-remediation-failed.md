# NvSentinel — remediation failed / down / node stuck cordoned

> The GPU auto-remediation layer (NVSentinel) is down, failed to remediate a
> fault, or left a GPU node cordoned. The BYO-GPU SLA layer needs a human now.

## Why it matters

NVSentinel is the automation that contains GPU faults in seconds (cordon →
drain → reboot) so a BYO-GPU SLA is defensible. When it can't finish a
remediation, the card is out of service and *not coming back on its own* — the
classic case being the RTX 2000 Ada **GSP firmware hang**, which a soft reboot
does **not** clear; only a hard power cycle recovers it.

## Triage

```bash
# Is NVSentinel itself alive? (NvSentinelDown)
kubectl -n nvsentinel get pods
kubectl -n nvsentinel logs deploy/nvsentinel --tail=200

# Which GPU nodes are cordoned / unschedulable? (NodeStuckCordoned)
kubectl get nodes -l nvidia.com/gpu.present=true -o wide

# What did NVSentinel classify + attempt? (RemediationFailed)
kubectl -n nvsentinel logs deploy/nvsentinel --tail=200 | grep -iE 'remediat|xid|gsp|reboot'
ssh <gpu-node> sudo dmesg | grep -iE 'nvidia|xid|gsp|fell off the bus'
```

## Resolve

### GSP hang / "fallen off the bus" — soft reboot will NOT fix it

This is the case NVSentinel's vanilla `reboot` cannot remediate. Escalate to a
**Rufio / BMC hard power-cycle** (the same path Daalu drives for the workload
cluster), then let the node come back:

```bash
# Hard power-cycle via Rufio/BMC (NOT `systemctl reboot`).
#   <see the workload-cluster Rufio/Tinkerbell power path>
```

After the card returns, do **not** re-admit it until the
`gpu_controller` `dcgmi diag -r 2` re-admission gate passes (doc 02 §4.1):

```bash
# From the AI-factory UI (admin → Diagnostics) or directly:
dcgmi diag -r 2 -j
kubectl uncordon <gpu-node>   # only after the diag passes
```

### NVSentinel down

Restart it; until it's back, GPU faults are unremediated — watch the manual
`GpuEccErrors` / `GpuHighTemperature` alerts and act on their runbooks.

```bash
kubectl -n nvsentinel rollout restart deploy/nvsentinel
```

## Note

NVSentinel ships in **observe / dry-run** mode first
(`deploy/k8s/gpu/09-nvsentinel/10-values.observe.yaml`). In observe mode it
records intent but does not cordon/reboot — a "remediation failed" in that mode
means the *detection/classification* path errored, not that a destructive action
half-completed. Promote to destructive mode only after the Rufio hook +
re-admission gate are validated (doc 03 P2–P3).

# GpuEccErrors

> Hardware-level GPU error (XID or ECC double-bit) observed.

## Why it matters

Critical. ECC double-bit means a real bit flip the card couldn't
correct. Continuing to use the card produces silently corrupt
inference output. Cordon and replace.

## Resolve

```bash
# Cordon the GPU node so no new workloads land
kubectl cordon gpu-node-1

# Drain it gracefully — NIM pods will move (or fail, since they're
# the only thing tolerating gpu=*:NoSchedule). The classifier tier
# falls back to external automatically.
kubectl drain gpu-node-1 --ignore-daemonsets --delete-emptydir-data

# Inspect the kernel ring on the GPU node
ssh gpu-node-1 sudo dmesg | grep -iE 'nvidia|xid|ecc'
```

A single XID 79 ("GPU has fallen off the bus") sometimes recovers
after a reboot. Multiple ECC double-bits in a short window =
hardware fault. RMA the card if under warranty; otherwise swap to a
spare.

## Escalate

Wake someone. This is rare and worth the disruption.

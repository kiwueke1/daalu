# GpuHighTemperature

> GPU > 85°C sustained for 10 minutes.

Ada-generation workstation GPUs (e.g. RTX 2000 Ada) throttle at 90°C — past that you lose throughput
without seeing an obvious failure. Long-term, heat shortens silicon
life.

## Resolve

Physical first: confirm the GPU node has airflow. Stack of books on
top is a classic cause.

Cluster-side: throttle the time-sliced replica count temporarily.
Editing `time-slicing-config` ConfigMap from `replicas: 4` to
`replicas: 2` halves concurrent compute and drops sustained
temperature by ~5–10°C.

Re-run after cooling to confirm it stays below 80°C under load.

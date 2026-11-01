# 95. GPU health — the `dcgmi diag` runner, the Reliability panel, and NVSentinel in observe mode

Chapter 32 tells the GSP-firmware-hang story and chapter 80 gets DCGM metrics
into AI Factory. This chapter covers what Daalu does when you need to know
whether a card is **healthy**, and what it deliberately does *not* do yet when
one isn't:

1. an on-demand **`dcgmi diag`** run (NVIDIA's validation suite) that a GPU
   owner starts from **AI Factory → Diagnostics** and the gpu-controller runs
   as a one-shot Job on the GPU node;
2. a read-only **Reliability** view (XID, uncorrectable ECC, temperature, and
   whether NVSentinel is running);
3. **NVSentinel**, NVIDIA's open-source fault auto-remediation controller,
   shipped as a hand-applied, **observe/dry-run** install that cordons,
   drains and reboots nothing.

| Piece | Path |
|---|---|
| Job builder, stress warnings, parser (pure) | `src/daalu_automation/gpu_controller/diagnostics.py` |
| Run row (`gpu_diagnostic_runs`) | `src/daalu_automation/models/gpu_diagnostic_run.py`, migration `migrations/versions/0023_gpu_diagnostic_runs.py` |
| Runner (reconcile loop) | `src/daalu_automation/gpu_controller/app.py:486` `_reconcile_diagnostics`, `:539` `_run_one_diagnostic` |
| API | `src/daalu_automation/api/routers/gpu_metrics.py:401` (observability validate), `:479-582` (diagnostics), `:821-944` (reliability) |
| Settings | `config.py:207-216` (`gpu_diagnostics_exec_enabled`, `gpu_dcgm_diag_image`), `:253-262` (`nvsentinel_metrics_job`), `:272` (`gpu_cuda_checkpoint_enabled`) |
| UI | `frontend/app/ai-factory/{diagnostics,reliability}/page.tsx`, `components/diagnostics-panel.tsx`, `components/reliability-panel.tsx` |
| Alert rules | `deploy/k8s/monitoring/04-alerting-rules.yaml:133-275` (`daalu-gpu`, `daalu-gpu-reliability`) |
| NVSentinel pilot | `deploy/k8s/gpu/09-nvsentinel/` (`00-install.md`, `10-values.observe.yaml`, `20-servicemonitor.yaml`, `CUDA-CHECKPOINT-LEGAL.md`) |
| Plans | `02-dcgm-observability.md` (internal design notes, not published) §4, `03-nvsentinel-resiliency.md` (internal design notes, not published) |
| Tests | `tests/test_ai_factory.py` (diag builders, parser, stress gate, scoping), `tests/test_aiperf.py:503-526` (reliability) |

---

## 95.1 Three kinds of "diagnostic", two execution paths

`GpuDiagnosticKind` (`models/gpu_diagnostic_run.py:37-40`):

| Kind | What runs | Who runs it | Touches the GPU? |
|---|---|---|---|
| `observability_validate` | the doc 02 §4A checklist: Prometheus configured, a DCGM target up, series carry this tenant's label, the alert pipeline alive (Watchdog) | **daalu-api, inline** (`POST /ai-factory/observability/validate`, `gpu_metrics.py:401`) — lands `passed`/`failed` immediately | no, read-only PromQL |
| `dcgmi_diag` | `dcgmi diag -r <level> -j` | **gpu-controller**, async | yes |
| `nccl_test` | nccl-tests `all_reduce_perf` | gpu-controller | yes — but see §95.3 |

The exec kinds follow the same queue pattern as AIPerf (chapter 93): the API
writes a `pending` row, and the gpu-controller — the only component with
Kubernetes credentials for GPU clusters — picks it up on its 30 s tick
(`app.py:354`). States: `pending → running → passed | failed | error`.

---

## 95.2 Asking for a run: roles and the stress acknowledgement

`POST /ai-factory/gpu/diagnostics` (`gpu_metrics.py:491`) requires a **tenant
admin** (`current_admin`) whose factory view has a GPU (owner or provider;
consumers get 403). Listing and detail are tenant-scoped; another tenant's run
id returns 404 (`gpu_metrics.py:558-567`).

**Stressful runs need an explicit acknowledgement.** `diag_stress_warning`
(`diagnostics.py:117`) returns a warning for anything heavier than a level-1
diag. If the request doesn't carry `acknowledged: true`, the API answers
**412** with `{requires_acknowledgement, warning, kind, level}`
(`gpu_metrics.py:508-519`). The diagnostics panel shows the warning in a
confirm dialog and re-submits with `acknowledged: true`
(`diagnostics-panel.tsx:27-58`, `:150-160`).

| Level | What the code says it does (`diagnostics.py:113-146`) | Ack? |
|---|---|---|
| 1 | quick software / PCIe / NVML pass, safe alongside live traffic | no |
| 2 | ~2–3 min targeted stress: memory, SM compute, PCIe bandwidth; needs several GB free VRAM, which vLLM on a shared card may not leave | yes |
| 3 | ~15+ min full stress suite; saturates the GPU, degrades live inference, and on a fragile card "can trip a firmware hang that needs a hard power-cycle" | yes |
| NCCL | full-bandwidth interconnect test across every GPU | yes |

**Levels are 1–3.** `clamp_level` (`diagnostics.py:24-28`) clamps anything
else into that range (default 1), and the UI offers only 1/2/3
(`diagnostics-panel.tsx:21-25`). DCGM itself also has a level 4; Daalu does not
expose it. These are `dcgmi` test depths, unrelated to the AI-Ops maturity
levels (chapters 81–90).

**Why the acknowledgement lives in the API, not only in the UI:** the warning
text is generated server-side from the same function the runner uses, so a
script calling the API hits the same 412 a browser does.

---

## 95.3 How `dcgmi diag` actually runs

### The exec gate: `gpu_diagnostics_exec_enabled` (off by default)

`config.py:212`. When off, `_reconcile_diagnostics` finishes every pending exec
run as `error` with `code: exec_disabled` and a message explaining why
(`app.py:508-526`): `dcgmi diag` exercises the GPU, and the production card is
a single shared card serving live traffic. `observability_validate` is not
affected by the flag (it never reaches the controller,
`app.py:493-500` filters it out). The flag is read by the gpu-controller from
`daalu-automation-secrets` (`deploy/k8s/gpu-controller/deployment.yaml:34-39`);
no manifest in `deploy/` sets it, so a fresh install has it off.

### NCCL is refused on single-GPU nodes

`_run_one_diagnostic` finishes any `nccl_test` immediately as `error`: "nccl-tests
require a multi-GPU node; N/A on the single-card Ada" (`app.py:554-564`). The
Job builder `nccl_all_reduce_job` and `nccl_eligible` (`diagnostics.py:202-239`)
exist and are unit-tested, but nothing calls them yet.

### The Job (`dcgmi_diag_job`, `diagnostics.py:47-110`)

The original design (the module docstring at `diagnostics.py:8`, and the unused
`DCGM_POD_NAMESPACES` / `DCGM_POD_LABEL` / `DCGM_CONTAINER` constants at
`:17-21`) was to `kubectl exec` `dcgmi` inside the GPU Operator's
dcgm-exporter pod. **That was abandoned:** the dcgm-exporter image ships no
`dcgmi` CLI and lacks the NVVS diagnostic plugins (`app.py:566-567`,
`diagnostics.py:36-43`). The docstring is stale; the Job is the real path.

What the controller now creates, in the GPU stack's namespace on the stack's
cluster (`app.py:568-575`):

- image `nvcr.io/nvidia/cloud-native/dcgm:4.4.2-1-ubuntu22.04`
  (`DCGM_DIAG_IMAGE`, `diagnostics.py:44`), overridable with
  `gpu_dcgm_diag_image`;
- command `nv-hostengine && sleep 2 && dcgmi diag -r <lvl> -j` — the full DCGM
  image doesn't start a host engine by itself (`diagnostics.py:98-103`);
- `resources.limits: nvidia.com/gpu: 1` (`diagnostics.py:104`);
- `nodeSelector` on `kubernetes.io/hostname` = the stack's pinned `gpu_node`,
  else on `gpu-class` (`diagnostics.py:64-68`); tolerations for `gpu` and
  `nvidia.com/gpu` taints;
- `backoffLimit: 0`, `activeDeadlineSeconds: 1800`, `ttlSecondsAfterFinished: 600`.

The controller waits up to 1500 s (`app.py:579`), reads the pod log, deletes
the Job, and parses.

**Image/driver matching is the most common failure.** Two things must line up
(`diagnostics.py:36-43`, `config.py:213-215`):

- DCGM version vs GPU driver: too old gives "Detected unsupported Cuda
  version";
- NVVS plugin set vs CUDA: the lab's driver (595.x) is CUDA-13 era, so the image
  must ship the cuda13 plugins. `4.4.2-1` ships cuda12, cuda13 and cudaless.

Mirror the image to Harbor and pin it per cluster with `gpu_dcgm_diag_image`.

**The Job needs a free GPU slice.** It requests one `nvidia.com/gpu`. On a
time-sliced node (chapter 94) that is one of the replicas; on an unshared card
already held by vLLM it stays `Pending` until the timeout. In that case
`_run_job_capture` returns the text "diagnostic pod never scheduled within the
timeout — no free GPU slice on the node, or the image could not be pulled"
instead of raising (`app.py:837-841`), which the parser reads as a failed run
with that text in `raw_tail`.

### Parsing (`parse_dcgmi_output`, `diagnostics.py:149`)

Prefers the `-j` JSON: walks the document collecting every verdict string
under a `result` key (DCGM 3.x) or a `status` key (DCGM 4.4.x
`test_summary`), accepting only known verdict values so unrelated `status`
fields aren't picked up (`_collect_results`, `diagnostics.py:180-199`).
`passed` = no `Fail`/`Failed`; the summary records `checks`, `failed`, and up to
50 results. Falls back to a text heuristic (`pass` present, `fail` absent). The
last 8000 chars of output are stored in `run.output`.

A failure to create or run the Job (image pull, RBAC, timeout) is surfaced
verbatim as `error` with the exact `dcgmi` command (`app.py:580-594`).

**RBAC to check.** On a tunnelled cluster the controller uses the joined
cluster's self-registered kubeconfig (chapter 80 §80.1, cluster-admin by
default). On the operator cluster it uses its own ServiceAccount, whose
ClusterRole (`deploy/k8s/gpu-controller/rbac.yaml`) has no `batch/jobs` or
`pods/log` permissions, so a diag against an operator-cluster stack fails with
a 403 until the role is extended.

---

## 95.4 The Reliability panel: read-only health signals

`GET /ai-factory/reliability` (`gpu_metrics.py:831`) is read-only and
tenant-scoped: every query goes through `tenant_selector` (`:870`) against the
tenant's Prometheus (`client_for_tenant`, chapter 80 §80.4). It returns:

| Signal | PromQL | Level |
|---|---|---|
| XID errors | `max(DCGM_FI_DEV_XID_ERRORS{sel})` | crit if > 0 |
| Uncorrectable ECC (DBE) | `max(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL{sel})` | crit if > 0 |
| Max temperature | `max(DCGM_FI_DEV_GPU_TEMP{sel})` | warn ≥ 85 °C, crit ≥ 90 °C |

(`gpu_metrics.py:876-897`). Overall status is the worst of the three.

Plus two blocks:

- **`nvsentinel`** — `active` is `max(up{job="<nvsentinel_metrics_job>"}) ≥ 1`
  (`gpu_metrics.py:903`, job default `nvsentinel`, `config.py:262`);
  `remediations` is a best-effort `sum(nvsentinel_remediations_total)` that
  tolerates a missing series (`:907-908`, the series name varies by NVSentinel
  version); **`mode` is hard-coded `"observe"`** (`:919`) — the API does not
  read the mode from NVSentinel.
- **`cuda_checkpoint`** — `gated` unless `gpu_cuda_checkpoint_enabled`
  (`config.py:272`, default false). cuda-checkpoint is proprietary NVIDIA
  software and needs legal sign-off before it ships
  (`deploy/k8s/gpu/09-nvsentinel/CUDA-CHECKPOINT-LEGAL.md`). Nothing in the code
  executes it.

When NVSentinel isn't scraped, the panel says: "Auto-remediation is not active
yet — GPU faults page a human via the runbook"
(`reliability-panel.tsx:135-136`).

**Design rule: the hub never drives remediation.** The reliability route
comment (`gpu_metrics.py:821-828`), the install doc and the plan all say the
same thing: NVSentinel acts cluster-side; Daalu only reads its metrics.

---

## 95.5 NVSentinel: what's in the repo, and why it's observe-only

NVSentinel ingests DCGM health, syslog/dmesg (XID and GSP kernel lines) and
Kubernetes node conditions, classifies the fault, and remediates: cordon →
drain → reboot (`03-nvsentinel-resiliency.md` (internal design notes, not published) §1).
It is NVIDIA's code, not Daalu's; Daalu ships install material and reads its
metrics.

### What's in `deploy/k8s/gpu/09-nvsentinel/`

- `00-install.md` — Helm install from a Harbor mirror into namespace
  `nvsentinel`, then apply the ServiceMonitor and set `nvsentinel_metrics_job`.
- `10-values.observe.yaml` — the safety switch: `remediation.dryRun: true`
  and `cordon`, `drain`, `reboot`, `powerCycle` all `false`
  (`10-values.observe.yaml:29-40`); `maxAttempts: 3`, `backoffSeconds: 300` so
  a flapping fault escalates to a human; reuse the GPU Operator's dcgm-exporter
  rather than running a second DCGM (`:23-26`); metrics on port 9500. The file
  itself warns that its key names "follow the upstream chart's intent" and must
  be reconciled against the pinned chart's `values.yaml` at install time
  (`:10-13`). **Verify the keys against the real chart before trusting the
  dry-run guarantee.**
- `20-servicemonitor.yaml` — scrapes NVSentinel and relabels `job` to
  `nvsentinel`, so the reliability query and the alerts match regardless of
  the chart's target naming (`20-servicemonitor.yaml:29-35`).

None of it auto-deploys: `09-` is outside the `daalu-gpu` ArgoCD include glob
`{01-,03-,05-,07-}*` (`00-install.md:63-69`). STATUS §03 records it as **not
installed**; the pilot target is the RTX 2000 Ada dev node.

### Why not turn remediation on

From the install doc (`00-install.md:18-33`) and plan §1.1: the failure this
hardware actually has — the RTX 2000 Ada GSP firmware hang (chapter 32, §GSP
firmware hangs) — **does not recover from a soft reboot**. Only a BMC hard
power-cycle (Rufio) brings the card back. A stock NVSentinel `reboot` would
report success while the card stays dead. So three things must exist before
destructive mode:

1. observe-mode history to calibrate which faults cordon vs page a human;
2. a GSP-hang → Rufio/BMC power-cycle escalation hook;
3. a re-admission gate: the gpu-controller only returns a node to service after
   a `dcgmi diag -r 2` pass.

**None of the three is built** (STATUS §03, "pending … deferred"). In
particular, the re-admission gate in plan §1.2 is a design: the
gpu-controller has no code that cordons, uncordons, or re-admits nodes. Today
the `dcgmi diag` runner in §95.3 is a manual tool, not a gate. SLA/downtime
accounting (plan P4) is also unbuilt.

---

## 95.6 Alert rules

`deploy/k8s/monitoring/04-alerting-rules.yaml`:

**`daalu-gpu`** (`:133-212`): `GpuHighTemperature` (> 85 °C for 10 min),
`GpuEccErrors` (`rate(DCGM_FI_DEV_XID_ERRORS[5m]) > 0 OR
rate(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL[5m]) > 0` for 5 min, critical),
`DcgmExporterDown` (`up{job="nvidia-dcgm"} == 0`), `GpuMemoryNearFull`
(> 97 % framebuffer), `GpuUnderUtilised` (info).

**`daalu-gpu-reliability`** (`:214-275`), dormant until NVSentinel is scraped:
`NvSentinelDown`, `NvSentinelRemediationFailed`
(`increase(nvsentinel_remediations_failed_total[15m]) > 0`, critical),
`NvSentinelNodeStuckCordoned`.

Things to check before explaining them:

- **`GpuEccErrors` applies `rate()` to `DCGM_FI_DEV_XID_ERRORS`.** Confirm that
  field's semantics in the dcgm-exporter docs for the pinned version: if it
  reports the most recent XID code rather than a running count, `rate()` fires
  on a *change* of code, not per error. The Reliability panel's `max(...) > 0`
  check on the same field has the matching caveat: it stays critical for as
  long as the last XID value is non-zero.
- **`DcgmExporterDown` hard-codes `job="nvidia-dcgm"`.** Prod's DCGM series
  arrive under per-tenant tunnel jobs (`gpu-tunnel-dcgm-<tenant>`, chapter 80
  §80.5), the same mismatch the observability self-check had to fix.
- **`NvSentinelNodeStuckCordoned` fires for any GPU node** unschedulable for an
  hour (`kube_node_spec_unschedulable` × `nvidia.com/gpu.present`), whoever
  cordoned it, NVSentinel or a human.
- **Two runbook links are dead:** `GpuMemoryNearFull` and `DcgmExporterDown`
  point at `docs/runbooks/gpu-memory-near-full.md` and
  `docs/runbooks/dcgm-exporter-down.md`, which don't exist. The others
  (`gpu-high-temp.md`, `gpu-ecc-errors.md`, `gpu-underutilised.md`,
  `nvsentinel-remediation-failed.md`) do.

---

## 95.7 Operational notes

- **Prefer level 1 on a live card.** Level 2 and 3 compete with vLLM for VRAM
  (vLLM reserves most of the card) and, on a card with a firmware-hang history,
  risk the very failure you're diagnosing. Run them on a drained card in a
  maintenance window.
- **Enabling exec:** set `GPU_DIAGNOSTICS_EXEC_ENABLED=true` (and, if needed,
  `GPU_DCGM_DIAG_IMAGE`) in `daalu-automation-secrets` and restart the
  gpu-controller by hand — it isn't ArgoCD-managed (chapter 80 §80.6).
- **When the panel says "not active":** check `up{job="nvsentinel"}` in the
  tenant's Prometheus; the panel is only as good as the ServiceMonitor relabel
  and `nvsentinel_metrics_job` agreeing.
- **Tests:** `tests/test_ai_factory.py` covers `dcgmi_diag_cmd` + `clamp_level`,
  the JSON/text parser, NCCL eligibility and Job shape, the stress-warning
  gate, the `dcgmi_diag_job` builder, "requires a GPU then enqueues", and
  tenant scoping. `tests/test_aiperf.py` covers the reliability route with no
  GPU and without Prometheus. Nothing exercises a real Job or a real
  NVSentinel.

---

## Related

- [Chapter 32 — NVIDIA-specific notes](32-nvidia-notes.md) (§GSP firmware
  hangs, §DCGM exporter).
- [Chapter 80 — Zero-touch GPU onboarding](80-zero-touch-gpu-onboarding.md):
  the gpu-controller, tenant Prometheus resolution, the multi-GPU DCGM scrape.
- [Chapter 94 — GPU sharing](94-gpu-sharing-time-slicing-and-mps.md): why a
  diag Job needs a free slice.
- [Chapter 93 — AIPerf load testing](93-aiperf-load-testing.md): the sibling
  runner on the same queue-and-Job pattern.
- [Chapter 30 — vLLM on the workload cluster](30-vllm-deployment.md).

# scripts/

Top-level deploy scripts. Each one is a thin, readable shell wrapper
around the `make` targets and the underlying installers in `deploy/`
and `dev/` — open any script and you can see exactly what runs.

Why `.sh` instead of just `make` targets directly: the `Makefile` is
optimised for short target names and tab-sensitive rules. Shell
scripts read top-to-bottom, can branch on flags, print progress
banners, and run pre-flight checks before invoking the same `make`
targets under the hood. There is no logic duplication — the scripts
*call* `make` rather than re-implementing it.

## When to run which

| Situation | Script |
|---|---|
| Brand-new laptop, want a working stack to develop against | `./deploy-from-scratch.sh` (defaults to dev/Kind) |
| Brand-new EKS/GKE/bare-metal cluster, building prod from zero | `./deploy-from-scratch.sh --prod` |
| Existing prod cluster, observability/SRE only | `./deploy-observability.sh` |
| Existing prod cluster, home-GPU/LLM-router only | `./deploy-gpu.sh` |

## The scripts

### `deploy-from-scratch.sh`
Full stand-up of the platform on a fresh cluster.

* **Default (dev):** runs `make bootstrap-dev` → creates a Kind
  cluster, installs ingress-nginx + monitoring, builds the app images
  locally and side-loads them, applies manifests, runs migrations,
  seeds the default tenant + SKUs. ~10 minutes.
* **`--prod`:** runs `deploy/bootstrap-prod.sh` → cert-manager +
  Let's Encrypt → ingress-nginx → kube-prometheus-stack + Loki +
  Promtail + blackbox + alert rules + dashboards → NVIDIA GPU
  Operator → ArgoCD + app-of-apps. Requires `$LETSENCRYPT_EMAIL` and
  KUBECONFIG already pointing at the cluster. ~15 minutes.

  Flags: `--email`, `--no-gpu`, `--ingress traefik`.

### `deploy-observability.sh`
Just the SRE stack: `make monitoring && make argocd`. Installs (or
upgrades — idempotent) kube-prometheus-stack, Loki, Promtail,
blackbox exporter, ServiceMonitors, alert rules, dashboards, and
ArgoCD with the app-of-apps. Prints the next-step instructions for
wiring paging (Alertmanager → Slack + PagerDuty).

Flags: `--env <dev|prod>` (default: prod).

### `deploy-gpu.sh`
The laptop-side half of Part A from the go-live checklist:

1. Host prep on the GPU node (driver + nvidia-container-toolkit) over
   SSH. Skipped with `--skip-host-prep` if already done.
2. GPU Operator install — **skipped** on the CRI-O workload cluster
   with `--skip-operator`. Use the standalone device-plugin DaemonSet
   instead (see `docs/nvidia-strategy.md` Appendix B §B.4).
3. Time-slicing ClusterPolicy patch — gated on the Operator path; a
   no-op with `--skip-operator`. Verifies `nvidia.com/gpu` capacity.
4. HF token secret in `daalu` ns (needs `$HF_TOKEN` — free; click
   through Meta's licence at
   https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct first)
5. vLLM Llama-3.1-8B-Instruct (AWQ INT4) Deployment + Service + PDB
   + NetworkPolicy
6. Wait for vLLM rollout (~5 GB AWQ weights download from HF on
   first start)
7. Upgrade `daalu-edge` with `serviceProxies[]` so :8000 (vLLM) and
   :9400 (DCGM) ride the existing WireGuard tunnel
8. Cross-tunnel smoke test from a daalu-api pod
9. Seed the SKU catalog (`daalu seed-skus`)
10. Prints the manual `kubectl patch secret` step to flip
    `LLM_LOCAL_BASE_URL` in `daalu-automation-secrets`.

Flags: `--skip-host-prep`, `--skip-operator`, `--skip-vllm`
(alias: `--skip-nim`), `--skip-edge-upgrade`, `--skip-ops-smoke`,
`--skip-seed-skus`, `--gpu-node <name>`, `--gpu-class <class>`.

## Conventions

* Every script is idempotent — re-running is safe.
* Every script uses `set -euo pipefail`.
* Every script accepts `-h` / `--help` and prints its own header.
* Long-running waits use `kubectl rollout status` / `wait` with
  explicit timeouts so a wedged install fails loudly instead of
  hanging forever.
* Scripts print **next steps** at the end — what to verify, what
  manual step (if any) still needs your hands.

## Mapping back to the existing `make` targets

| `make` target | Script that calls it |
|---|---|
| `make bootstrap-dev` | `deploy-from-scratch.sh` |
| `make bootstrap-prod` (via `deploy/bootstrap-prod.sh`) | `deploy-from-scratch.sh --prod` |
| `make monitoring` | `deploy-observability.sh` |
| `make argocd` | `deploy-observability.sh` |
| `deploy/k8s/gpu/00-install-gpu-operator.sh` | `deploy-gpu.sh` |

If you'd rather invoke `make` directly, that still works — the
scripts don't replace it, they just give you a single readable
entry point per scenario.

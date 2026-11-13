# 35. Connecting your own GPU — end-to-end

This chapter takes you from "I have a Kubernetes cluster with an NVIDIA
GPU in it" to "Daalu's AI runs on my hardware." It is shorter than it used
to be, because the onboarding is now **zero-touch**: once your cluster is
connected, adding a GPU is a single form in the UI. Daalu discovers the
card, deploys the serving stack, wires the routing, and shows you the
deploy as it happens.

Chapter 15 covers *why* local inference (cost, privacy, latency) and what
the router does with it. This chapter is the *how*.

---

## What you'll end up with

```mermaid
flowchart LR
    subgraph yours [Your cluster]
        V[vLLM serving stack<br/>OpenAI-compatible API]
        E[daalu-edge tunnel]
        V --- E
    end
    E ===|encrypted WireGuard tunnel| H[Daalu LLM router]
    H -->|hot-path inference| E
```

Daalu deploys an **open vLLM stack** (Apache-2.0 serving engine, open
model weights — Llama 3.1 8B Instruct by default) onto your GPU node and
routes your tenant's hot-path inference — alert triage, classification,
briefings — through the tunnel to it. Prompts stay on your network; your
per-call inference cost drops to the electricity.

---

## Hardware quick guide

| GPU | VRAM | Good for |
|---|---|---|
| 16 GB class (e.g. RTX 2000 Ada) | 16 GB | The 8B default model — the everyday starter card |
| 24 GB class (e.g. RTX 4090) | 24 GB | 8B with more headroom; best price/throughput |
| 48 GB class (e.g. RTX 6000 Ada, A40) | 48 GB | Larger models, including the ~30B coding models the Workspace uses |
| 80 GB class (A100/H100) | 80 GB | 70B-class models, heavy concurrency |

One card is enough to start. You can connect more than one — each shows
up individually on the AI Factory page (chapter 50).

---

## Prerequisites

Things that must be true before the form will succeed. All are one-time:

1. **The NVIDIA driver on the GPU node.** `nvidia-smi` on the node lists
   the card. Daalu doesn't install drivers: that stays with whoever owns the
   node's OS image.
2. **The cluster is connected to Daalu** via `daalu-edge` (chapter 34).
   The install snippet the cluster-onboarding wizard generates already
   includes everything the GPU path needs — the inference and telemetry
   proxies are pre-wired, and the edge registers the cluster credential
   with Daalu by itself. There is no extra manifest and no kubeconfig to
   paste.
3. **A Hugging Face token, if your model is gated.** The default Llama
   weights require accepting Meta's licence on Hugging Face and supplying
   a read token once. Ungated models (e.g. Qwen) need none.

That's it. In particular, you do **not** need to:

- install the **NVIDIA GPU Operator** or the **NVIDIA Container Toolkit** —
  if the cluster doesn't have them, **Add GPU** installs the operator, and
  the operator installs the toolkit (next section);
- hand-label the GPU node — Daalu derives the GPU class automatically
  from what GPU discovery reports about the card;
- mint credentials or assemble a kubeconfig — the edge self-registers,
  and a rebuilt cluster replaces its own stale credential automatically;
- run any follow-up `helm upgrade` — the initial install is complete.

> **Running Daalu yourself?** The deploy is done by Daalu's **GPU
> controller** service. Both install methods in
> [Getting started](../../getting-started.md) include it; it needs
> `SERVICE_TOKEN_SECRET_KEY` set in your `.env` (the guide's first step).

---

## The NVIDIA GPU Operator, installed for you

Kubernetes only sees a GPU once the NVIDIA GPU Operator runs (device
plugin, GPU discovery, DCGM telemetry). When you pick a cluster in **Add
GPU** that advertises no GPU, the form offers **Install GPU Operator**.
Confirm, and Daalu installs chart v25.10.1 into the `gpu-operator`
namespace over the tunnel, with the settings that cluster needs:

| Part | What Daalu sets up |
|---|---|
| **Driver** | Uses the one already on the node. The operator can build and install drivers itself, but only for the distributions and kernels NVIDIA publishes driver images for, so Daalu leaves the driver to whoever owns the node's OS. That's why the driver is the one prerequisite. |
| **Container Toolkit** | Installed by the operator, as a pod on each GPU node. It puts the NVIDIA runtime under `/usr/local/nvidia/toolkit` and writes the GPU's CDI spec (below). **On k3s** its containerd settings go to a file k3s never reads: left to its defaults it writes containerd's config with standard CNI paths that k3s doesn't use, and the node goes `NotReady`. Instead k3s restarts once (about a minute without the cluster API; running pods keep running) and registers the NVIDIA runtime itself. |
| **CDI** | The [Container Device Interface](https://github.com/cncf-tags/container-device-interface): a small file on the node (e.g. `/var/run/cdi/nvidia.yaml`) that tells the container runtime which device files and driver libraries make up "GPU 0". containerd 2.x reads it directly, so a pod gets a GPU by asking for `nvidia.com/gpu: 1`, with no special runtime class. Daalu's vLLM pod relies on this. |
| **GPU metrics** | DCGM's ServiceMonitor is turned on when the cluster runs the Prometheus Operator (the operator ships it off, so Prometheus would never scrape GPU metrics and AI Factory would stay empty). |
| **Tenant label** | Every GPU metric is labelled with your tenant's ID; AI Factory only shows series carrying it. Daalu fills it in. |

The form shows how far along it is ("3 of 8 pods ready"), waits through
the k3s restart, and moves on to the detected GPU by itself, usually 2–5
minutes later. Expand **GPU Operator pods** to watch the install live: the
same rows `kubectl get pods -n gpu-operator` prints on the cluster, red
while a pod is coming up or stuck and green once it's ready, with each
pod's logs one click away. A cluster that already runs the operator skips all of this.
If the GPU node still has no NVIDIA runtime afterwards (for example k3s
isn't run by systemd, so nothing restarted it), the form says so and shows
the commands to run on the node.

### Installing it yourself instead

If your platform team manages the operator, install it with the same
settings and Add GPU finds the GPU straight away. Replace the tenant ID
with yours (the default tenant of a new Daalu install is the one shown).
On k3s:

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia && helm repo update
helm upgrade --install gpu-operator nvidia/gpu-operator --version v25.10.1 \
  -n gpu-operator --create-namespace \
  --set driver.enabled=false \
  --set cdi.enabled=true \
  --set-json 'toolkit.env=[{"name":"CONTAINERD_CONFIG","value":"/etc/daalu/nvidia-toolkit-unused/config.toml"},{"name":"CONTAINERD_SOCKET","value":"/run/k3s/containerd/containerd.sock"},{"name":"RUNTIME_CONFIG_SOURCE","value":"file"}]' \
  --set dcgmExporter.serviceMonitor.enabled=true \
  --set dcgmExporter.serviceMonitor.interval=15s \
  --set-json 'dcgmExporter.serviceMonitor.relabelings=[{"action":"replace","targetLabel":"tenant","replacement":"00000000-0000-0000-0000-000000000010"}]'
```

The `toolkit.env` line is for k3s only; drop it elsewhere. Check it:

```bash
kubectl -n gpu-operator get pods      # all Running; nvidia-cuda-validator Completed
kubectl get node <gpu-node> --show-labels | tr ',' '\n' | grep -E 'nvidia.com/gpu\.(memory|product)'
#   nvidia.com/gpu.memory=49140      ← Daalu derives the GPU class from this
```

---

## Connecting the GPU

1. Open **AI Factory** (sidebar, under **Infrastructure**) and click **Add GPU**. (The same
   form is on **Usage & Pricing → Local GPU → Connect your GPU**.)
2. **Cluster** — pick the connected cluster the GPU lives in. The form
   immediately shows the **detected GPUs** on that cluster: node name,
   the derived GPU class, and how many GPUs the node exposes. Click a
   detected node to select its class — you don't need to know your node
   labels. If the cluster has no GPU Operator yet, the form first offers
   **Install GPU Operator** (see above) and shows the detected GPU when
   it's done.
3. **vLLM endpoint URL** — pre-filled from the cluster you picked; leave
   it alone unless you're pointing Daalu at a vLLM you already run
   yourself.
4. **HF token** — paste your Hugging Face read token (only needed for
   gated models, and only on first deploy — weights are cached on the
   node afterwards).
5. **Served model** — picked for you from the GPU class: the largest
   model that fits the card (for example `meta/llama-3.1-8b-instruct` on a
   16 GB card, a larger Qwen model on a 48 GB one). You can change it.
6. Click **Deploy & connect.** The button asks in place, naming the model
   and the node it will deploy onto; click **Deploy & connect** again to
   go ahead.

The request is accepted **immediately** — the deploy itself runs in the
background on your cluster, and the card flips to **Deploying…**. You can
leave the page; the status keeps updating.

### Watching it come up — the Deploy progress panel

A first vLLM boot is three sequential multi-minute waits that look
identical from the outside: pulling a ~10 GB serving image, downloading the
model weights from Hugging Face (~5 GB for Llama 3.1 8B, ~35 GB for
Qwen3.6-35B-A3B), then compiling GPU kernels. The weights land in
`/var/lib/daalu/hf-cache` on the GPU node, so a redeploy skips the download. The **Deploy progress**
panel on the Local GPU card makes each visible. Expand it to see:

- **Model pods**: the model's namespace as `kubectl get pods` shows it, red
  until ready and green after, including the *reason* when something is
  blocked (`ImagePullBackOff`, `CrashLoopBackOff`, …); click the pod for the
  vLLM logs, which follow the tail,
- the cluster events that explain a stall (the only signal that exists
  before a container has started),

The panel polls only while it's open (each poll reaches through the
tunnel into your cluster). When the stack answers its health check, the
pill flips to **Online** and the router starts using it.

> **A still-booting GPU is safe.** The router registers your endpoint up
> front and simply falls through to the next tier until it answers — a
> booting GPU behaves like a momentarily-offline one, never like an
> error in your face.

---

## Verifying it's really yours

- **Usage & Pricing** — the Local GPU pill reads **Online**, with the
  endpoint and model beside it.
- **AI Factory** (chapter 50) — the card appears with live utilisation
  and thermals once the cluster's Prometheus is connected.
- **Usage** — inference usage rows start landing in the local tier as
  the router sends traffic there (chapter 22).

Trigger something — regenerate the home-page briefing, or ask the
Assistant to triage an alert — and watch the utilisation move.

---

## More than one GPU

Connect additional cards the same way. Each physical GPU gets its own
card on the AI Factory page (cards are identified by the GPU's hardware
UUID, so two identical cards on different nodes never collapse into one
row). Different cards can serve different models — a 16 GB card for the
classifier tier and a 48 GB card for the coding model is a common
split — and the Workspace's model picker (chapter 26) reflects what is
actually serving.

---

## Disconnecting

**Disconnect GPU…** on the Local GPU card (**Usage & Pricing**), or on the
GPU's own card in **AI Factory**, removes the endpoint from the router. It
asks you to confirm in place ("Inference falls back to external models,
billed at external rates.") before anything happens. Your tenant falls back to the next configured tier (chapter
36); nothing else changes. The serving stack in your cluster is torn down
by the same controller that deployed it.

---

## Troubleshooting

**The card says Deploying… for a long time.** Expand **Deploy progress**.
The first boot's image + weight pulls genuinely take several minutes on
an ordinary uplink; as long as the events show pulls progressing, it's
working.

**`ImagePullBackOff` / `ErrImagePull` in the progress panel.** The node
can't fetch the serving image — check the cluster's egress and registry
access.

**Pod `Pending` with `Insufficient nvidia.com/gpu`.** Kubernetes isn't
advertising the GPU — the GPU Operator on the cluster isn't healthy on
that node. Check `kubectl -n gpu-operator get pods`.

**"The GPU node still has no NVIDIA runtime."** The operator's pods can't
start because containerd has no `nvidia` runtime. On k3s, k3s registers it
when it restarts: `sudo systemctl restart k3s` on the node. Elsewhere,
check the `nvidia-container-toolkit-daemonset` pod's logs. The form keeps
checking and carries on once the GPU appears.

**The operator's pods crash with `nvidia-smi` or driver errors.** The node
has no working NVIDIA driver (prerequisite 1); `nvidia-smi` on the node
tells you more.

**`CrashLoopBackOff` with CUDA/VRAM errors in the vLLM logs.** The model
doesn't fit the card. Pick a smaller model (or a quantised variant), or a
bigger card.

**Online, but AI Factory shows "No GPUs are reporting metrics yet."**
Serving and metrics are independent pipelines. Connect the cluster's
Prometheus (chapter 8) and run the **Observability check** on the AI
Factory page to find the quiet layer.

**Weights download fails.** For gated models, the HF token is missing,
wrong, or the licence wasn't accepted on Hugging Face.

---

## What's next

Chapter 36 explains how the LLM router decides which tier — your GPU,
the Daalu-hosted pool, or an external model — handles each call.

Chapter 50 is the AI Factory walkthrough — the observability home for
the GPU you just connected.

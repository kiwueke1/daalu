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
2. **On k3s only: the NVIDIA Container Toolkit on the GPU node**, then a
   k3s restart, so k3s registers the NVIDIA runtime (commands below). On
   other Kubernetes distributions the GPU Operator installs the toolkit
   itself.
3. **The cluster is connected to Daalu** via `daalu-edge` (chapter 34).
   The install snippet the cluster-onboarding wizard generates already
   includes everything the GPU path needs — the inference and telemetry
   proxies are pre-wired, and the edge registers the cluster credential
   with Daalu by itself. There is no extra manifest and no kubeconfig to
   paste.
4. **A Hugging Face token, if your model is gated.** The default Llama
   weights require accepting Meta's licence on Hugging Face and supplying
   a read token once. Ungated models (e.g. Qwen) need none.

That's it. In particular, you do **not** need to:

- install the **NVIDIA GPU Operator** — if the cluster doesn't have it,
  **Add GPU** installs it for you (next section);
- hand-label the GPU node — Daalu derives the GPU class automatically
  from what GPU discovery reports about the card;
- mint credentials or assemble a kubeconfig — the edge self-registers,
  and a rebuilt cluster replaces its own stale credential automatically;
- run any follow-up `helm upgrade` — the initial install is complete.

**The toolkit on a k3s GPU node** (prerequisite 2):

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo systemctl restart k3s          # running pods keep running
sudo grep -A3 "runtimes.'nvidia'" /var/lib/rancher/k3s/agent/etc/containerd/config.toml
#   BinaryName = "/usr/bin/nvidia-container-runtime"
```

The toolkit's `nvidia-cdi-refresh` service also writes the GPU's CDI spec
(`/var/run/cdi/nvidia.yaml`) on every boot, which is how containerd hands
the GPU to a pod. Forget this step and Add GPU tells you: the operator
install stops with "The GPU node has no NVIDIA runtime yet" and shows these
commands.

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

| Setting | Why |
|---|---|
| driver off | The driver is the node's prerequisite. |
| toolkit off **on k3s** | On k3s the toolkit lives on the host (prerequisite 2). The operator's own toolkit rewrites containerd's config with standard CNI paths that k3s doesn't use, and the node goes `NotReady`. |
| CDI on | GPUs reach pods through the CDI spec, so Daalu's vLLM pod needs only `nvidia.com/gpu: 1`, no runtime class. |
| DCGM ServiceMonitor on, if the cluster runs the Prometheus Operator | The operator ships it off, so Prometheus never scrapes GPU metrics and AI Factory stays empty. |
| `tenant` label on the DCGM series | AI Factory only shows series labelled with your tenant; Daalu fills in your tenant's ID. |

The form shows how far along it is ("3 of 8 pods ready") and moves on to
the detected GPU by itself, usually 2–5 minutes later. A cluster that
already runs the operator skips all of this.

### Installing it yourself instead

If your platform team manages the operator, install it with the same
settings and Add GPU finds the GPU straight away. Replace the tenant ID
with yours (the default tenant of a new Daalu install is the one shown):

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia && helm repo update
helm upgrade --install gpu-operator nvidia/gpu-operator --version v25.10.1 \
  -n gpu-operator --create-namespace \
  --set driver.enabled=false \
  --set toolkit.enabled=false \
  --set cdi.enabled=true \
  --set dcgmExporter.serviceMonitor.enabled=true \
  --set dcgmExporter.serviceMonitor.interval=15s \
  --set-json 'dcgmExporter.serviceMonitor.relabelings=[{"action":"replace","targetLabel":"tenant","replacement":"00000000-0000-0000-0000-000000000010"}]'
```

(`toolkit.enabled=false` is for k3s; drop it elsewhere.) Check it:

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

- the pod, its phase, and each container's state — including the *reason*
  when something is blocked (`ImagePullBackOff`, `CrashLoopBackOff`, …),
- the cluster events that explain a stall (the only signal that exists
  before a container has started),
- the vLLM logs, streamed in once the server process is up.

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

**"The GPU node has no NVIDIA runtime yet."** The operator install can't
start its pods because the node has no NVIDIA runtime: on k3s, install the
Container Toolkit on the node and restart k3s (prerequisite 2). The form
keeps checking and carries on once the GPU appears.

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

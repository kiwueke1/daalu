# 15. Local GPU and the LLM router

If you've ever looked at an OpenAI bill from a tool that uses
LLMs heavily, you know that "we're going to pay vendors for
every prompt" is not the only way an operations product can be
built. Daalu lets you bring your own GPU and run inference
locally, with cloud LLMs as a fallback for the small fraction of
prompts that need a model your hardware can't serve.

This chapter covers the customer-facing view: what local
inference is, when to enable it, what it costs you, and what
the router actually does. Chapter 35 in Part VI is the
operational setup guide.

---

## Why local inference

Three reasons customers turn this on:

1. **Cost.** Even at small scale, the inference cost of a
   single operator using the Assistant heavily, plus a few
   automations, plus the alert-explanation agent, is hundreds
   of dollars a month at commercial-LLM prices. A used data-
   center GPU pays for itself within months.

2. **Privacy.** With local inference, prompts and responses
   never leave your network. For some industries, this is the
   difference between "we can use this" and "we cannot."

3. **Latency.** A GPU two hops away from the daalu-api pod can
   respond in 100–300 ms. A commercial LLM round-trips closer
   to 1–3 seconds. Across thousands of small calls a day, that
   adds up.

You can use Daalu without local inference perfectly well. The
router seamlessly falls through to commercial LLMs. But if you
have GPU capacity sitting around, this is the way to spend it.

---

## What you need

The minimum local-inference setup:

- **A federated cluster** (Chapter 14). The GPU node lives
  inside one of your clusters; the router reaches it through
  the federation tunnel.
- **A GPU node** in that cluster. NVIDIA A-series, RTX 4090 or
  later, RTX 2000 Ada, or H100. Chapter 35 lists the cards and
  models we've tested; if yours isn't there, ask
  your operator before you buy.
- **The NVIDIA GPU Operator** on that cluster — the standard
  way to expose NVIDIA GPUs to Kubernetes. Most GPU clusters
  run it already.

That's the whole list. You do *not* deploy the serving stack
yourself: from **AI Factory → Add GPU** (or **Connect your GPU**
on *Usage & Pricing*), Daalu discovers the card, deploys an open vLLM serving
stack with open model weights (Llama 3.1 and Qwen families are
the tested defaults), and wires the routing — showing you the
deploy live as it happens. Chapter 35 walks through it
end-to-end.

---

## What the router does

The **LLM router** is a small component in the Daalu cloud that
decides, for each inference call, where to send it. The
decision is per-call, not per-tenant — the router can route some
prompts locally and others to a commercial LLM in the same
conversation.

The decision logic is roughly:

1. **Is your local GPU online?** If no, fall through to a
   commercial LLM.
2. **Does the local model support this kind of prompt?**
   Classifier calls, short-context completions, simple chat —
   yes. Long-context (>8K tokens), multi-modal (images,
   audio), specialty tasks — no.
3. **Are you over your local model's queue depth threshold?**
   To prevent one user's heavy batch from blocking another, the
   router will spill to cloud if local has too many in-flight
   requests.
4. **Is there a per-tenant override?** Overrides such as "always
   use cloud for classifier calls" are set by your operator, not
   in the app — ask your operator. *Usage & Pricing →
   Where tokens were made* shows where your calls actually went.

The result is that 80–95 % of inference goes local in a healthy
tenant, with the remainder spilling to commercial. The exact
ratio depends on what your team does with the Assistant.

---

## What you see on the Usage & Pricing page

The *Usage & Pricing* page (Chapter 22) shows:

- **Local GPU pill: Online / Deploying… / Offline**, or a note
  that no GPU is connected. The current state of your GPU.
- **Where tokens were made** — your usage split between *Local
  GPU* and the external tiers, so you can see how much ran on your
  own hardware.
- **Current period** and **30-day spend** — what you've paid.
- **Plans** — each plan's routing policy and its price per million
  tokens per tier. Useful when you're weighing a GPU against
  external usage.

---

## When local inference is and isn't worth it

Honest assessment:

### Worth it if:

- You're already running 1+ GPU nodes for other workloads —
  the Daalu vLLM pod is light, schedules alongside other GPU
  work fine on the same node.
- Your team uses the Assistant heavily — multiple operators,
  daily use, agents running on cron.
- You have privacy requirements that make cloud LLMs hard to
  justify.
- Your inference latency starts being noticed (you're calling
  the Assistant in the middle of incident response and the
  multi-second wait is friction).

### Not worth it if:

- You're a small team using the Assistant occasionally. Your
  commercial-LLM bill is going to be $20–$80/month. Buying a
  GPU isn't going to pay back.
- You don't have spare physical infrastructure and don't want
  to operate any. Some customers just don't want the
  hardware overhead.
- Your primary workload is long-context summarization (e.g.,
  digest 100k tokens of logs at a time). Local models can do
  this but it stresses VRAM; the economics of commercial
  long-context are better.

The router decision is *not* hard to reverse. You can turn local
on, evaluate for a month, turn it off if you don't like it —
nothing about your tenant changes.

---

## Model choices

Daalu's published vLLM chart supports a tested matrix of models.
At the time of writing:

- **Llama 3.1 8B Instruct (AWQ INT4)** — the default. Fits in
  one A40 / RTX 4090 / RTX 2000 Ada and serves 80+ tokens/sec.
  Good for chat, classification, summarization.
- **Llama 3.1 70B Instruct (AWQ INT4)** — needs two GPUs (or
  one with ≥48 GB VRAM). Better quality on complex reasoning;
  used when you want fewer cloud fallbacks.
- **Qwen 2.5 7B Instruct** — alternative to Llama 3.1 8B with
  similar performance characteristics, slightly different
  failure modes; some teams prefer one or the other for taste.

For the **Coding Assistant** specifically (Chapter 26), a dedicated
code-tuned model serves the workspace, and you choose it per workspace
in the create form:

- **Qwen2.5-Coder 14B (AWQ INT4)** — fits a 16 GB RTX 2000 Ada; the
  coding default.
- **Qwen2.5-Coder 32B** and **Qwen3-Coder 30B-A3B** — need a 48 GB
  RTX 6000 Ada; selectable once your operator brings that card online.

Picking a model is a single value in the Helm chart (for the router) or
the workspace create form (for the coding assistant). The router
auto-detects what model is being served and routes accordingly.

Newer models land in the chart on a rolling basis as we validate
them. Chapter 35 lists the current tested set.

---

## What you cannot do with local inference

To set expectations:

- **GPT-4-class quality on hard prompts.** Local open models
  are catching up but aren't there. The router knows this and
  falls through to GPT-4 / Claude when a prompt requires it.
- **Vision and audio.** Open vision-language models exist but
  aren't yet in our tested chart; multi-modal inference still
  goes to a cloud provider.
- **Fine-tuning your own model.** The chart serves base
  models. Custom-trained variants aren't supported at the
  customer level yet — talk to us if this matters.

---

## What's next

Part IV starts the feature-walkthrough chapters. Chapter 16
covers the Landing page in detail. The remaining chapters in
Part IV go page-by-page through the operator app.

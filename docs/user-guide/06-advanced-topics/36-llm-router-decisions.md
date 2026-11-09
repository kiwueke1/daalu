# 36. How the LLM router decides

This chapter is for the curious — the customer who wants to
know exactly what happens when they ask the Assistant a
question. None of it is necessary for daily use; the router
is invisible. But if you're sizing hardware, evaluating
inference cost, or debugging a "why did this go to cloud?"
question, this is the chapter.

---

## The router's job

For each inference call, the router decides:

1. **Where** to send it (local pod, cloud provider X, cloud
   provider Y).
2. **Which model** to use within the chosen destination.

The decision is per-call, not per-conversation. A single chat
can have some turns routed local and others to cloud,
depending on the size and shape of each.

---

## Inputs to the decision

The router considers:

- **Local GPU online?** A heartbeat probe runs every 60
  seconds. If the last probe failed, "local" is off the
  table.
- **Local model capabilities.** What model is the local pod
  serving? What max context does it handle? What
  modalities?
- **Request size.** Number of input tokens estimated by
  tokenizing. If the request exceeds local's max context,
  spill to cloud.
- **Request modality.** Text only? Includes images? Audio?
  Local models today are text-only; multi-modal goes to
  cloud.
- **Tier.** Daalu classifies each request as `classifier`,
  `chat`, `chat-large`, or `embed`. Each tier has its own
  routing rules.
- **Per-tenant overrides.** Admins can pin routes (e.g.,
  "always use cloud for chat-large").
- **Queue depth.** If the local pod has >N requests in
  flight, the router spills to cloud to keep latency
  predictable.

---

## The decision logic, simplified

```
function route(request):
    if not local_online:
        return choose_cloud(request)

    if request.modality != "text":
        return choose_cloud(request)

    if request.tokens_in > local_max_context:
        return choose_cloud(request)

    if local_queue_depth > local_queue_max:
        return choose_cloud(request)

    if tenant_override(request.tier) == "cloud":
        return choose_cloud(request)

    return local
```

`choose_cloud` then picks among configured cloud providers
based on price/quality/rate-limit availability for the tier.

---

## What goes local

In a typical tenant:

- **All classifier calls** — short prompts, fits in 8B's
  context, classifier accuracy is fine.
- **Most chat turns** — operator-style questions. 8B is
  surprisingly capable here.
- **Most agent reasoning** — daily briefings, alert
  explanations, drift summaries.
- **Embeddings** — local pod can serve sentence-transformers
  alongside the LLM.

---

## What goes to cloud

In the same tenant:

- **Long-context summarization** — "summarize these 100k
  tokens of logs." Local 8K context can't do it.
- **Multi-step reasoning on complex prompts** — sometimes
  8B isn't smart enough; the router defers to a stronger
  cloud model when it detects this (via a small classifier
  that pre-scores prompt complexity).
- **Multi-modal** — image / audio.
- **When local is down** — the fallback path during
  outages.

---

## Cloud provider selection

When the router decides cloud, it picks among the providers
Daalu has configured for the tenant:

- **Anthropic Claude.** Default for complex reasoning.
- **OpenAI.** Default for multi-modal, embeddings fallback.
- **Other** — depending on customer agreements.

The choice is influenced by:

- **Tenant-level preference.** Admins can pin to one
  provider.
- **Real-time rate-limit status.** If one provider is
  throttling, switch.
- **Cost per tier.** Cheaper provider preferred when
  capability is equivalent.

This is invisible to the user. The Assistant's response
mentions which model answered if you ask; the routing choice
itself isn't shown on the reply.

---

## Per-tenant overrides

Routing is set per organisation by the plan you're on.
Each plan card on *Usage & Pricing → Plans* states its routing
policy:

- **Local GPU first; external models for hard questions.**
- **Local first for everything; external on miss.**
- **External models only, no local routing.**
- **Your own GPU, federated cluster.**

Switching plan changes routing from that moment; the
**Switch to …** button asks you to confirm in place first.
There are no per-user or per-tier overrides in the app; if you
need a policy none of the plans offers, talk to Daalu
support.

---

## Latency breakdown

For a typical chat call:

- **Local:** 50 ms network + 200 ms compute = 250 ms total.
- **Cloud:** 200 ms network + 800 ms compute = 1000 ms total.

Latency is the most-felt benefit of local. Cost is the
most-quantified one.

---

## Cost breakdown

For a typical operator team (10 engineers, daily Assistant
use):

- **All-cloud:** $200–600/month commercial LLM bill.
- **Local + cloud fallback:** $20–60/month commercial bill +
  the amortized cost of one GPU.
- **A used RTX 4090** at $1,200 pays back in 3–6 months.
- **A new A100 80GB** at $15,000 doesn't make economic sense
  unless you're running the 70B model and serving many teams.

These are rough. Your mileage varies.

---

## How to inspect routing decisions

Two ways:

- **Aggregate.** *Usage & Pricing* shows **Where tokens were
  made** (local GPU vs external) and **Top sources this
  period** for the current billing period.
- **Per-call.** Ask the Assistant which model answered.

---

## When the router gets it wrong

It will happen. The 8B local model gives a poor answer; you
re-ask the same question expecting better. The router
considers it a fresh call and may route local again.

For systematic problems, move to a plan whose routing policy
sends more work to external models (*Usage & Pricing →
Plans*), or connect a bigger local GPU so a larger model can
serve locally (Chapter 35).

---

## What's next

Chapter 37 covers Personal Access Tokens — the durable
credential for scripts, CI, and the automation API.

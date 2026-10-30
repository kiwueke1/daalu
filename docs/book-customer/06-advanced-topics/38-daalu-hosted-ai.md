# 38. The Daalu-hosted AI tier

Chapter 35 (Local GPU) is about *your own* hardware. Chapter 36
(LLM router) explains how Daalu picks between local and cloud LLMs.
This chapter is about the third option — letting **Daalu** host the
GPU and selling you inference on it.

If you don't want to buy a GPU and you don't want your prompt data
flowing through Anthropic or OpenAI, this is the option you take.

---

## What it is

Daalu Automation operates a small pool of GPUs in our own cluster
running open-weight models (Llama 3.1 8B for chat, Qwen 2.5 7B Coder
for the coding assistant). When you enable the daalu-hosted tier on
your tenant, all your inference calls that would otherwise have gone
to a cloud LLM get routed to this pool instead.

You pay per million tokens, the same way you'd pay Anthropic — but
about an order of magnitude less, because the underlying COGS is
just amortised GPU + electricity, not a third-party vendor margin.

---

## When this tier is the right choice

Pick daalu-hosted when:

* **You don't want a GPU on premises.** Buying, racking, and
  maintaining a card is real work. The daalu-hosted tier lets you
  skip it.
* **You don't want your prompts going to a public LLM vendor.**
  Maybe legal flagged Anthropic's data-handling clause. Maybe you
  have a compliance constraint that says "no third-party AI." Daalu
  runs the GPU; we're already in your trust boundary as your
  ops-platform vendor, so the daalu-hosted tier doesn't expand it.
* **Your usage is bursty.** Buying a GPU only makes sense if you
  use it consistently. If your AI calls are quiet most of the time
  with occasional spikes, you pay-as-you-go on daalu-hosted and
  never pay for idle silicon.
* **You want to A/B test against your own GPU.** Run the same
  prompts through both, compare quality and cost.

Stick with **your own GPU** (Chapter 35) when:

* You have volume — past about 5 million tokens/month, owning the
  hardware wins outright on unit economics.
* Your prompts contain data that legal won't even let Daalu see.
  In that case the *only* acceptable tier is your own GPU
  (Chapter 36).
* You already bought the card.

Stick with **cloud LLMs** when:

* You need a frontier-quality model (Claude Sonnet, GPT-4). Daalu's
  open-weights catalogue is ~Llama 3.1 8B class; it's good, but it's
  not Sonnet.
* You're using a specific provider feature (artifact APIs, tool
  use forms that haven't been ported to OpenAI-compatible APIs).

The three tiers are not mutually exclusive — you can enable
daalu-hosted *and* keep cloud as a fallback. The router prefers the
cheapest available tier per call.

---

## How it's billed

Two line items on the billing page, both per million tokens:
prompt (input) and completion (output). The per-token rates are
set by whoever operates the hosted tier.

Your billing page (Chapter 22) shows daalu-hosted
spend broken out by source — "chat", "classifier", "coding" — so you
can see which feature is burning the budget.

---

## Quotas and rate limits

When we turn on the tier for your tenant, we set:

* A **monthly token cap** — your plan's included tokens, plus any
  overage policy you've configured.
* A **per-minute rate limit** — defaults to 60 requests/min and
  50,000 tokens/min. High enough that you never notice it during
  normal use; tight enough that a runaway loop in one of your agents
  can't burn your whole month in five minutes.

If you hit the monthly cap, three things can happen depending on
your **overage policy**:

* `hard_stop` — your daalu-hosted calls 429. The router does *not*
  automatically fall through to cloud. Useful when you've set a
  strict budget. Default for trial plans.
* `throttle` — your calls succeed but get slower (we throttle to
  ~1 token/sec). Lets a non-critical workflow finish without paying
  for cloud overage. Default for budget plans.
* `cloud_overflow` — overage requests fall through to cloud (and
  you pay the cloud passthrough rate). Default for production plans
  where AI uptime matters more than the few dollars of overflow.

You can switch policies any time on `/settings → AI tier`. The
change takes effect on the next call.

---

## Data handling

We treat the daalu-hosted tier the same as the rest of our platform:

* **Prompts and completions are not persisted by default.** vLLM
  holds them in memory long enough to answer; our gateway holds them
  long enough to relay; neither writes them to disk.
* **Token counts are persisted.** The `usage_events` row gives us
  exactly what you used and what it cost. We need this to bill you.
  We do *not* keep the prompt or completion text alongside it.
* **The audit log records** *that* a call happened, the tenant,
  the user, the source (which agent or feature triggered it), the
  model, the token counts, and the latency. No prompt body.
* **Opt-in full capture.** If you want full prompt+completion
  capture for debugging, turn on `tenant.audit.full_capture` and
  point it at an S3 bucket you control. Captures go straight there,
  not to Daalu's database.

We never use customers' prompts for training, fine-tuning, or
anything else.

---

## Enabling the tier

Your operator can turn it on for your tenant, or — if self-serve
is allowed — toggle it in `/settings → AI tier`:

1. Click **Enable Daalu-hosted inference**.
2. Pick a **monthly limit**.
3. Pick an **overage policy** (see above).
4. Save.

Within about 60 seconds your next inference call will start using
the daalu-hosted pool. Watch the **Billing page → "Source" panel**
to confirm — calls that previously showed `cloud:anthropic` will
start showing `local` (because the daalu-hosted pool is the LOCAL
tier from the router's point of view; see Chapter 36's terminology
note).

---

## When the pool is saturated

We over-provision capacity, but unlike cloud LLMs we don't have
infinite scale. If the pool is saturated:

* Your rate-limit bucket is unaffected — you're not getting denied
  for being a heavy user.
* Latency increases (vLLM continuous-batching queues requests). You
  might see p99 chat-completion latency rise from ~2 s to ~5 s for a
  few minutes.
* If you've set `overage_policy=cloud_overflow`, the router skips
  the slow tier and goes straight to cloud for the saturated period.

We alert internally on sustained saturation and add capacity. You
don't have to do anything.

---

## Limitations

A few things to know:

* **No streaming yet.** The first release of the tier returns
  responses in one shot; no token-by-token SSE. Streaming is on the
  near-term roadmap. (Cloud and your-own-GPU tiers both stream.)
* **No custom models.** We run a fixed catalogue. If your workflow
  needs a model we don't have, your own GPU is
  the path.
* **One region.** All daalu-hosted GPUs live in one data centre
  today. For users in distant geographies expect ~100 ms more
  latency than a local GPU would give. Multi-region is on the
  roadmap.

---

## See also

* Chapter 35 — Local GPU and SKU selection (your own hardware)
* Chapter 36 — How the LLM router decides
* Chapter 22 — Billing and spend (where AI cost shows up)
* Chapter 26 — The Coding Assistant (uses this tier exclusively when
  your tenant doesn't have its own GPU)

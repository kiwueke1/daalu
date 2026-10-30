# 33. The classifier and routing decisions

The "classifier" SKU is a high-volume, small-prompt LLM use
case used for routing decisions throughout the platform.
This chapter explains what classifiers do and why they're
their own SKU.

---

## What a classifier is

A classifier call asks an LLM to map a piece of input to one
of a small set of output labels. Examples:

- "Is this alert about a database or a network issue?"
- "Does this prompt need a long-context model?"
- "Is this event likely benign or suspicious?"
- "What's the right team to route this to?"

The prompt fits in ~500 tokens; the output is one of a fixed
set of strings. The work is structurally simple but
high-volume — Daalu makes thousands of classifier calls per
active user per day.

---

## Why a separate SKU

Two reasons we treat classifier as its own SKU instead of
just a "small chat" call:

1. **Different cost profile.** A classifier call uses
   ~100 input + 5 output tokens. A normal chat is 1k input +
   500 output. The cost ratio is 10× different.
2. **Different routing.** Classifier accuracy on the 8B local
   model is fine; chat accuracy on hard prompts isn't always.
   The router treats them differently — classifier almost
   always goes local even on tenants where chat sometimes
   spills.

The SKU tag makes the cost analytics on the Billing page
honest.

---

## Where classifiers are used

In Daalu's internal architecture, classifiers gate decisions
on:

### 1. LLM routing pre-check

The router runs a tiny classifier to decide whether a chat
prompt is "easy" (8B can handle) or "hard" (needs cloud
quality). This sits in `core/llm_router.py:_complexity_score`:

```python
async def _complexity_score(request: InferenceRequest) -> float:
    if request.tier == "classifier":
        return 0.0  # don't double-classify
    prompt = render_summary(request.messages)
    response = await classify(
        prompt=f"Score the difficulty of this question 1-10. {prompt}",
        labels=["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"],
    )
    return int(response) / 10.0
```

Score > 0.7 → spill to cloud.

### 2. Alert routing

When an alert is being assigned to a team:

```python
team = await classify(
    prompt=f"What team owns this alert? Labels: {labels}",
    labels=teams_list,
)
```

### 3. Drift severity

After a reconciler detects drift:

```python
severity = await classify(
    prompt=f"How severe is this drift? {diff_summary}",
    labels=["info", "warning", "critical"],
)
```

The classifier's output becomes the proposal's priority.

### 4. Spend anomaly classification

When the cost-anomaly agent fires:

```python
cause = await classify(
    prompt=f"Which cloud-cost cause best fits this spike? {context}",
    labels=causes_catalog,
)
```

---

## How classify() works internally

The `classify()` function lives in `core/llm_router/classifier.
py`:

```python
async def classify(prompt: str, labels: list[str]) -> str:
    response = await llm_call(
        tier="classifier",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20,
        temperature=0.0,
    )
    text = response.content.strip().lower()
    for label in labels:
        if label.lower() in text:
            return label
    return labels[0]  # default to first label on no match
```

Simple, transparent. The constrained generation pattern
(force the model to output one of N labels) is implemented
via prompt engineering rather than logits-based constraints
— we found it's robust enough at the 8B scale and avoids
vendor-specific machinery.

---

## Performance characteristics

A typical classifier call:

- Local: 30–80 ms.
- Cloud: 400–800 ms.

At Daalu's volumes, the latency difference compounds.
Classifier calls are the strongest argument for local
inference — they're the volume drivers.

---

## Accuracy

We measure classifier accuracy by sampling and human-grading
a subset. The 8B model on the classifiers we currently use
hits ~95 % accuracy. Cloud GPT-class models hit ~98 %.

The 3-point gap matters less than you'd think because:

- Classifier outputs feed into downstream decisions that
  often have their own fallbacks.
- We can tune the labels and prompt to skew toward
  "conservative" (e.g., escalate to a stronger model when
  unsure).

For the small set of classifiers where higher accuracy
matters (e.g., the security-event classifier), we pin them
to cloud in `Settings → LLM router`.

---

## Adding a new classifier

`core/llm_router/classifier.py:CLASSIFIERS` is a dict of
named classifier specs:

```python
CLASSIFIERS = {
    "alert.routing.team": ClassifierSpec(
        prompt_template="...",
        labels_provider=lambda ctx: ctx.tenant_teams,
        cache_ttl=300,
    ),
    "drift.severity": ClassifierSpec(
        prompt_template="...",
        labels=["info", "warning", "critical"],
        cache_ttl=60,
    ),
    # ...
}
```

Adding one is straightforward: define the spec, call
`classify(name="...", **inputs)` where you need it.

We cache classifier outputs aggressively when the input is
stable (the same alert text won't change classification
between calls).

---

## What's next

Part VII starts the observability deep dive. Chapter 37 —
kube-prometheus-stack configuration.

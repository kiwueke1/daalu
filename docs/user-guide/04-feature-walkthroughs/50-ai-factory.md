# 50. AI Factory — GPU observability and operations

**AI Factory** in the sidebar, under *Infrastructure*
(`https://ops.example.com/ai-factory`), is the
window into the GPUs that serve your AI: live utilisation, thermals,
memory, and power for every card, plus the "factory operations" — health
diagnostics, benchmarks, and reliability checks — you run against the
fleet.

If your tenant runs on its own GPU (chapter 35) this page is that
hardware's cockpit. If you consume a shared GPU pool, you see the cards
that serve you.

---

## The overview

The overview is deliberately small: a health pill, the GPU list, and a
launcher — everything detailed lives one click deeper, so one noisy card
can't clutter the whole floor.

- **Health pill** — Healthy / Degraded / Critical, computed from the worst
  signal across your GPUs. It only appears once at least one GPU is
  reporting.
- **Your GPUs** — one card per physical GPU. Multiple GPUs — including
  single cards spread across different nodes or clusters — each get their
  own card, keyed by the card's hardware UUID. Each shows model,
  utilisation, temperature, memory, and power at a glance.
- **Factory operations** — four tiles that launch fleet-wide checks, each
  with a small last-run pill. The first two have a run shortcut
  (**Run health check**, **Run self-check**); starting one shows a short
  message saying where the results will appear:
  - **Diagnostics** — GPU health diagnostics (device-level checks and
    multi-GPU communication tests).
  - **Observability check** — a self-test of the metrics pipeline itself:
    is every layer between the GPU and this page actually flowing?
  - **Benchmark** — drive a load test against your serving endpoint and
    measure throughput and latency.
  - **Reliability** — the fleet's error posture over time.

  Results always open on their own page — tiles show status only.

Click a GPU card to drill into it: metric cards, time-series charts
(Utilisation / Temp / Memory / Power over selectable ranges), hardware
error counters (**Driver errors (XID)**, **Memory errors (ECC)**), firing
alerts, and that
card's reliability history.

---

## Where the numbers come from

GPU metrics are collected by the standard NVIDIA telemetry exporter
(DCGM) running **next to the GPU** — in whichever cluster the card lives —
and land in **that cluster's own Prometheus**. Daalu reads them from your
Prometheus over your cluster tunnel, the same tenant-scoped data plane the
Dashboards page uses. Your GPU telemetry never routes through anyone
else's store.

Two consequences worth knowing:

- **"No GPUs are reporting metrics yet" is about metrics, not serving.**
  A GPU can be deployed and serving inference while this page is empty —
  the metrics pipeline is independent. The empty state says exactly that,
  and the **Observability check** tile is the tool for finding which layer
  is quiet (exporter not scraped, series not labelled for your tenant,
  Prometheus not connected).
- **Connect Prometheus for this page to work.** If you haven't wired the
  cluster's Prometheus (chapter 8), there's nothing for Daalu to read.

---

## Deploy status vs. metrics

Whether your GPU stack is *deployed and healthy* lives on **Usage &
Pricing → Local GPU** (chapter 35), including a live deploy-progress panel
during installs. AI Factory is the *observability* view of the same
hardware. The two agree — they share one health probe — but they answer
different questions: "is it up?" versus "how is it doing?".

---

## See also

- Chapter 35 — connecting your own GPU (zero-touch onboarding).
- Chapter 15 — the LLM router and where inference runs.
- Chapter 48 — Dashboards (the same tenant data plane, for everything
  else).

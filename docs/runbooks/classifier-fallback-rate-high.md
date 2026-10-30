# ClassifierFallbackRateHigh

> More than 30% of classifier calls fell back from local.

## Why it matters

The local NIM is *reachable* (or this would be `LocalGpuOffline`) but
30%+ of requests fail on it. Cost climbs because each failure becomes
an Anthropic call.

## Investigate

```bash
# Per-failure reason — the metric label tells you what blew up
sum by (reason) (rate(daalu_llm_fallbacks_total{failed_tier="local"}[15m]))
```

Common reasons:
* `httpx.ConnectError` — tunnel flapping. Check WG handshake age on
  the GPU node.
* `RuntimeError` / `BadRequestError` — VRAM exhaustion. Reduce
  time-slicing replicas or shrink `max_tokens` on the noisiest caller.
* `TimeoutError` — model is loaded but inference is queueing. Check
  NIM `nim_queue_size` on the GPU dashboard.

## Resolve

* VRAM exhaustion → see `local-gpu-offline.md` "OOM" section.
* Tunnel flapping → restart `wg-quick@wg0` on the GPU node.
* Queue saturation → temporarily double the time-sliced replicas (if
  you have a 48 GB card you can absorb it) or accept the fallback
  cost for the duration of the spike.

## Escalate

If sustained >2h, consider it a capacity event and start sizing the
next GPU buy.

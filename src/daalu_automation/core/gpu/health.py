"""Is a tenant's sovereign GPU endpoint answering right now?

One probe, shared by every surface that reports GPU health, because they used
to disagree: ``/billing/local-gpu`` (the status pill) probed the endpoint,
while ``/onboarding/gpu`` (the GPU card) never computed ``healthy`` at all and
returned the field's ``False`` default. The same page therefore rendered
"Local GPU online" in the pill and "Offline" on the card, for a GPU that was
serving fine.

Cached briefly: both of those endpoints are polled every ~5s by an open
Usage & Pricing page, and each probe is a round-trip through the WireGuard
tunnel to the customer's cluster.
"""

from __future__ import annotations

import time

import httpx

# url → (monotonic stamp, healthy)
_PROBE_CACHE: dict[str, tuple[float, bool]] = {}
_PROBE_TTL_S = 15.0


async def probe_openai_endpoint(
    base_url: str, token_enc: str | None = None, *, use_cache: bool = True
) -> bool:
    """Best-effort ``GET <base_url>/models``; any failure means unhealthy.

    The stored URL is already the reachable form (tunnel IP / edge proxy) that
    onboarding validated, so no proxy resolution is needed here.

    Only ``200`` counts. Unlike the coding-upstream probe — where a ``401``
    from the gateway still proves something is routing — this endpoint is a
    raw vLLM that answers 200 or is not usable.
    """
    if not base_url:
        return False

    url = base_url.rstrip("/") + "/models"
    now = time.monotonic()
    if use_cache:
        cached = _PROBE_CACHE.get(url)
        if cached and now - cached[0] < _PROBE_TTL_S:
            return cached[1]

    headers: dict[str, str] = {}
    if token_enc:
        try:
            from daalu_automation.core.crypto import decrypt_secret

            headers["Authorization"] = f"Bearer {decrypt_secret(token_enc)}"
        except Exception:  # noqa: BLE001 — a rotated key must not read as "down"
            pass

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, headers=headers)
            ok = r.status_code == 200
    except Exception:  # noqa: BLE001 — unreachable is the answer, not an error
        ok = False

    _PROBE_CACHE[url] = (now, ok)
    return ok

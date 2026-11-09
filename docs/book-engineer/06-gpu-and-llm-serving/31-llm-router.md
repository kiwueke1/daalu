# 31. The LLM router

`core/llm_router.py`. Decides for each inference call where
to send it. Chapter 36 of the user guide covers the
user-visible behaviour; this chapter is the implementation.

---

## Inputs

The router's `route()` method receives an `InferenceRequest`:

```python
class InferenceRequest(BaseModel):
    tenant_id: UUID
    user_id: UUID | None
    tier: Literal["classifier", "chat", "chat-large", "embed"]
    modality: Literal["text", "image", "audio"]
    messages: list[Message]
    max_tokens: int = 1024
    temperature: float = 0.7
    tokens_in: int          # precomputed
```

Tokens are estimated by `tiktoken` before the call. The
estimate is fast and avoids overshoot on local model
context.

---

## Decision tree

```python
async def route(request: InferenceRequest) -> InferenceResponse:
    override = await get_tenant_override(request.tenant_id, request.tier)
    if override == Override.FORCE_CLOUD:
        return await _route_cloud(request)
    if override == Override.FORCE_LOCAL:
        return await _route_local_strict(request)  # fails if local down

    local_status = await get_local_status(request.tenant_id)
    if not local_status.online:
        return await _route_cloud(request)
    if request.modality != "text":
        return await _route_cloud(request)
    if request.tokens_in > local_status.max_context:
        return await _route_cloud(request)
    if local_status.queue_depth > MAX_QUEUE_DEPTH:
        return await _route_cloud(request)

    try:
        return await _route_local(request)
    except LocalInferenceError as exc:
        log.warning("local.inference.error", exc=str(exc))
        return await _route_cloud(request)
```

The order is important. Tenant override comes first because
operators sometimes need an emergency cloud-only mode and
don't want to wait for local-online detection.

---

## Local routing

```python
async def _route_local(request: InferenceRequest) -> InferenceResponse:
    endpoint = await get_local_endpoint(request.tenant_id)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{endpoint}/v1/chat/completions",
            json=request.to_openai_format(),
        )
        r.raise_for_status()
        data = r.json()
    await record_usage_event(request, "local", "local", data)
    return InferenceResponse.from_openai(data)
```

`endpoint` is `http://<tunnel-ip>:8000` for the cluster's
edge tunnel. Resolved via the `cluster_tunnels` table.

---

## Cloud routing

```python
async def _route_cloud(request: InferenceRequest) -> InferenceResponse:
    provider = await choose_cloud_provider(request.tenant_id, request.tier)
    client = make_cloud_client(provider)
    resp = await client.complete(request)
    await record_usage_event(request, "cloud", provider, resp)
    return resp
```

`choose_cloud_provider` factors in:

- Tenant pinned preference (if any).
- Provider availability (recent rate-limit signals).
- Per-tier cost preference.

Cloud client implementations live in `core/llm_router/
providers/`:

- `anthropic.py` — Claude.
- `openai.py` — GPT.
- (Future) `vertex.py`, `bedrock.py`.

Each provider class implements:

```python
class ProviderClient(Protocol):
    async def complete(self, request: InferenceRequest) -> InferenceResponse: ...
    async def embed(self, text: str) -> list[float]: ...
    async def health(self) -> bool: ...
```

The router only sees the protocol; provider-specific quirks
are encapsulated in each class.

---

## Local online detection

A background task polls each tenant's local endpoint every 60
seconds:

```python
@celery_app.task(name="daalu.llm.heartbeat")
async def heartbeat_local_endpoints():
    tenants = await get_tenants_with_local()
    for tenant in tenants:
        try:
            await asyncio.wait_for(
                httpx_get(f"http://{tenant.tunnel_ip}:8000/health"),
                timeout=5,
            )
            await mark_local_online(tenant.id)
        except Exception:
            await mark_local_offline(tenant.id)
```

The result is cached in Redis with a 90-second TTL. The
router reads from cache on every call.

---

## Queue depth detection

Same pattern: a polling task reads vLLM's
`vllm:num_requests_running` metric every 30 seconds and
caches the value. The router consults the cache.

The threshold `MAX_QUEUE_DEPTH` is configurable per-tenant.
Default: 8.

---

## Usage events

Every successful inference call writes a `usage_events` row:

```python
class UsageEvent(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    tier: Mapped[str]
    sku: Mapped[str]
    destination: Mapped[str]   # 'local' or 'cloud'
    provider: Mapped[str]      # 'local', 'anthropic', 'openai', ...
    tokens_in: Mapped[int]
    tokens_out: Mapped[int]
    cost_usd: Mapped[Decimal]
    latency_ms: Mapped[int]
```

The Billing page's "local vs cloud" chart is built by
aggregating this table.

---

## Metrics

Prometheus metrics emitted:

- `llm_inference_total{destination,tier,provider}` — counter.
- `llm_inference_latency_seconds{destination,tier,provider}`
  — histogram.
- `llm_inference_errors_total{destination,tier,provider,kind}`
  — counter.

Dashboards in Grafana use these to spot trends.

---

## Future

Things on the roadmap but not implemented:

- **Cross-cloud failover.** When one cloud provider rate-
  limits, switch to another. The interface supports it but
  the heuristics are simplistic today.
- **Per-prompt classifier for routing.** A small model that
  decides whether a prompt needs a stronger model
  irrespective of size. Today we use only static heuristics.
- **Tenant-specific cloud quotas.** Cap cloud spend at a
  configurable level, fall through to local-or-fail when
  hit.

---

## What's next

Chapter 32 — NVIDIA-specific notes for both the customer-
side GPU node and the operator-side observability.

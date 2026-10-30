"""Daalu-hosted inference gateway.

A small FastAPI service that fronts the operator-owned vLLM pool
(`daalu-vllm-pool`). Every request from ``daalu-api`` carries a
short-lived service-token JWT (see ``core.service_tokens``); the
gateway:

1. Verifies the token and extracts (tenant, user, purpose).
2. Looks up the tenant's ``daalu_hosted_quotas`` row.
3. Enforces the rate limit (Redis token bucket) and the monthly
   quota; refuses or throttles per the overage policy.
4. Proxies the OpenAI-compatible request to the configured vLLM
   upstream.
5. Records a ``usage_events`` row with ``tier='local'`` (the
   operator-owned pool reuses the existing LOCAL tier — see the
   terminology note at the top of ``core/llm.py``).

This module is *not* loaded by ``daalu-api``; it has its own entry
point (``daalu inference-gateway``) and runs as a separate Deployment
in the hub cluster. ``daalu-api`` is the only legitimate caller.
"""

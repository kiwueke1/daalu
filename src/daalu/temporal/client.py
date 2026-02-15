# src/daalu/temporal/client.py

from __future__ import annotations
from temporalio.client import Client
from .settings import load_temporal_settings

async def get_temporal_client() -> Client:
    s = load_temporal_settings()
    # Temporal namespaces isolate workflow executions + task queues :contentReference[oaicite:3]{index=3}
    return await Client.connect(s.address, namespace=s.namespace)

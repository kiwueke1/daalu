# src/daalu/temporal/settings.py


from __future__ import annotations
from dataclasses import dataclass
import os

@dataclass(frozen=True)
class TemporalSettings:
    address: str
    namespace: str
    task_queue: str

def load_temporal_settings() -> TemporalSettings:
    # sensible defaults for dev; override via env
    return TemporalSettings(
        address=os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
        task_queue=os.getenv("DAALU_TASK_QUEUE", "daalu.deployments"),
    )

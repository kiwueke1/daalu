# src/daalu/config/monitoring.py

from pydantic import BaseModel, Field
from typing import Optional


class ThanosConfig(BaseModel):
    bucket: str
    endpoint: str
    access_key: str
    secret_key: str


class MonitoringConfig(BaseModel):
    thanos: Optional[ThanosConfig] = None

# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/config/monitoring.py

from pydantic import BaseModel, Field
from typing import Optional


class ThanosConfig(BaseModel):
    bucket: str
    endpoint: str
    access_key: str
    secret_key: str


class OpenSearchConfig(BaseModel):
    admin_password: str


class MonitoringConfig(BaseModel):
    thanos: Optional[ThanosConfig] = None
    opensearch: Optional[OpenSearchConfig] = None

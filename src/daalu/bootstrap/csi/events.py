# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/csi/events.py
from __future__ import annotations

from dataclasses import dataclass
from daalu.observers.events import BaseEvent


@dataclass(frozen=True)
class CSIStarted(BaseEvent):
    stage: str
    message: str


@dataclass(frozen=True)
class CSIProgress(BaseEvent):
    stage: str
    message: str


@dataclass(frozen=True)
class CSIFailed(BaseEvent):
    stage: str
    error: str


@dataclass(frozen=True)
class CSISucceeded(BaseEvent):
    stage: str
    message: str

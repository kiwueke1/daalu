# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations
from typing import Protocol
from .events import BaseEvent

class Observer(Protocol):
    def notify(self, event: BaseEvent) -> None: ...

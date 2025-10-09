# src/daalu/observers/dispatcher.py
from __future__ import annotations
from typing import List
from .events import BaseEvent

class EventBus:
    def __init__(self, observers: List = None):
        self._observers = observers or []

    def emit(self, event: BaseEvent) -> None:
        for ob in self._observers:
            try:
                ob.notify(event)
            except Exception:
                pass  # observers must not break deploys


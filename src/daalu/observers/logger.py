from __future__ import annotations
import logging
from .events import BaseEvent


class LoggerObserver:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def notify(self, event: BaseEvent) -> None:
        d = event.dict()
        etype = event.__class__.__name__
        msg = ", ".join(f"{k}={v}" for k, v in d.items() if k not in ("ts",))

        self.logger.info(f"[EVENT] {etype}: {msg}")

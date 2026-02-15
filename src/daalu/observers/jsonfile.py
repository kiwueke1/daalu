from __future__ import annotations
import json
from pathlib import Path
from .interface import Observer
from .events import BaseEvent

class JsonFileObserver(Observer):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def notify(self, event: BaseEvent) -> None:
        with self.path.open("a") as f:
            json.dump({"type": event.__class__.__name__, **event.dict()}, f)
            f.write("\n")

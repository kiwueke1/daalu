# src/daalu/observers/console.py
from .events import BaseEvent
class ConsoleObserver:
    def notify(self, event: BaseEvent) -> None:
        d = event.dict()
        k = event.__class__.__name__
        print(f"[{d['ts']}] {k} run={d['run_id']} env={d['env']} ctx={d['context']} data={{"
              + ", ".join(f"{x}={y}" for x,y in d.items() if x not in ('ts','run_id','env','context')) + "}}")
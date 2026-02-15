from dataclasses import dataclass

@dataclass(frozen=True)
class ExecutionContext:
    """
    controls how commands are executed 
    """

    dry_run: bool = False
    
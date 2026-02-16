# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass

@dataclass(frozen=True)
class ExecutionContext:
    """
    controls how commands are executed 
    """

    dry_run: bool = False
    
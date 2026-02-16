# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/infrastructure/models.py

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Set


@dataclass(frozen=True)
class InfraSelection:
    """
    Represents which infrastructure components the user wants.
    """
    components: Optional[Set[str]]  # None = all


def parse_infra_flag(infra: Optional[str]) -> InfraSelection:
    """
    Parse --infra flag.

    --infra metallb
    --infra metallb,argocd
    --infra all
    --infra None  -> all
    """
    if infra is None or infra == "all":
        return InfraSelection(components=None)

    parts = {p.strip().lower() for p in infra.split(",") if p.strip()}
    if not parts:
        return InfraSelection(components=None)

    return InfraSelection(components=parts)

# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/openstack/components/glance/images.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict


@dataclass
class GlanceImageSpec:
    """
    Declarative specification for a Glance image.

    Used by:
    - GlanceComponent
    - (future) image import tooling
    - tests / fixtures
    """
    name: str
    url: str

    container_format: Optional[str] = None
    disk_format: Optional[str] = None
    min_disk: Optional[int] = None
    min_ram: Optional[int] = None
    properties: Optional[Dict[str, str]] = None
    is_public: Optional[bool] = None
    kernel: Optional[str] = None
    ramdisk: Optional[str] = None

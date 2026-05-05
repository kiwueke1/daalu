# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/temporal/cli.py
"""
Small CLI helpers for the temporal package.

``daalu-registry``  Dumps the workflow registry as JSON to stdout. Used by
                    the temporal-console image build to bake the schemas in.
"""
from __future__ import annotations

import sys

from daalu.temporal.schemas import registry_as_json


def dump_registry() -> None:
    sys.stdout.write(registry_as_json())
    sys.stdout.write("\n")

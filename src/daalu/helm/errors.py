# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/helm/errors.py
class HelmError(RuntimeError):
    """Base class for Helm-related failures."""

class HelmDiffError(HelmError):
    """Raised when helm diff fails for non-diff reasons."""

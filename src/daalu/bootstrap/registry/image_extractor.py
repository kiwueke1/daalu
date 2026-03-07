# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

# src/daalu/bootstrap/registry/image_extractor.py

from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger("daalu")


class ImageExtractor:
    """
    Scans all assets/*/values.yaml files (and nested chart default values) for
    images.tags.* entries and returns a sorted, deduplicated list of image references.

    Also reads assets/*/extra_images.yaml files — plain YAML lists of image refs —
    for components whose charts don't use the images.tags.* convention (e.g. ArgoCD).

    Placeholder strings like CHANGE_ME_* are excluded.
    Custom/local registry images (IP:port style) are included — the caller decides
    whether to skip them.
    """

    def __init__(self, assets_dir: Path):
        self.assets_dir = assets_dir

    def extract_all(self) -> list[str]:
        """Return sorted unique image list from all values files."""
        images: set[str] = set()

        # Main values files: assets/*/values.yaml
        for path in sorted(self.assets_dir.glob("*/values.yaml")):
            images.update(self._extract_from_file(path))

        # Nested chart default values: assets/*/charts/*/values.yaml
        for path in sorted(self.assets_dir.glob("*/charts/*/values.yaml")):
            images.update(self._extract_from_file(path))

        # Explicit image lists: assets/*/extra_images.yaml
        for path in sorted(self.assets_dir.glob("*/extra_images.yaml")):
            images.update(self._extract_from_list_file(path))

        result = sorted(images)
        log.debug("[registry] Extracted %d unique images from assets/", len(result))
        return result

    def _extract_from_list_file(self, path: Path) -> set[str]:
        """Read a plain YAML list of image refs from an extra_images.yaml file."""
        found: set[str] = set()
        try:
            data = yaml.safe_load(path.read_text())
        except Exception as exc:
            log.warning("[registry] Failed to parse %s: %s", path, exc)
            return found

        if not isinstance(data, list):
            log.warning("[registry] %s must be a YAML list, skipping", path)
            return found

        for item in data:
            if not isinstance(item, str) or not item:
                continue
            if item.startswith("CHANGE_ME"):
                continue
            found.add(item)

        return found

    def _extract_from_file(self, path: Path) -> set[str]:
        """Walk data['images']['tags'] and collect non-empty string values."""
        found: set[str] = set()
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception as exc:
            log.warning("[registry] Failed to parse %s: %s", path, exc)
            return found

        if not isinstance(data, dict):
            return found

        tags = data.get("images", {})
        if isinstance(tags, dict):
            tags = tags.get("tags", {})

        if not isinstance(tags, dict):
            return found

        for value in tags.values():
            if not isinstance(value, str) or not value:
                continue
            if value.startswith("CHANGE_ME"):
                continue
            found.add(value)

        return found

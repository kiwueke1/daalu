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

    Also reads assets/*/extra_images.yaml files for components whose charts don't use
    the images.tags.* convention (e.g. ArgoCD).

    extra_images.yaml supports two formats:

      Plain list (goes to the default Harbor project):
        - docker.io/foo/bar:tag
        - quay.io/baz:tag

      Project-keyed dict (goes to the named Harbor project):
        project: myproject
        images:
          - example.io/some/image:tag

    Placeholder strings like CHANGE_ME_* are excluded.
    Custom/local registry images (IP:port style) are included — the caller decides
    whether to skip them.
    """

    _DEFAULT_PROJECT = "__default__"

    def __init__(self, assets_dir: Path):
        self.assets_dir = assets_dir

    def extract_all(self) -> list[str]:
        """Return sorted unique image list from all values files (all projects merged)."""
        images: set[str] = set()

        # Per-component merged extraction: daalu override wins over chart defaults.
        for component_dir in sorted(self.assets_dir.glob("*/")):
            if component_dir.is_dir():
                images.update(self._extract_for_component(component_dir))

        # Explicit image lists: assets/*/extra_images.yaml (all projects)
        by_project = self._extract_extra_by_project()
        for project_images in by_project.values():
            images.update(project_images)

        result = sorted(images)
        log.debug("[registry] Extracted %d unique images from assets/", len(result))
        return result

    def extract_by_project(self) -> dict[str, list[str]]:
        """
        Return images grouped by Harbor project name.

        Images from values.yaml files and plain-list extra_images.yaml go under
        the key '__default__' (caller should use registry_cfg.project for these).
        Images from project-keyed extra_images.yaml go under their named project.
        """
        by_project: dict[str, set[str]] = {}

        default = by_project.setdefault(self._DEFAULT_PROJECT, set())

        # Per-component merged extraction → default project
        for component_dir in sorted(self.assets_dir.glob("*/")):
            if component_dir.is_dir():
                default.update(self._extract_for_component(component_dir))

        # extra_images.yaml — merge into appropriate project bucket
        for project, imgs in self._extract_extra_by_project().items():
            by_project.setdefault(project, set()).update(imgs)

        return {project: sorted(imgs) for project, imgs in by_project.items() if imgs}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_extra_by_project(self) -> dict[str, set[str]]:
        """Parse all extra_images.yaml files and bucket by project."""
        by_project: dict[str, set[str]] = {}
        for path in sorted(self.assets_dir.glob("*/extra_images.yaml")):
            project, imgs = self._parse_extra_images_file(path)
            by_project.setdefault(project, set()).update(imgs)
        return by_project

    def _parse_extra_images_file(self, path: Path) -> tuple[str, set[str]]:
        """
        Parse one extra_images.yaml.
        Returns (project_name, image_set).
        project_name is _DEFAULT_PROJECT for plain lists.
        """
        found: set[str] = set()
        try:
            data = yaml.safe_load(path.read_text())
        except Exception as exc:
            log.warning("[registry] Failed to parse %s: %s", path, exc)
            return self._DEFAULT_PROJECT, found

        # Dict format: {project: "ai", images: [...]}
        if isinstance(data, dict):
            project = data.get("project") or self._DEFAULT_PROJECT
            image_list = data.get("images", [])
            if not isinstance(image_list, list):
                log.warning("[registry] %s: 'images' key must be a list, skipping", path)
                return project, found
            for item in image_list:
                if isinstance(item, str) and item and not item.startswith("CHANGE_ME"):
                    found.add(item)
            return project, found

        # Plain list format (legacy)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str) and item and not item.startswith("CHANGE_ME"):
                    found.add(item)
            return self._DEFAULT_PROJECT, found

        log.warning("[registry] %s: unsupported format (must be list or dict), skipping", path)
        return self._DEFAULT_PROJECT, found

    def _extract_from_list_file(self, path: Path) -> set[str]:
        """Read a plain YAML list of image refs from an extra_images.yaml file."""
        _, found = self._parse_extra_images_file(path)
        return found

    def _extract_for_component(self, component_dir: Path) -> set[str]:
        """
        Extract images for one component directory, merging the daalu override
        (assets/<component>/values.yaml) on top of the chart defaults
        (assets/<component>/charts/<component>/values.yaml) before extracting.

        This mirrors how Helm applies values: the override wins on key conflicts,
        so old/inaccessible images in chart defaults that have been replaced in the
        daalu override are not included in the mirror list.

        Sub-charts whose name differs from the component name have no daalu override
        and are extracted directly from their default values.
        """
        found: set[str] = set()
        component_name = component_dir.name

        # Load daalu override tags (images.tags.* from assets/<component>/values.yaml)
        override_tags: dict = {}
        override_path = component_dir / "values.yaml"
        if override_path.exists():
            override_tags = self._load_image_tags(override_path)

        # Main chart defaults: assets/<component>/charts/<component>/values.yaml
        main_chart = component_dir / "charts" / component_name / "values.yaml"
        if main_chart.exists():
            base_tags = self._load_image_tags(main_chart)
            # Override wins: any key present in override replaces the chart default
            merged = {**base_tags, **override_tags}
            found.update(self._images_from_tags(merged))
        elif override_path.exists():
            # No matching chart defaults — extract directly from the override
            found.update(self._images_from_tags(override_tags))

        # Other sub-charts (not the main chart) — no daalu override, extract as-is
        for chart_values in sorted(component_dir.glob("charts/*/values.yaml")):
            if chart_values.parent.name != component_name:
                found.update(self._extract_from_file(chart_values))

        return found

    def _load_image_tags(self, path: Path) -> dict:
        """Return images.tags dict from a values.yaml, empty dict on failure."""
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception as exc:
            log.warning("[registry] Failed to parse %s: %s", path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        tags = data.get("images", {})
        if isinstance(tags, dict):
            tags = tags.get("tags", {})
        return tags if isinstance(tags, dict) else {}

    def _images_from_tags(self, tags: dict) -> set[str]:
        """Extract non-empty, non-placeholder image strings from a tags dict."""
        found: set[str] = set()
        for value in tags.values():
            if not isinstance(value, str) or not value:
                continue
            if value.startswith("CHANGE_ME"):
                continue
            found.add(value)
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

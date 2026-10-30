"""Catalog resolution and honest reporting for the device importer.

A 12-row lab import failed every row with "unknown site / device_type / role /
platform" against a Nautobot that plainly contained the platform. Three
separate causes, each of which made the failure harder to read than it needed
to be:

* Nautobot 2.x dropped ``slug``, so the name-or-slug index could not match the
  hyphenated form operators write in a device file (``cumulus-linux`` vs the
  displayed ``Cumulus Linux``).
* An unreadable catalog returned empty lists, which is indistinguishable from
  a genuinely empty one — so a 403 got reported as the operator's file naming
  things that don't exist.
* Everything after the device POST (the primary IP) was log-only, so a device
  created without its IP was reported as a clean success.
"""

from __future__ import annotations

from daalu_automation.api.routers.sot_devices import (
    CatalogItem,
    _catalog_index,
    _slugify,
)


def test_slugify_matches_the_form_operators_write():
    assert _slugify("Cumulus Linux") == "cumulus-linux"
    assert _slugify("  NVIDIA  SN5600 ") == "nvidia-sn5600"
    assert _slugify("Border-Leaf") == "border-leaf"


def test_display_name_resolves_from_the_hyphenated_form():
    # The regression: Nautobot 2.4 Platform has no `slug`, so this used to be
    # unmatchable and every row failed "unknown platform 'cumulus-linux'".
    idx = _catalog_index([CatalogItem(id="p1", name="Cumulus Linux", slug=None)])
    assert idx["cumulus-linux"] == "p1"
    # The displayed form still resolves too.
    assert idx["cumulus linux"] == "p1"


def test_real_slug_still_wins_over_a_slugified_alias():
    # An object literally named `cumulus-linux` must beat one that merely
    # slugifies to it, or an exact match would resolve to the wrong UUID.
    idx = _catalog_index(
        [
            CatalogItem(id="display", name="Cumulus Linux", slug=None),
            CatalogItem(id="exact", name="cumulus-linux", slug=None),
        ]
    )
    assert idx["cumulus-linux"] == "exact"


def test_slug_is_indexed_when_the_nautobot_version_supplies_one():
    idx = _catalog_index([CatalogItem(id="s1", name="Lab CP03", slug="lab-cp03")])
    assert idx["lab-cp03"] == "s1"
    assert idx["lab cp03"] == "s1"


def test_unnamed_entries_do_not_create_an_empty_key():
    # A blank name slugifies to "", which must not become a catch-all key that
    # silently resolves every unmatched reference.
    idx = _catalog_index([CatalogItem(id="x", name="", slug=None)])
    assert "" not in idx


def test_natural_slug_style_values_do_not_masquerade_as_slugs():
    # Nautobot 2.x `natural_slug` carries a uniquifying hash suffix, so it can
    # never match what an operator writes. Feeding it in as `slug` must not
    # displace the slugified-name alias that actually resolves.
    idx = _catalog_index(
        [CatalogItem(id="p1", name="Cumulus Linux", slug="cumulus-linux_0ec5")]
    )
    assert idx["cumulus-linux"] == "p1"

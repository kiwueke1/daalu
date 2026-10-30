"""Naming rules for a management-environment instance.

An environment's slug is not cosmetic — it becomes a Kubernetes namespace and
a public DNS label:

    namespace      cm-<tenant>-<env>
    base hostname  <env>-<tenant>.cmtools.daalu.io
    tool hostname  <tool>-<env>-<tenant>.cmtools.daalu.io

Everything therefore has to fit in a **single** DNS label. That keeps the
existing ``*.cmtools.daalu.io`` wildcard certificate, the wildcard DNS record
and the ingress host rule working untouched — a dotted scheme
(``<tool>.<env>.cmtools…``) would break all three at once, since a wildcard
matches exactly one label (RFC 6125).

Because the slug is baked into a namespace and hostnames, it is immutable
after create: renaming would mean a new namespace and new URLs, i.e. a
rebuild rather than a rename. Validation therefore happens once, up front.
"""

from __future__ import annotations

import re

# Tool prefixes the hub's tool proxy strips from a hostname label before
# treating the remainder as the environment. Kept in sync with
# ``api/tool_proxy._TOOL_PREFIXES``.
RESERVED_PREFIXES: tuple[str, ...] = (
    "config-store",
    "nautobot",
    "render",
    "workflow",
    "temporal",
)

# Names that would be ambiguous or misleading as an environment identity.
RESERVED_SLUGS: frozenset[str] = frozenset(
    {"svc", "api", "www", "admin", "internal", "gateway", "default-default"}
)

# A DNS label: lowercase alphanumerics and hyphens, no leading/trailing hyphen.
_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# The label carries `<tool>-<env>-<tenant>`; 63 is the DNS label ceiling, so
# cap the env portion well short of it to leave room for both neighbours.
MAX_SLUG_LEN = 24


class InvalidEnvironmentName(ValueError):
    """The requested environment name cannot be used as a slug."""


def slugify(name: str) -> str:
    """Best-effort conversion of a display name to a candidate slug."""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def validate_slug(slug: str) -> str:
    """Return ``slug`` if usable, else raise :class:`InvalidEnvironmentName`.

    The reserved-prefix check is the subtle one. The tool proxy resolves a
    host by stripping a known tool prefix and treating the rest as the
    environment. An environment slugged ``render-prod`` would give the
    config-browser host ``render-prod-<tenant>.cmtools…``, which the proxy
    reads as *the render tool of an environment called ``prod``* — silently
    routing to the wrong service. Reject it at create time, which is the only
    time we can, since the slug is immutable afterwards.
    """
    if not slug:
        raise InvalidEnvironmentName("name cannot be empty")
    if len(slug) > MAX_SLUG_LEN:
        raise InvalidEnvironmentName(
            f"name is too long — {MAX_SLUG_LEN} characters maximum "
            f"(got {len(slug)})"
        )
    if not _SLUG_RE.match(slug):
        raise InvalidEnvironmentName(
            "name must use only lowercase letters, numbers and hyphens, "
            "and cannot start or end with a hyphen"
        )
    if slug in RESERVED_SLUGS:
        raise InvalidEnvironmentName(f"{slug!r} is reserved")
    for prefix in RESERVED_PREFIXES:
        if slug == prefix or slug.startswith(prefix + "-"):
            raise InvalidEnvironmentName(
                f"name cannot start with {prefix!r} — that prefix is used to "
                "route service consoles and would send traffic to the wrong "
                "service"
            )
    return slug


def slug_from_name(name: str) -> str:
    """Slugify then validate, raising with a message safe to show a user."""
    return validate_slug(slugify(name))


def namespace_for(tenant_slug: str, env_slug: str) -> str:
    return f"cm-{tenant_slug}-{env_slug}"


def base_hostname_for(tenant_slug: str, env_slug: str, base_domain: str) -> str:
    """Single-label host, so the existing wildcard cert/DNS/ingress apply."""
    return f"{env_slug}-{tenant_slug}.{base_domain}"

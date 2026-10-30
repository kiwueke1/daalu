"""Hub reverse proxy for per-tenant NV-CM tool UIs, over the WG tunnel.

A remote tenant logs into ``ops.daalu.io`` (Keycloak SSO) and clicks a
tool (Nautobot, Render, Workflow, Config-store, the config-browser UI)
on the Network-&-servers page. Those tools run on the tenant's *private*
cluster, reachable from the hub only over the WireGuard tunnel. This
module makes them reachable from a browser by terminating TLS on the hub
and reverse-proxying the request over the tunnel to the tenant's Envoy
Gateway.

URL shape (host-routed — see api/main.py mounting via Starlette ``Host``):

  https://<tool>-<env>-<tenant>.<cmtools_base_domain>/…   → tenant gateway
  https://<env>-<tenant>.<cmtools_base_domain>/…          → config-browser UI

and the pre-environment shapes, still served (they address the tenant's
**primary** environment):

  https://<tool>-<tenant>.<cmtools_base_domain>/…         → tenant gateway
  https://<tenant>.<cmtools_base_domain>/…                → config-browser UI

Everything stays inside a *single* DNS label on purpose — the hub holds one
``*.<cmtools_base_domain>`` wildcard cert and one Cloudflare wildcard record,
and a wildcard matches exactly one label (RFC 6125), so a dotted
``<tool>.<env>.<tenant>`` scheme would break cert, DNS and ingress at once.

Authentication & authorization
------------------------------
The browser is authenticated by the **hub session cookie** (``daalu_session``,
scoped to the parent domain via ``auth_cookie_domain`` so it reaches the
cmtools zone). We decode it, confirm the user's tenant owns the requested
slug, then **inject a Keycloak machine Bearer token** on the forwarded
request — reusing the *same* ``daalu-hub-nvcm`` client-credentials identity
the executor uses for the verified ``svc-nautobot /api/`` path
(``conn_from_integration_config`` + ``core.keycloak.get_machine_token``).

The tenant Envoy Gateway's SecurityPolicy is ``jwt.optional: true`` with an
OIDC fallback: a valid Bearer makes the request proceed and **OIDC never
fires**, so there is no per-host cookie-domain / redirect problem and no
per-tenant ``oidc-client-secret`` is needed. WebSocket upgrades work for the
same reason.

KNOWN TRADEOFF: every tenant user reaches the gateway under the single
service-account identity (no per-user attribution in NV-CM's audit log). This
is acceptable for the "optional, read-mostly vendor consoles" these tools are
(day-to-day changes go through Operations, which *is* per-user). A future
upgrade is Keycloak token-exchange to mint a per-user downstream token.

Transport
---------
* If the tenant's integration has a ``cluster_tunnel_id``, requests ride the
  edge forward proxy (``http://<tunnel_ip>:8888``, core/cluster_proxy) and we
  preserve the public Host via ``X-Daalu-Forward-Host`` (the edge resets Host
  otherwise). WebSocket rides a raw ``CONNECT`` through that proxy.
* Otherwise we dial ``gateway_internal_url`` directly (the hub already has a
  route to it — same as the machine path) and set ``Host`` ourselves.

The in-cluster gateway target comes from the resolved environment row
(``config_manager_tenants.gateway_internal_url``), falling back to the
``config_manager`` Integration config key of the same name for primary rows
provisioned before that column existed. The Keycloak service credentials
always come from the Integration — there is one per tenant and its client is
tenant-wide, not per environment.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocket

from daalu_automation.config import get_settings
from daalu_automation.core.auth import TokenError, decode_token
from daalu_automation.core.cluster_proxy import get_proxy_url
from daalu_automation.core.configmgr import conn_from_integration_config
from daalu_automation.core.keycloak import (
    KeycloakAuthError,
    get_machine_token,
    get_user_nvcm_token,
)
from daalu_automation.database import AsyncSessionLocal
from daalu_automation.models import ConfigManagerTenant, Integration, Tenant

logger = structlog.get_logger(__name__)

# Tool label prefixes, longest-first so ``config-store`` is matched before any
# shorter prefix that could be a substring. A bare label (no prefix) is the UI.
_TOOL_PREFIXES: tuple[str, ...] = (
    "config-store",
    "nautobot",
    "render",
    "workflow",
    "temporal",
)

# httpx decodes the body, so content-encoding/length no longer match — let
# Starlette recompute content-length from the (decoded) body we return.
_DROP_RESP_HEADERS = frozenset(
    {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"}
)
# ``authorization``/``x-forwarded-*`` are dropped because we set our own below.
# Starlette yields header names lower-cased, so leaving an incoming
# ``authorization`` in place would not be *overwritten* by ``fwd_headers
# ["Authorization"]`` — dict keys differ by case — and the gateway would receive
# two Authorization headers, letting a stale client-supplied JWT win the
# jwt_authn extraction over the fresh one we inject.
_DROP_REQ_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "cookie",
        "authorization",
        "x-forwarded-host",
        "x-forwarded-proto",
    }
)

# NV-CM's tool auth plugins (e.g. Nautobot's nv_config_manager_auth) read the
# gateway-validated JWT from this cookie (NV_CONFIG_MANAGER_JWT_COOKIE, chart
# default). With the interactive OIDC filter disabled (see render_values), the
# hub injects the service Bearer here so the tool's web UI authenticates as the
# shared daalu-hub-nvcm identity instead of bouncing to the tool's own login.
_NVCM_JWT_COOKIE = "NVConfigManagerAccessToken"


def _forward_cookie(cookie_header: str, bearer: str, hub_cookie_name: str) -> str:
    """Build the Cookie header sent upstream.

    Keep the browser's tool cookies (Nautobot's ``sessionid``/``csrftoken`` etc.)
    but drop the hub session cookie (the tenant doesn't need it), and append the
    NV-CM JWT cookie carrying the injected service Bearer.
    """
    kept = [
        part.strip()
        for part in cookie_header.split(";")
        if part.strip()
        and not part.strip().startswith(f"{hub_cookie_name}=")
        and not part.strip().startswith(f"{_NVCM_JWT_COOKIE}=")
    ]
    kept.append(f"{_NVCM_JWT_COOKIE}={bearer}")
    return "; ".join(kept)


class _ProxyUnavailable(Exception):
    """Tenant exists but its tool UI can't be reached/authed right now (→ 502)."""


class _Unauthorized(Exception):
    """No valid hub session (→ 302 to login for browsers, else 401)."""


class _Forbidden(Exception):
    """Authenticated, but not allowed to reach this tenant's tools (→ 403)."""


@dataclass(frozen=True)
class _Target:
    proxy_url: str | None   # edge forward proxy, or None for direct dial
    gateway_url: str        # in-cluster gateway base URL
    bearer: str             # Keycloak machine token to inject


def _parse_host(host: str, base_domain: str) -> tuple[str | None, str] | None:
    """Split ``<tool>-<label>.<base_domain>`` → ``(tool, label)``.

    ``label`` is either a bare tenant slug (legacy) or ``<env>-<tenant>``;
    telling those apart needs the database, so it is left to
    :func:`_resolve_label`. Returns ``(None, label)`` for the bare UI host.
    Returns ``None`` if the host isn't under ``base_domain``. Matches tool
    prefixes longest-first so ``config-store-acme`` isn't mis-split.
    """
    host = host.split(":", 1)[0].lower().rstrip(".")
    suffix = "." + base_domain.lower()
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if not label or "." in label:  # must be a single label
        return None
    for tool in _TOOL_PREFIXES:
        if label.startswith(tool + "-"):
            rest = label[len(tool) + 1 :]
            if rest:
                return tool, rest
    # No tool prefix → the bare-host config-browser UI.
    return None, label


def _claims_from_cookie(cookie_header: str) -> dict | None:
    """Decode the hub session cookie from a raw Cookie header. None if absent/bad."""
    settings = get_settings()
    prefix = f"{settings.auth_cookie_name}="
    token: str | None = None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(prefix):
            token = part[len(prefix) :]
            break
    if not token:
        return None
    try:
        return decode_token(token)
    except TokenError:
        return None


async def _primary_environment(
    db: AsyncSession, tenant_id: uuid.UUID
) -> ConfigManagerTenant | None:
    """The environment every legacy caller means by "this tenant's stack"."""
    return (
        await db.execute(
            select(ConfigManagerTenant).where(
                ConfigManagerTenant.tenant_id == tenant_id,
                ConfigManagerTenant.is_primary.is_(True),
            )
        )
    ).scalar_one_or_none()


async def _resolve_label(
    db: AsyncSession, label: str
) -> tuple[Tenant, ConfigManagerTenant | None] | None:
    """Map a host label to ``(tenant, environment)``, or ``None`` if unknown.

    Both slugs may themselves contain hyphens, so ``<env>-<tenant>`` cannot be
    split lexically — the database decides. Order matters:

    1. **The whole label as a tenant slug** wins first. That keeps every
       pre-environment host resolving exactly as it did (to the tenant's
       primary environment) even if some other tenant later happens to create
       a slug that makes the same label parse as ``<env>-<tenant>``.
    2. Otherwise try each hyphen as the boundary, **longest tenant slug
       first**. A tenant slug is globally unique while an environment slug is
       only unique within its tenant, so anchoring on the longer tenant match
       is the choice that can't silently hand traffic to a different customer.

    This function performs no authorization — the caller must check ownership
    against the tenant returned here.
    """
    tenant = (
        await db.execute(select(Tenant).where(Tenant.slug == label))
    ).scalar_one_or_none()
    if tenant is not None:
        return tenant, await _primary_environment(db, tenant.id)

    # Candidate splits: {tenant_slug: env_slug}. Each hyphen yields a distinct
    # right-hand side, so the mapping is unambiguous.
    candidates = {
        label[i + 1 :]: label[:i]
        for i, ch in enumerate(label)
        if ch == "-" and label[:i] and label[i + 1 :]
    }
    if not candidates:
        return None
    matches = (
        await db.execute(select(Tenant).where(Tenant.slug.in_(candidates)))
    ).scalars().all()
    for cand in sorted(matches, key=lambda t: len(t.slug), reverse=True):
        env = (
            await db.execute(
                select(ConfigManagerTenant).where(
                    ConfigManagerTenant.tenant_id == cand.id,
                    ConfigManagerTenant.slug == candidates[cand.slug],
                )
            )
        ).scalar_one_or_none()
        if env is not None:
            return cand, env
    return None


async def _resolve(label: str, claims: dict | None) -> _Target:
    """Authorize + resolve a host label into a forwarding target.

    Raises :class:`_Unauthorized` (no session), :class:`_Forbidden` (wrong
    tenant), or :class:`_ProxyUnavailable` (tenant/integration/tunnel not
    ready). On success returns a :class:`_Target` with a minted Bearer.
    """
    if claims is None:
        raise _Unauthorized()

    async with AsyncSessionLocal() as db:
        resolved = await _resolve_label(db, label)
        if resolved is None:
            raise _ProxyUnavailable("no such tenant")
        tenant, env = resolved
        # Tenant ownership: the session's tenant must match the host's tenant.
        # This is the only thing standing between a logged-in customer and
        # another customer's consoles, so it stays immediately after
        # resolution and before anything is read on the tenant's behalf.
        if str(claims.get("tid")) != str(tenant.id):
            raise _Forbidden()
        integ = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == tenant.id,
                    Integration.provider == "config_manager",
                )
            )
        ).scalar_one_or_none()
        if integ is None:
            raise _ProxyUnavailable("no config_manager integration for this tenant")
        cfg = integ.config or {}
        # Transport binds to the *environment* when we resolved a non-primary
        # one: it can live on a different cluster than the primary, and the
        # Integration row only ever tracks the primary. Crucially there is no
        # Integration fallback in that case — falling back would silently
        # forward a secondary environment's traffic into the primary's stack,
        # which is a wrong-stack write, not a graceful degradation. A
        # not-yet-provisioned gateway must fail closed as a 502.
        if env is not None and not env.is_primary:
            gateway_url = env.gateway_internal_url
            cluster_tunnel_id = env.target_cluster_tunnel_id
        else:
            # Primary (or a legacy tenant with no row at all): the row is
            # authoritative once stamped, else the Integration copy that
            # predates the column.
            gateway_url = (
                env.gateway_internal_url if env is not None else None
            ) or cfg.get("gateway_internal_url")
            cluster_tunnel_id = integ.cluster_tunnel_id
        proxy_url = await get_proxy_url(db, cluster_tunnel_id)

    if not gateway_url:
        raise _ProxyUnavailable(
            "config_manager integration has no gateway_internal_url "
            "(in-cluster gateway address not provisioned)"
        )
    if cluster_tunnel_id is not None and not proxy_url:
        raise _ProxyUnavailable("tenant cluster tunnel is not connected")

    try:
        conn = conn_from_integration_config(cfg)
    except KeyError as e:
        raise _ProxyUnavailable(f"integration not fully wired (missing {e})") from e
    if not conn.keycloak_client_id or not conn.keycloak_client_secret:
        raise _ProxyUnavailable("integration has no Keycloak service credentials")

    # Prefer a PER-USER downstream Bearer: token-exchange the user's stored
    # Keycloak refresh token (``krt`` claim, set at OIDC login) into an
    # nv-config-manager-audience token that carries the user's identity, so the
    # tool web UI logs them in as themselves. Fall back to the daalu-hub-nvcm
    # service identity when there's no stored token (e.g. password-login
    # sessions, or exchange failure) — see the known-tradeoff note up top.
    bearer: str | None = None
    krt = claims.get("krt")
    if krt:
        try:
            from daalu_automation.core.crypto import decrypt_secret

            bearer = await get_user_nvcm_token(
                refresh_token=decrypt_secret(krt),
                nvcm_client_id=conn.keycloak_client_id,
                nvcm_client_secret=conn.keycloak_client_secret,
            )
        except (KeycloakAuthError, ValueError) as e:
            logger.warning("tool_proxy.user_exchange_failed", label=label, error=str(e))
            bearer = None  # fall back to the service token below
    if bearer is None:
        try:
            bearer = await get_machine_token(
                client_id=conn.keycloak_client_id,
                client_secret=conn.keycloak_client_secret,
                audience=conn.audience,
            )
        except KeycloakAuthError as e:
            raise _ProxyUnavailable(f"could not mint service token: {e}") from e

    return _Target(proxy_url=proxy_url, gateway_url=gateway_url, bearer=bearer)


def _login_redirect(request: Request) -> Response:
    """302 a browser to the hub login with ?next back to this tool URL."""
    settings = get_settings()
    hub = (settings.public_base_url or "https://ops.daalu.io").rstrip("/")
    # request.url.scheme is ``http`` behind the TLS-terminating hub ingress, but
    # the cmtools tool hosts are always https externally — force it so the
    # post-login ``next`` round-trip doesn't bounce the browser to http://.
    nxt = str(request.url.replace(scheme="https"))
    return RedirectResponse(url=f"{hub}/login?next={nxt}", status_code=302)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


# ── HTTP ────────────────────────────────────────────────────────────────


async def tool_proxy_http(request: Request) -> Response:
    """Proxy an HTTP request to the tenant's gateway over the tunnel."""
    settings = get_settings()
    public_host = request.headers.get("host", "").split(":", 1)[0]
    parsed = _parse_host(public_host, settings.cmtools_base_domain)
    if parsed is None:
        return Response("not found", status_code=404)
    tool, label = parsed

    claims = _claims_from_cookie(request.headers.get("cookie", ""))
    try:
        target = await _resolve(label, claims)
    except _Unauthorized:
        if request.method in ("GET", "HEAD") and _wants_html(request):
            return _login_redirect(request)
        return Response("not authenticated", status_code=401)
    except _Forbidden:
        return Response("forbidden", status_code=403)
    except _ProxyUnavailable as e:
        return Response(str(e), status_code=502)

    base = target.gateway_url.rstrip("/")
    url = base + request.url.path
    if request.url.query:
        url = url + "?" + request.url.query

    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQ_HEADERS
    }
    fwd_headers["Authorization"] = f"Bearer {target.bearer}"
    fwd_headers["X-Forwarded-Host"] = public_host
    fwd_headers["X-Forwarded-Proto"] = "https"
    fwd_headers["Cookie"] = _forward_cookie(
        request.headers.get("cookie", ""), target.bearer, settings.auth_cookie_name
    )
    if target.proxy_url:
        # The edge proxy resets Host to the URL host; tell it to preserve ours.
        fwd_headers["X-Daalu-Forward-Host"] = public_host
    else:
        # Direct dial — set the gateway-routing Host ourselves.
        fwd_headers["Host"] = public_host

    body = await request.body()
    try:
        async with httpx.AsyncClient(
            proxy=target.proxy_url, timeout=60.0, follow_redirects=False
        ) as client:
            resp = await client.request(
                request.method, url, headers=fwd_headers, content=body
            )
    except httpx.HTTPError as e:
        logger.warning("tool_proxy.transport_error", error=str(e), label=label, tool=tool)
        return Response("tool unreachable", status_code=502)

    out = Response(content=resp.content, status_code=resp.status_code)
    # multi_items() + append so duplicate Set-Cookie headers are preserved
    # (a dict would collapse them to the last one).
    for k, v in resp.headers.multi_items():
        if k.lower() not in _DROP_RESP_HEADERS:
            out.headers.append(k, v)
    return out


# ── WebSocket ───────────────────────────────────────────────────────────


async def tool_proxy_ws(scope: Scope, receive: Receive, send: Send) -> None:
    """Bridge a browser WebSocket to the tenant gateway over the tunnel.

    Rides a raw ``CONNECT`` through the edge forward proxy when the tenant is
    on a tunnel (plain-HTTP forward can't carry an Upgrade), else dials the
    gateway directly. Mirrors api/ide_proxy.py's WS bridge; injects the same
    machine Bearer as the HTTP path.
    """
    import asyncio
    import socket

    try:
        from websockets.asyncio.client import connect as ws_connect
    except ImportError:  # pragma: no cover — uvicorn[standard] provides it
        from websockets.client import connect as ws_connect  # type: ignore[no-redef]

    settings = get_settings()
    headers = dict(scope.get("headers", []))
    public_host = headers.get(b"host", b"").decode().split(":", 1)[0]
    browser = WebSocket(scope=scope, receive=receive, send=send)

    parsed = _parse_host(public_host, settings.cmtools_base_domain)
    if parsed is None:
        await browser.close(code=1008)
        return
    _tool, label = parsed

    claims = _claims_from_cookie(headers.get(b"cookie", b"").decode())
    try:
        target = await _resolve(label, claims)
    except _Unauthorized:
        await browser.close(code=1008)
        return
    except _Forbidden:
        await browser.close(code=1008)
        return
    except _ProxyUnavailable:
        await browser.close(code=1011)
        return

    gw = urlsplit(target.gateway_url)
    gw_host = gw.hostname or ""
    gw_port = gw.port or (443 if gw.scheme == "https" else 80)
    path = scope.get("path", "/")
    query = scope.get("query_string", b"").decode()
    ws_scheme = "wss" if gw.scheme == "https" else "ws"
    upstream_uri = f"{ws_scheme}://{gw_host}:{gw_port}{path}" + (f"?{query}" if query else "")
    extra = {"Host": public_host, "Authorization": f"Bearer {target.bearer}"}

    loop = asyncio.get_running_loop()
    sock: socket.socket | None = None
    if target.proxy_url:
        pu = urlsplit(target.proxy_url)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            await loop.sock_connect(sock, (pu.hostname, pu.port or 8888))
            connect_req = (
                f"CONNECT {gw_host}:{gw_port} HTTP/1.1\r\n"
                f"Host: {gw_host}:{gw_port}\r\n\r\n"
            ).encode()
            await loop.sock_sendall(sock, connect_req)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = await loop.sock_recv(sock, 1024)
                if not chunk:
                    raise OSError("edge proxy closed during CONNECT")
                buf += chunk
            if b" 200 " not in buf.split(b"\r\n", 1)[0]:
                raise OSError(f"edge CONNECT refused: {buf.split(chr(13).encode())[0]!r}")
        except OSError as e:
            logger.warning("tool_proxy.ws.connect_failed", error=str(e), label=label)
            sock.close()
            await browser.close(code=1011)
            return

    subprotocols = browser.scope.get("subprotocols", []) or []
    connect_kwargs: dict = {
        "subprotocols": subprotocols or None,
        "open_timeout": 15.0,
        "max_size": None,
    }
    if sock is not None:
        connect_kwargs["sock"] = sock
    try:
        upstream = await ws_connect(
            upstream_uri, additional_headers=extra, **connect_kwargs
        )
    except TypeError:
        # Older websockets: extra_headers instead of additional_headers.
        upstream = await ws_connect(
            upstream_uri, extra_headers=extra, **connect_kwargs  # type: ignore[call-arg]
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("tool_proxy.ws.upstream_failed", error=str(e), label=label)
        if sock is not None:
            sock.close()
        await browser.close(code=1011)
        return

    await browser.accept(subprotocol=upstream.subprotocol or None)

    async def b2u() -> None:
        try:
            while True:
                msg = await browser.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send(msg["text"])
        except Exception:  # noqa: BLE001
            return

    async def u2b() -> None:
        try:
            async for frame in upstream:
                if isinstance(frame, bytes):
                    await browser.send_bytes(frame)
                else:
                    await browser.send_text(frame)
        except Exception:  # noqa: BLE001
            return

    try:
        _done, pending = await asyncio.wait(
            {asyncio.create_task(b2u()), asyncio.create_task(u2b())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            pass

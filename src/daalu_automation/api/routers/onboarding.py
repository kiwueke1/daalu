"""Onboarding workflow API.

The UI wizard at ``/onboarding`` calls into three surfaces here:

- ``GET /onboarding/status`` — aggregate state across every integration
  in the wizard catalog plus the cluster-tunnel feature. The wizard's
  welcome screen reads this to label each step "already done" /
  "not yet".

- ``POST /onboarding/test/{provider}`` — actually verify the operator's
  credentials work *before* committing them. Slack: webhook returns 2xx.
  SMTP: TLS + AUTH + NOOP. HTTP-shaped integrations (Prometheus, Loki,
  OpenSearch, Thanos): GET a low-impact known endpoint. Kubernetes:
  decode the kubeconfig and list a single namespace. The test path is
  side-effect-free for read integrations; for Slack it posts a small
  visible "Daalu Automation handshake" message (the operator sees it
  immediately, so an invisible probe would be worse — silent failures
  are worse than visible ones).

- ``POST /onboarding/validate/{provider}`` — schema check only, no
  network. The router enforces per-provider required fields so a missing
  SMTP host shows up as a structured 422 instead of a successful PUT
  that silently does nothing later.

The wizard is the primary client but every endpoint is stable enough
for the operator to use from a shell.
"""

from __future__ import annotations

import asyncio
import json
import smtplib
import time
import uuid
from datetime import datetime
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daalu_automation.api.deps import current_admin, current_tenant_id
from daalu_automation.config import get_settings
from daalu_automation.core.gpu.health import probe_openai_endpoint
from daalu_automation.core.wireguard import VLLM_PROXY_PORT as _VLLM_PROXY_PORT
from daalu_automation.database import get_db
from daalu_automation.models import (
    ClusterTunnel,
    ClusterTunnelStatus,
    GpuTenant,
    GpuTenantState,
    Integration,
    IntegrationStatus,
    Tenant,
    User,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


# ── Wizard catalog (mirror of frontend/app/onboarding/page.tsx) ─────────
#
# Kept in sync with the wizard's STEPS array. The frontend is the visual
# source of truth; this catalog is what the backend enforces for
# validation + status. Mismatches will surface immediately because the
# wizard's "is configured?" check hits /onboarding/status.

# Per-provider required fields. Optional fields may be absent.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "slack": ("webhook_url",),
    "smtp": ("host", "port", "username", "password", "from"),
    "prometheus": ("url",),
    "loki": ("url",),
    "thanos": ("url",),
    "opensearch": ("url",),
    "pagerduty": ("api_token",),
    "kubernetes": ("kubeconfig",),
    "aws": ("access_key_id", "secret_access_key", "region"),
    "gcp": ("service_account_json", "project_id"),
    "azure": ("tenant_id", "client_id", "client_secret", "subscription_id"),
    # Nautobot SoT: BYO mode needs both. Hosted mode fills these via
    # POST /onboarding/provision-nautobot, which lands the same shape.
    "nautobot": ("url", "token_ciphertext"),
}

# Catalog order — drives the welcome screen + status response ordering.
_CATALOG: tuple[tuple[str, str | None], ...] = (
    ("slack", "slack"),
    ("email", "smtp"),
    ("prometheus", "prometheus"),
    ("loki", "loki"),
    ("thanos", "thanos"),
    ("opensearch", "opensearch"),
    ("pagerduty", "pagerduty"),
    ("kubernetes", "kubernetes"),
    ("aws", "aws"),
    ("gcp", "gcp"),
    ("azure", "azure"),
    # Nautobot SoT — the source-of-truth + device-management feature is
    # off until this is wired (BYO or hosted via /provision-nautobot).
    ("nautobot", "nautobot"),
    # Cluster step has no /integrations/config row — we count rows in
    # cluster_tunnels for "configured?" instead.
    ("cluster", None),
)


# ── Schemas ─────────────────────────────────────────────────────────────


class StepStatus(BaseModel):
    id: str
    provider: str | None
    configured: bool
    # For integration rows: "connected"/"disconnected"/"error". For the
    # cluster step: a derived rollup ("none"/"healthy"/"degraded").
    status: str
    # Cluster step uses this; integration steps leave it at 0.
    count: int = 0
    # Required fields the caller still needs to fill in. Empty when
    # the step is fully set up.
    missing: list[str] = []


class OnboardingStatusOut(BaseModel):
    steps: list[StepStatus]
    completed: int
    total: int


class TestIn(BaseModel):
    # The wizard sends the current form values directly; the server uses
    # them in preference to anything already in the DB so the test
    # reflects what the *next* save would write, not stale state.
    config: dict[str, Any] = Field(default_factory=dict)
    # When set, probe the URL through the named ClusterTunnel's edge
    # proxy — required for any URL that only resolves inside the workload
    # cluster (`*.svc.cluster.local`, RFC1918 etc.). See
    # daalu_automation.core.cluster_proxy.
    cluster_tunnel_id: uuid.UUID | None = None


class TestOut(BaseModel):
    ok: bool
    message: str
    latency_ms: int


class ValidateIn(BaseModel):
    config: dict[str, Any]


class ValidateOut(BaseModel):
    ok: bool
    missing: list[str]


# ── Status ──────────────────────────────────────────────────────────────


def _missing_for(provider: str, config: dict[str, Any]) -> list[str]:
    if provider not in _REQUIRED:
        return []
    return [k for k in _REQUIRED[provider] if not config.get(k)]


@router.get("/status", response_model=OnboardingStatusOut)
async def get_status(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    """Aggregate every wizard step's "is this configured?" state.

    Cheap: one SELECT for integrations, one COUNT for cluster_tunnels.
    Safe to call on every wizard page-load.
    """
    rows = (
        await db.execute(
            select(Integration).where(Integration.tenant_id == tenant_id)
        )
    ).scalars().all()
    by_provider = {r.provider: r for r in rows}

    cluster_count = (
        await db.execute(
            select(func.count(ClusterTunnel.id)).where(
                ClusterTunnel.tenant_id == tenant_id
            )
        )
    ).scalar_one()
    # Cluster rollup: "healthy" if every row is connected, "degraded"
    # if any non-connected, "none" if zero rows. The UI uses this to
    # decide whether the step gets the green check.
    cluster_status = "none"
    if cluster_count > 0:
        bad = (
            await db.execute(
                select(func.count(ClusterTunnel.id)).where(
                    ClusterTunnel.tenant_id == tenant_id,
                    ClusterTunnel.status != ClusterTunnelStatus.connected,
                )
            )
        ).scalar_one()
        cluster_status = "degraded" if bad else "healthy"

    steps: list[StepStatus] = []
    for step_id, provider in _CATALOG:
        if provider is None:
            steps.append(
                StepStatus(
                    id=step_id,
                    provider=None,
                    configured=cluster_count > 0,
                    status=cluster_status,
                    count=int(cluster_count),
                )
            )
            continue
        row = by_provider.get(provider)
        if row is None:
            steps.append(
                StepStatus(
                    id=step_id,
                    provider=provider,
                    configured=False,
                    status="disconnected",
                    missing=list(_REQUIRED.get(provider, ())),
                )
            )
            continue
        missing = _missing_for(provider, row.config or {})
        steps.append(
            StepStatus(
                id=step_id,
                provider=provider,
                configured=not missing,
                status=row.status.value,
                missing=missing,
            )
        )

    completed = sum(1 for s in steps if s.configured)
    return OnboardingStatusOut(steps=steps, completed=completed, total=len(steps))


# ── Validate ────────────────────────────────────────────────────────────


@router.post("/validate/{provider}", response_model=ValidateOut)
async def validate_provider(provider: str, payload: ValidateIn):
    """Pure schema check — no network, no DB.

    Lives behind auth (auto via the router's middleware gate) but doesn't
    require admin, since it doesn't mutate anything. Useful as a quick
    pre-flight before the wizard tries an actual test/save.
    """
    if provider not in _REQUIRED:
        raise HTTPException(404, f"unknown provider: {provider}")
    missing = _missing_for(provider, payload.config or {})
    return ValidateOut(ok=not missing, missing=missing)


# ── Test connection ─────────────────────────────────────────────────────


async def _test_slack(config: dict[str, Any]) -> tuple[bool, str]:
    url = config.get("webhook_url") or ""
    if not url:
        return False, "webhook_url is required"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                url,
                json={"text": ":wave: Daalu Automation onboarding test — you can ignore this message."},
            )
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        return False, f"webhook rejected: HTTP {e.response.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, f"could not POST to webhook: {e}"
    return True, "Slack accepted the test message"


def _test_smtp_sync(config: dict[str, Any]) -> tuple[bool, str]:
    host = config.get("host") or ""
    port = int(config.get("port") or 587)
    username = config.get("username") or ""
    password = config.get("password") or ""
    if not host:
        return False, "host is required"
    try:
        with smtplib.SMTP(host, port, timeout=10) as s:
            s.starttls()
            if username:
                s.login(username, password)
            # NOOP: confirms the session is healthy after AUTH without
            # actually sending mail. We deliberately do not send a
            # test email — the wizard's user may not want a spurious
            # message in the recipient's inbox.
            s.noop()
    except Exception as e:  # noqa: BLE001
        return False, f"SMTP failed: {e}"
    return True, "SMTP login succeeded"


async def _test_smtp(config: dict[str, Any]) -> tuple[bool, str]:
    return await asyncio.to_thread(_test_smtp_sync, config)


async def _http_probe(
    url: str | None,
    path: str = "/-/healthy",
    *,
    auth: tuple[str, str] | None = None,
    accept_any_status: bool = False,
    proxy: str | None = None,
) -> tuple[bool, str]:
    if not url:
        return False, "url is required"
    target = url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=10, auth=auth, proxy=proxy) as c:
            r = await c.get(target)
            if accept_any_status:
                # Some endpoints (OpenSearch root) return 200; others
                # gate even root with auth → 401. Either way means
                # "the host is reachable and speaks HTTPS".
                if r.status_code < 500:
                    return True, f"reachable ({r.status_code})"
                return False, f"server error: HTTP {r.status_code}"
            r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return False, f"probe failed: {e}"
    return True, "endpoint responded"


async def _test_prometheus(
    config: dict[str, Any], *, proxy: str | None = None
) -> tuple[bool, str]:
    # Alertmanager v2 status endpoint; works for both alertmanager and
    # vanilla Prometheus (different paths, but both 200 on /-/healthy).
    return await _http_probe(config.get("url"), "/-/healthy", proxy=proxy)


async def _test_loki(
    config: dict[str, Any], *, proxy: str | None = None
) -> tuple[bool, str]:
    auth: tuple[str, str] | None = None
    user = config.get("user") or config.get("username")
    pw = config.get("password")
    if user and pw:
        auth = (str(user), str(pw))
    return await _http_probe(
        config.get("url"), "/loki/api/v1/labels", auth=auth, proxy=proxy
    )


async def _test_thanos(
    config: dict[str, Any], *, proxy: str | None = None
) -> tuple[bool, str]:
    return await _http_probe(config.get("url"), "/-/healthy", proxy=proxy)


async def _test_opensearch(
    config: dict[str, Any], *, proxy: str | None = None
) -> tuple[bool, str]:
    auth: tuple[str, str] | None = None
    user = config.get("user") or config.get("username")
    pw = config.get("password")
    if user and pw:
        auth = (str(user), str(pw))
    # OpenSearch's root returns 200 with a cluster summary; if security
    # is on and the creds are wrong, 401 — still "reachable".
    return await _http_probe(
        config.get("url"), "/", auth=auth, accept_any_status=True, proxy=proxy
    )


async def _test_pagerduty(config: dict[str, Any]) -> tuple[bool, str]:
    token = config.get("api_token") or ""
    if not token:
        return False, "api_token is required"
    headers = {
        "Authorization": f"Token token={token}",
        "Accept": "application/vnd.pagerduty+json;version=2",
    }
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as c:
            r = await c.get("https://api.pagerduty.com/users?limit=1")
            r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return False, f"PagerDuty rejected the token: {e}"
    return True, "PagerDuty accepted the token"


def _test_kubernetes_sync(config: dict[str, Any]) -> tuple[bool, str]:
    """Parse kubeconfig + do a single list-namespaces call.

    Synchronous because the kubernetes-client is blocking; the async
    wrapper just hands it to a thread.
    """
    blob = config.get("kubeconfig") or ""
    if not blob:
        return False, "kubeconfig is required"
    try:
        import tempfile

        import yaml
        from kubernetes import client  # type: ignore
        from kubernetes import config as kconfig

        # Parse-check first so a bad YAML fails before we hit the API.
        parsed = yaml.safe_load(blob)
        if not isinstance(parsed, dict) or parsed.get("kind") != "Config":
            return False, "kubeconfig is not a valid kind=Config YAML document"

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".kubeconfig", delete=True
        ) as fh:
            fh.write(blob)
            fh.flush()
            ctx = config.get("default_context") or None
            kconfig.load_kube_config(config_file=fh.name, context=ctx)
            v1 = client.CoreV1Api()
            v1.list_namespace(_request_timeout=8, limit=1)
    except Exception as e:  # noqa: BLE001
        return False, f"kubeconfig failed: {e}"
    return True, "kubeconfig connects to the API server"


async def _test_kubernetes(config: dict[str, Any]) -> tuple[bool, str]:
    return await asyncio.to_thread(_test_kubernetes_sync, config)


# ── Cloud probes ────────────────────────────────────────────────────────


def _test_aws_sync(config: dict[str, Any]) -> tuple[bool, str]:
    """STS GetCallerIdentity — the cheapest possible 'these creds work'
    probe in AWS. Never touches a billable service."""
    try:
        import boto3  # type: ignore
    except ImportError:
        return False, "boto3 is not installed in the API image"
    try:
        sess = boto3.session.Session(
            aws_access_key_id=config.get("access_key_id"),
            aws_secret_access_key=config.get("secret_access_key"),
            aws_session_token=config.get("session_token"),
            region_name=config.get("region") or "us-east-1",
        )
        ident = sess.client("sts").get_caller_identity()
    except Exception as e:  # noqa: BLE001
        return False, f"AWS rejected the credentials: {e}"
    return True, f"AWS accepted; caller arn = {ident.get('Arn')}"


async def _test_aws(config: dict[str, Any]) -> tuple[bool, str]:
    return await asyncio.to_thread(_test_aws_sync, config)


def _test_gcp_sync(config: dict[str, Any]) -> tuple[bool, str]:
    """Parse the service-account JSON and call Compute Engine's
    project endpoint — cheapest no-side-effect GCP probe that proves
    both the key and the project_id are valid."""
    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError:
        return False, "google-auth / google-api-python-client are not installed"
    raw = config.get("service_account_json") or ""
    project = config.get("project_id") or ""
    if not raw or not project:
        return False, "service_account_json and project_id are required"
    try:
        info = json.loads(raw) if isinstance(raw, str) else raw
        creds = service_account.Credentials.from_service_account_info(info)
        svc = build("compute", "v1", credentials=creds, cache_discovery=False)
        svc.projects().get(project=project).execute()
    except Exception as e:  # noqa: BLE001
        return False, f"GCP rejected the credentials: {e}"
    return True, "GCP accepted; project metadata fetched"


async def _test_gcp(config: dict[str, Any]) -> tuple[bool, str]:
    return await asyncio.to_thread(_test_gcp_sync, config)


def _test_azure_sync(config: dict[str, Any]) -> tuple[bool, str]:
    """Acquire a token against the management.azure.com audience using
    the service principal. No state changes, no resource calls."""
    try:
        from azure.identity import ClientSecretCredential  # type: ignore
    except ImportError:
        return False, "azure-identity is not installed"
    try:
        cred = ClientSecretCredential(
            tenant_id=config.get("tenant_id"),
            client_id=config.get("client_id"),
            client_secret=config.get("client_secret"),
        )
        cred.get_token("https://management.azure.com/.default")
    except Exception as e:  # noqa: BLE001
        return False, f"Azure rejected the service principal: {e}"
    return True, "Azure issued a management.azure.com token"


async def _test_azure(config: dict[str, Any]) -> tuple[bool, str]:
    return await asyncio.to_thread(_test_azure_sync, config)


async def _test_nautobot(config: dict[str, Any]) -> tuple[bool, str]:
    """GET /api/status/ — Nautobot's lightweight health endpoint.

    Authenticates with the token the wizard just typed in, so a 200
    confirms both reachability AND that the token has at least the
    minimum scope. Tokens with no permissions still pass this — that's
    OK; the per-tenant ObjectPermission scope is verified later when
    the reconciler actually reads devices.
    """
    url = (config.get("url") or "").rstrip("/")
    token = config.get("token") or _decrypt_if_ct(config.get("token_ciphertext"))
    if not url or not token:
        return False, "url and token are required"
    s = get_settings()
    if s.cmtools_base_domain and s.cmtools_base_domain in url:
        # A *.cmtools.daalu.io host is the hub's own browser proxy for NV-CM
        # tool UIs: it authenticates with the Daalu session cookie and
        # replaces the Authorization header, so a Nautobot API token can
        # never pass through it — every test would 401 against the proxy,
        # not Nautobot. The bundled Nautobot needs no BYO setup at all.
        return False, (
            "that URL is Daalu's own browser proxy for your NV-CM tools — "
            "API tokens can't authenticate through it. Your NV-CM-bundled "
            "Nautobot is wired automatically (Managed infra → Network & "
            "servers); BYO setup is only for a Nautobot you run yourself."
        )
    try:
        async with httpx.AsyncClient(
            timeout=10,
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
        ) as c:
            r = await c.get(f"{url}/api/status/")
            r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return False, f"Nautobot rejected the request: {e}"
    return True, "Nautobot accepted the token"


def _decrypt_if_ct(ct: str | None) -> str | None:
    """Decrypt a stored ciphertext for the test path; None for plaintext.

    The wizard posts cleartext ``token``; the DB stores ``token_ciphertext``.
    Reusing one tester for both shapes keeps the merge logic in
    :func:`test_provider` simple.
    """
    if not ct:
        return None
    from daalu_automation.core.crypto import decrypt_secret

    try:
        return decrypt_secret(ct)
    except Exception:
        return None


# Dispatch table — keep ordered the same as the wizard catalog above.
_TESTERS = {
    "slack": _test_slack,
    "smtp": _test_smtp,
    "prometheus": _test_prometheus,
    "loki": _test_loki,
    "thanos": _test_thanos,
    "opensearch": _test_opensearch,
    "pagerduty": _test_pagerduty,
    "kubernetes": _test_kubernetes,
    "aws": _test_aws,
    "gcp": _test_gcp,
    "azure": _test_azure,
    "nautobot": _test_nautobot,
}


@router.post("/test/{provider}", response_model=TestOut)
async def test_provider(
    provider: str,
    payload: TestIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Probe the integration with the supplied config.

    Uses the *posted* config rather than what's currently in the DB —
    that way the wizard can test changes before saving. Falls back to
    the stored row when a field is missing in the payload (mask-aware:
    the redaction placeholder ``"***"`` is treated as "use the stored
    value").
    """
    tester = _TESTERS.get(provider)
    if tester is None:
        raise HTTPException(404, f"no tester for provider: {provider}")

    # Merge: posted values win, but ``***`` (the redaction placeholder
    # the read API returns for sensitive fields) means "use whatever's
    # in the DB" — otherwise testing a saved Slack from the wizard would
    # always fail because the password field reads ``"***"``.
    config = dict(payload.config or {})
    row = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == user.tenant_id,
                Integration.provider == provider,
            )
        )
    ).scalar_one_or_none()
    stored = (row.config if row and row.config else {}) or {}
    for k, v in list(config.items()):
        if v == "***":
            config[k] = stored.get(k, "")
    for k, v in stored.items():
        config.setdefault(k, v)

    # Resolve the edge-proxy URL if the caller wants to test through a
    # cluster tunnel. Only the HTTP-shaped observability testers accept a
    # proxy kwarg — for the rest we silently ignore the field, which keeps
    # the wizard payload uniform.
    from daalu_automation.core.cluster_proxy import get_proxy_url

    proxy = await get_proxy_url(db, payload.cluster_tunnel_id)
    proxy_aware = {"prometheus", "loki", "thanos", "opensearch"}

    started = time.monotonic()
    try:
        if provider in proxy_aware:
            ok, msg = await tester(config, proxy=proxy)
        else:
            ok, msg = await tester(config)
    except Exception as e:  # noqa: BLE001
        logger.exception("onboarding.test.crashed", provider=provider)
        ok, msg = False, f"tester crashed: {e}"
    latency_ms = int((time.monotonic() - started) * 1000)
    return TestOut(ok=ok, message=msg, latency_ms=latency_ms)


# ── Hosted Nautobot provisioning ────────────────────────────────────────


class HostedNautobotStatus(BaseModel):
    """Surface for the wizard to decide whether to offer the hosted button."""

    hosted_available: bool
    # If hosted_available is False, this carries the reason — usually
    # "platform operator has not configured managed_nautobot_url".
    detail: str = ""


class ProvisionNautobotOut(BaseModel):
    ok: bool
    message: str
    url: str = ""
    # The Nautobot Tenant slug + UUID — recorded back on the daalu
    # tenant's Integration row so a later /provision call can pick up
    # the same Nautobot Tenant rather than create a duplicate.
    nautobot_tenant_slug: str = ""
    nautobot_tenant_id: str = ""


@router.get("/nautobot/hosted-status", response_model=HostedNautobotStatus)
async def hosted_nautobot_status():
    """Tell the wizard whether hosted Nautobot is available on this deploy.

    A platform with no managed_nautobot_* settings can still serve BYO
    tenants — the wizard just hides the "Provision a hosted Nautobot"
    button. No auth (the answer is platform-level, not per-tenant);
    auth gate on the actual provision call below is what matters.
    """
    from daalu_automation.core.sot.nautobot_provisioning import is_hosted_enabled

    if not is_hosted_enabled():
        return HostedNautobotStatus(
            hosted_available=False,
            detail=(
                "this deploy is not configured for hosted Nautobot; "
                "tenants must BYO their own Nautobot URL + token"
            ),
        )
    return HostedNautobotStatus(hosted_available=True)


@router.post("/nautobot/provision", response_model=ProvisionNautobotOut)
async def provision_nautobot(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Provision a hosted Nautobot slice for the caller's tenant.

    Idempotent: if the platform's managed Nautobot already has a Tenant
    with the matching slug + an ObjectPermission named
    ``daalu-tenant-<slug>``, those are reused rather than recreated.
    A fresh APIToken is minted on every call — old ones aren't
    revoked here, so calling this twice yields two valid tokens; the
    integration row only stores the latest.

    Writes the resulting (url, token_ciphertext) into the tenant's
    ``Integration(provider="nautobot")`` row, upserting. Returns 503
    if hosted mode is disabled, 502 if the managed Nautobot itself
    rejects the call.
    """
    from daalu_automation.core.crypto import encrypt_secret
    from daalu_automation.core.sot.nautobot_provisioning import (
        ProvisioningError,
        is_controller_enabled,
        is_hosted_enabled,
        provision_tenant,
        provision_via_controller,
    )

    if not is_hosted_enabled():
        raise HTTPException(
            503,
            "hosted Nautobot provisioning is not configured on this deploy",
        )

    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        # Defensive — current_admin should have already loaded the user,
        # but a tenant row deletion between auth and here would land us
        # here.
        raise HTTPException(404, "tenant not found")

    try:
        # Prefer the per-tenant controller (each tenant gets their own
        # isolated Nautobot stack). Fall back to the legacy
        # shared-Nautobot-with-ObjectPermission-slice path only when
        # the controller isn't deployed — this keeps pre-controller
        # installs working.
        if is_controller_enabled():
            provisioned = await provision_via_controller(
                tenant_id=user.tenant_id,
            )
        else:
            provisioned = await provision_tenant(
                tenant_slug=tenant.slug,
                tenant_name=tenant.name,
            )
    except ProvisioningError as e:
        logger.error(
            "onboarding.nautobot.provision_failed",
            tenant_slug=tenant.slug,
            error=str(e),
        )
        raise HTTPException(502, str(e)) from e

    # Upsert the Integration row with the provisioned URL + token.
    settings = get_settings()
    new_cfg = {
        "url": provisioned.url,
        "token_ciphertext": encrypt_secret(provisioned.token),
        "nautobot_tenant_slug": provisioned.tenant_slug,
        "nautobot_tenant_id": provisioned.tenant_id,
        "permission_id": provisioned.permission_id,
        "hosted": True,
    }
    existing = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == user.tenant_id,
                Integration.provider == "nautobot",
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            Integration(
                tenant_id=user.tenant_id,
                provider="nautobot",
                module="infra",
                name="Nautobot (hosted)",
                status=IntegrationStatus.connected,
                config=new_cfg,
            )
        )
    else:
        # Preserve any operator-set fields outside the provisioning surface
        # (e.g., a webhook_secret_ciphertext the operator added by hand).
        merged = dict(existing.config or {})
        merged.update(new_cfg)
        existing.config = merged
        existing.status = IntegrationStatus.connected
        existing.name = existing.name or "Nautobot (hosted)"
    await db.commit()

    _ = settings  # kept for future cadence-bound provisioning quotas
    return ProvisionNautobotOut(
        ok=True,
        message=f"provisioned Nautobot tenant {provisioned.tenant_slug!r}",
        url=provisioned.url,
        nautobot_tenant_slug=provisioned.tenant_slug,
        nautobot_tenant_id=provisioned.tenant_id,
    )


# ── NV-CM (network config-management plane) provisioning ─────────────────


class ProvisionConfigManagerIn(BaseModel):
    components: dict[str, bool] | None = None
    size_profile: str = "small"
    base_hostname: str | None = None
    target_cluster_tunnel_id: uuid.UUID | None = None


@router.post("/config-manager/provision", status_code=202)
async def provision_config_manager(
    body: ProvisionConfigManagerIn,
    user: User = Depends(current_admin),
):
    """Accept an NV-CM deployment. **Async (202).**

    Drives the config-manager-controller to ``helm upgrade --install`` a
    per-tenant NV-CM release into the tenant's cluster over the tunnel (the
    controller auto-selects it). 503 if the controller isn't configured.

    Returns as soon as the controller has accepted the spec — the returned
    ``state`` is ``pending``/``provisioning``, **not** ``active``. This used to
    block server-side for up to 600 s while the edge proxy cut the connection
    at ~100 s, so every *successful* install surfaced in the browser as
    ``Failed to fetch``. Poll ``GET /onboarding/config-manager/progress``,
    which also writes the Integration row once the stack is up.

    See docs/design/nv-config-manager-integration.md §7.
    """
    from daalu_automation.core.configmgr.provision import (
        ConfigManagerProvisioningError,
        is_config_manager_controller_enabled,
        start_provision_via_config_manager_controller,
    )

    if not is_config_manager_controller_enabled():
        raise HTTPException(
            503, "config-manager provisioning is not configured on this deploy"
        )

    try:
        view = await start_provision_via_config_manager_controller(
            tenant_id=user.tenant_id,
            components=body.components,
            size_profile=body.size_profile,
            base_hostname=body.base_hostname,
            target_cluster_tunnel_id=body.target_cluster_tunnel_id,
        )
    except ConfigManagerProvisioningError as e:
        logger.error("onboarding.config_manager.provision_failed", error=str(e))
        raise HTTPException(502, str(e)) from e

    logger.info(
        "onboarding.config_manager.provision_scheduled",
        tenant_id=str(user.tenant_id),
        state=view.get("state"),
    )
    return {
        "ok": True,
        "message": "NV-CM install scheduled",
        "state": view.get("state", "pending"),
        "namespace": view.get("namespace", ""),
        "base_hostname": view.get("base_hostname", ""),
    }


def _keycloak_service_creds(existing_cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Keycloak service-client keys to merge onto the Integration config.

    The hub authenticates to every tenant's NV-CM as one shared confidential
    client (``keycloak_nvcm_client_id``). Nothing in the provision path used to
    write it, so a freshly provisioned stack had no way to authenticate and
    every tool console failed with "integration has no Keycloak service
    credentials" until someone wrote the credential by hand — and because
    deleting an environment drops the Integration row, that recurred on every
    re-provision.

    Returns ``{}`` (write nothing) when the secret isn't configured, so an
    operator-written credential is never blanked by a deployment that hasn't
    got the setting yet. Also returns ``{}`` when the stored ciphertext already
    decrypts to the configured secret: ``encrypt_secret`` is nonce-based, so
    re-encrypting unconditionally would rewrite the row on every progress poll.
    """
    from daalu_automation.core.crypto import decrypt_secret, encrypt_secret

    settings = get_settings()
    secret = settings.keycloak_nvcm_client_secret
    if not secret:
        return {}
    cfg = existing_cfg or {}
    stored_ct = cfg.get("keycloak_client_secret_ciphertext")
    if cfg.get("keycloak_client_id") == settings.keycloak_nvcm_client_id and stored_ct:
        try:
            if decrypt_secret(str(stored_ct)) == secret:
                return {}
        except Exception:  # noqa: BLE001
            # Undecryptable (key rotated, hand-written junk) — fall through and
            # replace it rather than leaving the stack unable to authenticate.
            pass
    return {
        "keycloak_client_id": settings.keycloak_nvcm_client_id,
        "keycloak_client_secret_ciphertext": encrypt_secret(secret),
    }


async def _upsert_config_manager_integration(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    urls: dict[str, Any],
    base_hostname: str,
    components: dict[str, Any],
    resolved_tunnel_id: uuid.UUID | None,
    gateway_internal_url: str | None = None,
    nautobot_token_ciphertext: str | None = None,
) -> None:
    """Write the ``Integration(provider="config_manager")`` row for a live stack.

    Called from the progress poll rather than the provision request, so the
    row lands even though the request that started the install returned long
    before the stack was ready — and so a restarted API pod still converges.
    """
    new_cfg = {
        # Machine (svc-*) URLs the ConfigManagerExecutor + SoT use.
        "config_store_url": urls.get("config_store_url", ""),
        "render_url": urls.get("render_url", ""),
        "workflow_url": urls.get("workflow_url", ""),
        "nautobot_url": urls.get("nautobot_url", ""),
        "ui_url": urls.get("ui", ""),
        # Human (browser) URLs — flat hosts served through the hub tool proxy;
        # the frontend renders these as the tool-console links.
        "ui_human": urls.get("ui", ""),
        "nautobot_human": urls.get("nautobot_human", ""),
        "render_human": urls.get("render_human", ""),
        "workflow_human": urls.get("workflow_human", ""),
        "config_store_human": urls.get("config_store_human", ""),
        "base_hostname": base_hostname,
        "components": components,
        "hosted": True,
    }
    # The Nautobot API token the SoT client / bulk import authenticate with.
    # Only set when the controller resolved one — a mid-install poll reports
    # None and must not blank a previously stamped credential.
    if nautobot_token_ciphertext:
        new_cfg["nautobot_token_ciphertext"] = nautobot_token_ciphertext
    # The in-cluster gateway address the tool proxy dials, reported by the
    # controller once Envoy Gateway has provisioned the proxy Service. Set it
    # only when the controller actually resolved one: a mid-install poll
    # returns None, and writing that would blank a value a later poll (or an
    # operator) had already put there.
    if gateway_internal_url:
        new_cfg["gateway_internal_url"] = gateway_internal_url
    existing = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.provider == "config_manager",
            )
        )
    ).scalar_one_or_none()
    new_cfg.update(
        _keycloak_service_creds(existing.config if existing is not None else None)
    )
    if existing is None:
        db.add(
            Integration(
                tenant_id=tenant_id,
                provider="config_manager",
                module="infra",
                name="Network management stack",
                status=IntegrationStatus.connected,
                config=new_cfg,
                # Dial the tenant's NV-CM over its WireGuard tunnel (the tool
                # proxy + executor resolve the edge proxy from this). NULL for
                # an operator-local cluster (reached directly).
                cluster_tunnel_id=resolved_tunnel_id,
            )
        )
    else:
        merged = dict(existing.config or {})
        merged.update(new_cfg)
        existing.config = merged
        existing.status = IntegrationStatus.connected
        existing.name = existing.name or "Network management stack"
        # Only (re)bind the tunnel when one was resolved, so a re-provision
        # that lands on the hub doesn't silently detach an existing tunnel.
        if resolved_tunnel_id is not None:
            existing.cluster_tunnel_id = resolved_tunnel_id
    await db.commit()


class ConfigManagerPodOut(BaseModel):
    name: str
    phase: str
    ready: int = 0
    total: int = 0
    reason: str | None = None


class ConfigManagerEventOut(BaseModel):
    reason: str = ""
    message: str = ""
    object: str = ""


class ConfigManagerProgressOut(BaseModel):
    requested: bool = False
    state: str = "absent"
    namespace: str = ""
    chart_version: str = ""
    last_error: str | None = None
    message: str | None = None
    pods: list[ConfigManagerPodOut] = []
    events: list[ConfigManagerEventOut] = []
    summary: dict[str, int] = {}
    urls: dict[str, Any] = {}


@router.get("/config-manager/progress", response_model=ConfigManagerProgressOut)
async def config_manager_progress(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Live NV-CM install progress — row state plus pods and notable events.

    Polled by the UI while the stack comes up, so a multi-minute install is
    legible rather than a spinner, and the failures that actually happen
    (unschedulable pods, image pulls that can't verify TLS) are visible
    without kubectl.

    This is also where the ``Integration`` row gets written: the request that
    *started* the install returns immediately (202), so the row is created on
    the first poll that observes ``active``. That keeps it correct across an
    API restart mid-install, which a background task would not.
    """
    from daalu_automation.core.configmgr.provision import (
        ConfigManagerProvisioningError,
        is_config_manager_controller_enabled,
        progress_via_controller,
    )

    if not is_config_manager_controller_enabled():
        return ConfigManagerProgressOut(requested=False)
    try:
        data = await progress_via_controller(tenant_id=user.tenant_id)
    except ConfigManagerProvisioningError as e:
        logger.warning("onboarding.config_manager.progress_failed", error=str(e))
        raise HTTPException(502, str(e)) from e
    if data is None:
        return ConfigManagerProgressOut(requested=False)

    urls = data.get("urls") or {}
    if data.get("state") == "active" and urls:
        try:
            await _upsert_config_manager_integration(
                db,
                tenant_id=user.tenant_id,
                urls=urls,
                base_hostname=urls.get("base_hostname")
                or data.get("base_hostname", ""),
                components=data.get("components") or {},
                gateway_internal_url=data.get("gateway_internal_url") or None,
                nautobot_token_ciphertext=data.get("nautobot_token_ciphertext")
                or None,
                resolved_tunnel_id=(
                    uuid.UUID(data["target_cluster_tunnel_id"])
                    if data.get("target_cluster_tunnel_id")
                    else None
                ),
            )
        except Exception:  # noqa: BLE001
            # Never fail the status poll because the bookkeeping write did —
            # the next poll retries it.
            logger.exception("onboarding.config_manager.integration_upsert_failed")

    return ConfigManagerProgressOut(
        requested=True,
        state=data.get("state", "unknown"),
        namespace=data.get("namespace", ""),
        chart_version=data.get("chart_version", ""),
        last_error=data.get("last_error"),
        message=data.get("message"),
        pods=[ConfigManagerPodOut(**p) for p in (data.get("pods") or [])],
        events=[ConfigManagerEventOut(**e) for e in (data.get("events") or [])],
        summary=data.get("summary") or {},
        urls=urls,
    )


class ConfigManagerVersionOut(BaseModel):
    provisioned: bool
    state: str = "absent"
    chart_version: str = ""
    latest_chart_version: str | None = None
    available_chart_versions: list[str] = []
    upgrade_available: bool = False
    last_error: str | None = None


@router.get("/config-manager/version", response_model=ConfigManagerVersionOut)
async def config_manager_version(
    user: User = Depends(current_admin),
):
    """Which NV-CM chart this tenant runs, and whether a newer one ships.

    Polled by the UI to decide whether to offer the upgrade button. Returns
    ``provisioned=false`` (rather than erroring) when there is no stack yet,
    so the caller can render a neutral state.
    """
    from daalu_automation.core.configmgr.provision import (
        ConfigManagerProvisioningError,
        is_config_manager_controller_enabled,
    )
    from daalu_automation.core.configmgr.upgrades import get_version_status

    if not is_config_manager_controller_enabled():
        return ConfigManagerVersionOut(provisioned=False)
    try:
        status = await get_version_status(tenant_id=user.tenant_id)
    except ConfigManagerProvisioningError as e:
        logger.warning("onboarding.config_manager.version_failed", error=str(e))
        raise HTTPException(502, str(e)) from e
    if status is None:
        return ConfigManagerVersionOut(provisioned=False)
    return ConfigManagerVersionOut(
        provisioned=True,
        state=status.state,
        chart_version=status.chart_version,
        latest_chart_version=status.latest_chart_version,
        available_chart_versions=status.available_chart_versions,
        upgrade_available=status.upgrade_available,
        last_error=status.last_error,
    )


class UpgradeConfigManagerIn(BaseModel):
    chart_version: str | None = None


@router.post("/config-manager/upgrade", status_code=202)
async def upgrade_config_manager(
    body: UpgradeConfigManagerIn,
    user: User = Depends(current_admin),
):
    """Schedule an NV-CM chart upgrade; returns 202 without waiting.

    Deliberately non-blocking: a Helm rollout of the full NV-CM stack far
    outlives the edge proxy's ~100s connection ceiling, so a synchronous
    version of this would surface every *successful* upgrade in the browser
    as a failed fetch (the same trap the GPU provisioning flow hit). The
    client polls ``GET /onboarding/config-manager/version`` for progress.
    """
    from daalu_automation.core.configmgr.provision import (
        ConfigManagerProvisioningError,
        is_config_manager_controller_enabled,
    )
    from daalu_automation.core.configmgr.upgrades import schedule_upgrade

    if not is_config_manager_controller_enabled():
        raise HTTPException(
            503, "config-manager provisioning is not configured on this deploy"
        )
    try:
        status = await schedule_upgrade(
            tenant_id=user.tenant_id, chart_version=body.chart_version
        )
    except ConfigManagerProvisioningError as e:
        logger.error("onboarding.config_manager.upgrade_failed", error=str(e))
        raise HTTPException(409, str(e)) from e
    logger.info(
        "onboarding.config_manager.upgrade_scheduled",
        tenant_id=str(user.tenant_id),
        chart_version=status.chart_version,
    )
    return {
        "ok": True,
        "message": f"upgrade to {status.chart_version} scheduled",
        "chart_version": status.chart_version,
        "state": status.state,
    }


# ── NV-CM named environments ─────────────────────────────────────────────
#
# The plural surface. The singular routes above stay the tenant's default and
# resolve the PRIMARY environment (the controller's ``_find_row`` does that
# resolution), so nothing that predates environments has to change.
#
# The ``Integration(provider="config_manager")`` row stays strictly one per
# tenant and tracks the primary only. Four consumers read it with
# ``.scalar_one_or_none()``, which raises on a second row — so
# ``_upsert_config_manager_integration`` is called only for the primary.


class ConfigManagerEnvironmentOut(BaseModel):
    id: str
    name: str = ""
    slug: str = ""
    is_primary: bool = False
    state: str = "unknown"
    namespace: str = ""
    base_hostname: str = ""
    chart_version: str = ""
    # Named so the UI can say what an upgrade would move *to*, rather than
    # offering a bare "upgrade available" with no target.
    latest_chart_version: str | None = None
    upgrade_available: bool = False
    # Per-environment, so a tile can report "N components running" without
    # reaching for the Integration row — which exists only for the primary.
    components: dict[str, Any] = {}
    last_error: str | None = None
    urls: dict[str, Any] = {}
    # Break-glass Nautobot credential. Populated only on the create response —
    # list/get never carry it, so the UI must show it at creation or not at
    # all. Stays recoverable by an operator from the row's encrypted bag.
    admin_username: str | None = None
    admin_password: str | None = None


class CreateConfigManagerEnvironmentIn(BaseModel):
    name: str
    components: dict[str, bool] | None = None
    size_profile: str = "small"
    # Break-glass local admin for Nautobot — the only NV-CM tool with its own
    # login. Everyone else is SSO'd in from their Daalu session by the tool
    # proxy and never sees a password. Both optional: the username defaults to
    # "admin" and the password is generated when blank.
    admin_username: str | None = None
    admin_password: str | None = None


def _environment_out(view: dict[str, Any]) -> ConfigManagerEnvironmentOut:
    return ConfigManagerEnvironmentOut(
        id=str(view.get("id", "")),
        name=view.get("name") or "",
        slug=view.get("slug") or "",
        is_primary=bool(view.get("is_primary")),
        state=view.get("state", "unknown"),
        namespace=view.get("namespace", ""),
        base_hostname=view.get("base_hostname", ""),
        chart_version=view.get("chart_version") or "",
        latest_chart_version=view.get("latest_chart_version"),
        upgrade_available=bool(view.get("upgrade_available")),
        components=view.get("components") or {},
        last_error=view.get("last_error"),
        urls=view.get("urls") or {},
        admin_username=view.get("admin_username"),
        admin_password=view.get("admin_password"),
    )


@router.get(
    "/config-manager/environments",
    response_model=list[ConfigManagerEnvironmentOut],
)
async def list_config_manager_environments(
    user: User = Depends(current_admin),
):
    """Every management environment this tenant runs, primary first.

    Returns an empty list (not an error) when the controller isn't configured
    on this deploy, so the UI can render a neutral empty state.

    ``destroyed`` rows are filtered out: the environment they describe has
    already been uninstalled, so listing them shows the user a tile for a
    thing that isn't there. Current controllers delete the row instead of
    parking it in that state; this keeps an older one from resurrecting a
    ghost.
    """
    from daalu_automation.core.configmgr.environments import list_environments
    from daalu_automation.core.configmgr.provision import (
        ConfigManagerProvisioningError,
        is_config_manager_controller_enabled,
    )

    if not is_config_manager_controller_enabled():
        return []
    try:
        views = await list_environments(tenant_id=user.tenant_id)
    except ConfigManagerProvisioningError as e:
        logger.warning("onboarding.config_manager.env_list_failed", error=str(e))
        raise HTTPException(502, str(e)) from e
    return [
        _environment_out(v) for v in views if v.get("state") != "destroyed"
    ]


@router.post("/config-manager/environments", status_code=202)
async def create_config_manager_environment(
    body: CreateConfigManagerEnvironmentIn,
    user: User = Depends(current_admin),
):
    """Create a named environment and queue its install. **Async (202).**

    The controller derives the slug (and from it the namespace and public
    hostnames) from ``name``, and rejects a name it can't use: 400 when it
    isn't a usable DNS label or collides with a tool-console prefix, 409 when
    the tenant already has that slug. Both details are written for the person
    who typed the name, so they are relayed **verbatim** rather than being
    flattened into a generic failure.
    """
    from daalu_automation.core.configmgr.environments import (
        ConfigManagerRequestError,
        create_environment,
    )
    from daalu_automation.core.configmgr.provision import (
        ConfigManagerProvisioningError,
        is_config_manager_controller_enabled,
    )

    if not is_config_manager_controller_enabled():
        raise HTTPException(
            503, "config-manager provisioning is not configured on this deploy"
        )
    try:
        view = await create_environment(
            tenant_id=user.tenant_id,
            name=body.name,
            components=body.components,
            size_profile=body.size_profile,
            admin_username=body.admin_username,
            admin_password=body.admin_password,
        )
    except ConfigManagerRequestError as e:
        # 400/409 are the caller's fault and actionable; anything else is the
        # controller failing, which is a 502 from the browser's point of view.
        # 422 is a rejected admin credential — also the caller's to fix.
        status = e.status_code if e.status_code in (400, 409, 422) else 502
        logger.warning(
            "onboarding.config_manager.env_create_rejected",
            status=e.status_code,
            error=e.detail,
        )
        raise HTTPException(status, e.detail) from e
    except ConfigManagerProvisioningError as e:
        logger.error("onboarding.config_manager.env_create_failed", error=str(e))
        raise HTTPException(502, str(e)) from e

    logger.info(
        "onboarding.config_manager.env_create_scheduled",
        tenant_id=str(user.tenant_id),
        env_id=view.get("id"),
        slug=view.get("slug"),
        is_primary=view.get("is_primary"),
    )
    return {
        "ok": True,
        "message": f"environment {view.get('slug', '')!r} scheduled",
        "environment": _environment_out(view).model_dump(),
    }


@router.get(
    "/config-manager/environments/{env_id}/progress",
    response_model=ConfigManagerProgressOut,
)
async def config_manager_environment_progress(
    env_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Live install progress for one environment.

    Mirrors the singular progress route, including its bookkeeping write —
    but only when this environment is the primary. A non-primary environment
    must not write the Integration row: there is exactly one per tenant and it
    describes the primary, so a second writer would silently repoint the SoT
    client, executor and NATS bridge at the wrong stack.
    """
    from daalu_automation.core.configmgr.environments import environment_progress
    from daalu_automation.core.configmgr.provision import (
        ConfigManagerProvisioningError,
        is_config_manager_controller_enabled,
    )

    if not is_config_manager_controller_enabled():
        return ConfigManagerProgressOut(requested=False)
    try:
        data = await environment_progress(tenant_id=user.tenant_id, env_id=env_id)
    except ConfigManagerProvisioningError as e:
        logger.warning("onboarding.config_manager.env_progress_failed", error=str(e))
        raise HTTPException(502, str(e)) from e
    if data is None:
        raise HTTPException(404, "no such environment")

    urls = data.get("urls") or {}
    if data.get("is_primary") and data.get("state") == "active" and urls:
        try:
            await _upsert_config_manager_integration(
                db,
                tenant_id=user.tenant_id,
                urls=urls,
                base_hostname=urls.get("base_hostname")
                or data.get("base_hostname", ""),
                components=data.get("components") or {},
                gateway_internal_url=data.get("gateway_internal_url") or None,
                nautobot_token_ciphertext=data.get("nautobot_token_ciphertext")
                or None,
                resolved_tunnel_id=(
                    uuid.UUID(data["target_cluster_tunnel_id"])
                    if data.get("target_cluster_tunnel_id")
                    else None
                ),
            )
        except Exception:  # noqa: BLE001
            # Never fail the status poll because the bookkeeping write did —
            # the next poll retries it.
            logger.exception("onboarding.config_manager.integration_upsert_failed")

    return ConfigManagerProgressOut(
        requested=True,
        state=data.get("state", "unknown"),
        namespace=data.get("namespace", ""),
        chart_version=data.get("chart_version", ""),
        last_error=data.get("last_error"),
        message=data.get("message"),
        pods=[ConfigManagerPodOut(**p) for p in (data.get("pods") or [])],
        events=[ConfigManagerEventOut(**e) for e in (data.get("events") or [])],
        summary=data.get("summary") or {},
        urls=urls,
    )


@router.delete("/config-manager/environments/{env_id}", status_code=202)
async def delete_config_manager_environment(
    env_id: uuid.UUID,
    force: bool = False,
    user: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Schedule teardown of one environment; 202 without waiting.

    Delete means *gone*: the controller owns the heavy half (helm uninstall +
    namespace sweep) and then drops its own record, so the environment stops
    being listed rather than lingering as a ``destroyed`` tombstone.

    The hub-side ``config_manager`` Integration row is deleted HERE when the
    torn-down environment is the tenant's primary — the row tracks the
    primary stack's Nautobot/tool wiring, and leaving it behind resurrects
    the environment as a ghost "active" tile (the UI's fallbackEnv reads it).

    ``force`` skips waiting on a successful uninstall and removes the record
    outright — the way out for an environment whose host cluster no longer
    exists, whose teardown can never succeed.
    """
    from daalu_automation.core.configmgr.environments import (
        ConfigManagerRequestError,
        delete_environment,
        list_environments,
    )
    from daalu_automation.core.configmgr.provision import (
        ConfigManagerProvisioningError,
        is_config_manager_controller_enabled,
    )

    if not is_config_manager_controller_enabled():
        raise HTTPException(
            503, "config-manager provisioning is not configured on this deploy"
        )
    try:
        envs = await list_environments(tenant_id=user.tenant_id)
        target = next((e for e in envs if e.get("id") == str(env_id)), None)
        await delete_environment(
            tenant_id=user.tenant_id, env_id=env_id, force=force
        )
    except ConfigManagerRequestError as e:
        status = e.status_code if e.status_code in (400, 404, 409) else 502
        raise HTTPException(status, e.detail) from e
    except ConfigManagerProvisioningError as e:
        raise HTTPException(502, str(e)) from e

    if target is None or target.get("is_primary", True):
        row = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == user.tenant_id,
                    Integration.provider == "config_manager",
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            await db.delete(row)
            await db.commit()
    logger.info(
        "onboarding.config_manager.env_delete_scheduled",
        tenant_id=str(user.tenant_id),
        env_id=str(env_id),
        primary=bool(target is None or target.get("is_primary", True)),
        force=force,
    )
    return {"detail": "environment removed" if force else "teardown scheduled"}


@router.post("/config-manager/environments/{env_id}/upgrade", status_code=202)
async def upgrade_config_manager_environment(
    env_id: uuid.UUID,
    body: UpgradeConfigManagerIn,
    user: User = Depends(current_admin),
):
    """Schedule a chart upgrade for one environment; 202 without waiting."""
    from daalu_automation.core.configmgr.environments import (
        ConfigManagerRequestError,
        schedule_environment_upgrade,
    )
    from daalu_automation.core.configmgr.provision import (
        ConfigManagerProvisioningError,
        is_config_manager_controller_enabled,
    )

    if not is_config_manager_controller_enabled():
        raise HTTPException(
            503, "config-manager provisioning is not configured on this deploy"
        )
    try:
        view = await schedule_environment_upgrade(
            tenant_id=user.tenant_id, env_id=env_id, chart_version=body.chart_version
        )
    except ConfigManagerRequestError as e:
        # 404 (no such environment) and 409 (not newer / mid-teardown) are both
        # meaningful to the caller; keep them rather than collapsing to 409.
        status = e.status_code if e.status_code in (400, 404, 409) else 502
        logger.warning(
            "onboarding.config_manager.env_upgrade_rejected",
            status=e.status_code,
            error=e.detail,
        )
        raise HTTPException(status, e.detail) from e
    except ConfigManagerProvisioningError as e:
        logger.error("onboarding.config_manager.env_upgrade_failed", error=str(e))
        raise HTTPException(502, str(e)) from e

    logger.info(
        "onboarding.config_manager.env_upgrade_scheduled",
        tenant_id=str(user.tenant_id),
        env_id=str(env_id),
        chart_version=view.get("chart_version"),
    )
    return {
        "ok": True,
        "message": f"upgrade to {view.get('chart_version', '')} scheduled",
        "chart_version": view.get("chart_version", ""),
        "state": view.get("state", "pending"),
    }


# ── Local-GPU onboarding ────────────────────────────────────────────────
#
# Deploy the existing deploy/k8s/gpu vLLM stack onto a joined cluster
# over the WG tunnel (via the gpu-controller), then register the endpoint
# as the tenant's SOVEREIGN tier so core/llm.py routes to it and the
# Usage & Pricing banner flips to "configured".
# See docs/plans/2026-06-02-gpu-onboarding.md.

# Tunnel-reachable port daalu-edge fronts the in-cluster ``llm-classifier``
# Service on — see ``core.wireguard.VLLM_PROXY_PORT`` (imported at the top of
# this module). It is deliberately *not* re-declared here: the constant the
# generated ``helm install`` snippet is built from must be the same one we
# pre-fill this form with, or the listener the customer installs and the URL
# the hub dials drift apart — which they did (this read 8000, the *coder*
# port, so the suggested endpoint pointed at the coding stack while the
# served-model-name said llama).


class GpuClusterOut(BaseModel):
    cluster_tunnel_id: str
    slug: str
    name: str
    tunnel_ip: str
    # A pre-filled, tunnel-reachable vLLM URL the UI offers as the default
    # (the edge proxy fronts the in-cluster ``llm-classifier`` Service).
    suggested_base_url: str


class GpuOnboardIn(BaseModel):
    # Pick a joined cluster (auto-deploy vLLM there) OR leave null to use
    # the operator cluster. Either way the controller applies the stack.
    target_cluster_tunnel_id: uuid.UUID | None = None
    gpu_node: str | None = None
    gpu_class: str = "ada-16"
    # HF read token for the gated Llama repo. Optional (ungated/cached).
    hf_token: str | None = None
    model_classifier: str = "meta/llama-3.1-8b-instruct"
    model_quality: str | None = None
    # The endpoint the router will call once vLLM is up. Defaults to the
    # tunnel edge-proxy URL; override for a direct NodePort/DNS.
    base_url: str | None = None
    # Optional bearer token if the endpoint enforces auth (vLLM usually
    # doesn't). Stored Fernet-encrypted, never echoed back.
    endpoint_token: str | None = None


class GpuStatusOut(BaseModel):
    configured: bool
    state: str | None = None
    healthy: bool = False
    base_url: str | None = None
    model_classifier: str | None = None
    source: str = "none"
    cluster_tunnel_id: str | None = None
    # The node the model was deployed onto, so AI Factory can offer
    # Disconnect on that card's own page. None for rows from older forms.
    gpu_node: str | None = None
    last_error: str | None = None


@router.get("/gpu/clusters", response_model=list[GpuClusterOut])
async def list_gpu_clusters(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    """Connected WireGuard clusters the tenant can deploy a GPU onto."""
    rows = (
        (
            await db.execute(
                select(ClusterTunnel).where(
                    ClusterTunnel.tenant_id == tenant_id,
                    ClusterTunnel.status == ClusterTunnelStatus.connected,
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        GpuClusterOut(
            cluster_tunnel_id=str(r.id),
            slug=r.slug,
            name=r.name,
            tunnel_ip=str(r.tunnel_ip),
            suggested_base_url=f"http://{r.tunnel_ip}:{_VLLM_PROXY_PORT}/v1",
        )
        for r in rows
    ]


async def _gpu_status(tenant: Tenant, row: GpuTenant | None) -> GpuStatusOut:
    """Build the GPU card's view, including a live health probe.

    ``healthy`` used to be left unset here, so it fell back to the schema's
    ``False`` default and the card read **Offline** for a GPU that was serving
    — while the pill on the same page, which does probe, read **online**. Both
    now go through the same cached probe so they cannot disagree.
    """
    if tenant.sovereign_inference_url:
        return GpuStatusOut(
            configured=True,
            state=row.state.value if row else None,
            healthy=await probe_openai_endpoint(
                tenant.sovereign_inference_url,
                tenant.sovereign_inference_token_enc,
            ),
            base_url=tenant.sovereign_inference_url,
            model_classifier=tenant.sovereign_model_classifier,
            source="sovereign",
            cluster_tunnel_id=(
                str(row.target_cluster_tunnel_id)
                if row and row.target_cluster_tunnel_id
                else None
            ),
            gpu_node=row.gpu_node if row else None,
            last_error=row.last_error if row else None,
        )
    return GpuStatusOut(
        configured=False,
        state=row.state.value if row else None,
        last_error=row.last_error if row else None,
    )


class GpuNodeOut(BaseModel):
    name: str
    gpu_class: str | None = None
    # Human-readable card, e.g. "NVIDIA RTX 6000 Ada Generation" — lets the
    # form show what was discovered rather than a bare class string.
    gpu_product: str | None = None
    allocatable_gpus: str | None = None
    ready: bool = False


@router.get("/gpu/nodes", response_model=list[GpuNodeOut])
async def list_gpu_nodes(
    cluster_tunnel_id: uuid.UUID | None = None,
    _user: User = Depends(current_admin),
):
    """Schedulable GPU nodes on the chosen cluster (best-effort hint)."""
    from daalu_automation.core.gpu.provision import list_gpu_nodes_via_controller

    nodes = await list_gpu_nodes_via_controller(
        tenant_id=_user.tenant_id,
        target_cluster_tunnel_id=cluster_tunnel_id,
    )
    return [GpuNodeOut(**n) for n in nodes]


@router.get("/gpu", response_model=GpuStatusOut)
async def gpu_status(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "tenant not found")
    row = (
        await db.execute(select(GpuTenant).where(GpuTenant.tenant_id == tenant_id))
    ).scalar_one_or_none()
    return await _gpu_status(tenant, row)


class GpuServingOut(BaseModel):
    """What Daalu deployed onto the tenant's GPU, for the card's own page."""

    model: str
    hf_model: str
    open_weights: bool
    quantization: str
    engine: str
    image: str
    gpu_memory_utilization: float
    context_len: int
    tool_calling: bool
    cluster_name: str | None = None
    cluster_slug: str | None = None
    node: str | None = None
    namespace: str
    gpu_class: str
    state: str
    healthy: bool
    endpoint: str | None = None
    online_since: datetime | None = None


@router.get("/gpu/serving", response_model=GpuServingOut | None)
async def gpu_serving(
    db: AsyncSession = Depends(get_db),
    tenant_id: uuid.UUID = Depends(current_tenant_id),
):
    """The serving stack as the controller renders it (``classifier_params``),
    so the page shows what is actually on the card, not the form's input.
    ``null`` when nothing is deployed (no row, or deleting/destroyed)."""
    from daalu_automation.gpu_controller.manifests import classifier_params

    tenant = await db.get(Tenant, tenant_id)
    row = (
        await db.execute(select(GpuTenant).where(GpuTenant.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if (
        tenant is None
        or row is None
        or row.state in (GpuTenantState.deleting, GpuTenantState.destroyed)
    ):
        return None
    p = classifier_params(
        row.model_classifier,
        gpu_memory_utilization=row.gpu_memory_utilization,
        namespace=row.namespace,
        gpu_class=row.gpu_class,
    )
    ct = (
        await db.get(ClusterTunnel, row.target_cluster_tunnel_id)
        if row.target_cluster_tunnel_id
        else None
    )
    status = await _gpu_status(tenant, row)
    return GpuServingOut(
        model=p.served_model_name,
        hf_model=p.hf_model,
        # Every catalog model is an open-weights checkpoint (FOSS-only
        # serving); the flag is explicit so the UI never has to guess.
        open_weights=True,
        quantization=p.quantization or (
            "fp8" if p.hf_model.lower().endswith("-fp8") else "native"
        ),
        engine=p.engine,
        image=p.image,
        gpu_memory_utilization=p.gpu_memory_utilization,
        context_len=p.max_model_len,
        tool_calling=bool(p.tool_call_parser),
        cluster_name=ct.name if ct else None,
        cluster_slug=ct.slug if ct else None,
        node=row.gpu_node,
        namespace=row.namespace,
        gpu_class=row.gpu_class,
        state=row.state.value,
        healthy=status.healthy,
        endpoint=status.base_url,
        online_since=row.last_ready_at if row.state == GpuTenantState.active else None,
    )


class GpuContainerOut(BaseModel):
    name: str
    ready: bool = False
    restarts: int = 0
    # Why it isn't up yet — ImagePullBackOff, CrashLoopBackOff, …
    reason: str | None = None
    message: str | None = None


class GpuPodOut(BaseModel):
    name: str
    phase: str | None = None
    node: str | None = None
    containers: list[GpuContainerOut] = []
    logs: str | None = None


class GpuEventOut(BaseModel):
    type: str | None = None
    reason: str | None = None
    object: str | None = None
    count: int = 1
    message: str | None = None
    at: str | None = None


class GpuProgressOut(BaseModel):
    phase: str | None = None
    pods: list[GpuPodOut] = []
    events: list[GpuEventOut] = []


@router.get("/gpu/progress", response_model=GpuProgressOut)
async def gpu_progress(user: User = Depends(current_admin)):
    """Live deploy progress: pod phase, kubelet events, vLLM logs.

    A first vLLM boot is three sequential multi-minute waits — pull the ~10 GB
    image, pull ~5 GB of weights, compile CUDA graphs — and "Deploying…" looks
    identical throughout. Events are the only signal until a container starts;
    logs take over once it does. The UI polls this while state is
    ``provisioning`` so the wait is legible instead of a spinner.
    """
    from daalu_automation.core.gpu.provision import gpu_progress_via_controller

    return GpuProgressOut(**await gpu_progress_via_controller(tenant_id=user.tenant_id))


class GpuOperatorStatusOut(BaseModel):
    # absent | installing | blocked | error | ready
    phase: str
    installed: bool = False
    gpu_nodes: int = 0
    distribution: str | None = None
    pods_total: int = 0
    pods_ready: int = 0
    # What's in the way, in a sentence, and (k3s) the host commands that fix it.
    hint: str | None = None
    host_commands: str | None = None


class GpuOperatorInstallIn(BaseModel):
    cluster_tunnel_id: uuid.UUID


@router.get("/gpu/operator", response_model=GpuOperatorStatusOut)
async def gpu_operator_status(
    cluster_tunnel_id: uuid.UUID,
    user: User = Depends(current_admin),
):
    """The NVIDIA GPU Operator on a connected cluster: there, coming up, or stuck.

    Backs the Add GPU form when a cluster advertises no GPU yet.
    """
    from daalu_automation.core.gpu.provision import (
        GpuProvisioningError,
        gpu_operator_status_via_controller,
    )

    try:
        return GpuOperatorStatusOut(
            **await gpu_operator_status_via_controller(
                tenant_id=user.tenant_id, target_cluster_tunnel_id=cluster_tunnel_id
            )
        )
    except GpuProvisioningError as e:
        raise HTTPException(502, str(e)) from e


@router.post("/gpu/operator", status_code=202)
async def install_gpu_operator(
    body: GpuOperatorInstallIn,
    user: User = Depends(current_admin),
):
    """Install the NVIDIA GPU Operator on a connected cluster. **Async (202).**

    The controller works out the settings (k3s keeps the operator's toolkit
    off; DCGM metrics carry this tenant's label) and runs the helm install
    over the tunnel. Poll ``GET /onboarding/gpu/operator`` for progress.
    """
    from daalu_automation.core.gpu.provision import (
        GpuProvisioningError,
        install_gpu_operator_via_controller,
    )

    try:
        return await install_gpu_operator_via_controller(
            tenant_id=user.tenant_id, target_cluster_tunnel_id=body.cluster_tunnel_id
        )
    except GpuProvisioningError as e:
        raise HTTPException(502, str(e)) from e


@router.post("/gpu/provision", response_model=GpuStatusOut, status_code=202)
async def provision_gpu(
    body: GpuOnboardIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Accept a GPU deployment and register it as SOVEREIGN. **Async (202).**

    Drives the gpu-controller to apply the ``deploy/k8s/gpu`` stack (operator
    cluster by default, or the customer's cluster when
    ``target_cluster_tunnel_id`` is set) and writes the tenant's SOVEREIGN
    routing config so ``core/llm.py`` starts routing LLM calls to the GPU.
    503 if the controller isn't configured.

    Returns as soon as the controller has accepted the spec — the returned
    ``state`` is ``provisioning``, not ``active``. Poll ``GET /onboarding/gpu``
    for the transition; first boot takes minutes (weights + CUDA graphs).

    This used to block until vLLM was ready, up to 900s. Since the edge in
    front of this API cuts the connection at 100s, every successful deploy
    surfaced in the browser as a bare ``Failed to fetch`` while the deploy
    quietly carried on server-side.
    """
    from daalu_automation.core.crypto import encrypt_secret
    from daalu_automation.core.gpu.provision import (
        GpuProvisioningError,
        is_gpu_controller_enabled,
        upsert_gpu_spec,
    )

    if not is_gpu_controller_enabled():
        raise HTTPException(503, "GPU onboarding is not configured on this deploy")

    if body.target_cluster_tunnel_id is not None:
        ct = await db.get(ClusterTunnel, body.target_cluster_tunnel_id)
        if ct is None or ct.tenant_id != user.tenant_id:
            raise HTTPException(404, "cluster_tunnel not found")
        if ct.status != ClusterTunnelStatus.connected:
            raise HTTPException(
                409, f"cluster_tunnel is {ct.status.value} — must be 'connected'"
            )
        default_url = f"http://{ct.tunnel_ip}:{_VLLM_PROXY_PORT}/v1"
    else:
        default_url = get_settings().llm_local_base_url or ""

    base_url = (body.base_url or default_url).strip()
    if not base_url:
        raise HTTPException(
            400,
            "base_url is required when no target cluster is selected — "
            "give the tunnel-reachable vLLM endpoint",
        )

    # Hand the spec to the controller and return — do NOT wait for vLLM.
    # Only the upsert is synchronous, so a bad spec / unreachable cluster /
    # missing kubeconfig still fails fast with a real message. Everything
    # after it (pulling ~5 GB of weights, compiling CUDA graphs) takes
    # minutes, and the edge in front of this API cuts idle requests at 100s —
    # so waiting here made a *successful* deploy show up in the browser as
    # ``Failed to fetch``. The client polls ``GET /onboarding/gpu`` instead.
    try:
        await upsert_gpu_spec(
            tenant_id=user.tenant_id,
            gpu_node=body.gpu_node,
            gpu_class=body.gpu_class,
            hf_token=body.hf_token,
            model_classifier=body.model_classifier,
            model_quality=body.model_quality,
            target_cluster_tunnel_id=body.target_cluster_tunnel_id,
        )
    except GpuProvisioningError as e:
        logger.error("onboarding.gpu.provision_failed", error=str(e))
        raise HTTPException(502, str(e)) from e

    # Register the endpoint as the tenant's SOVEREIGN tier now, rather than
    # after vLLM reports ready. Safe because the router's cascade falls
    # through to the next tier on transport failure (_resolve_plan), so a
    # still-booting endpoint behaves exactly like a GPU that is momentarily
    # down — and this way the config survives an API-replica restart mid-boot
    # instead of depending on whoever handled the request staying alive.
    tenant = await db.get(Tenant, user.tenant_id)
    tenant.sovereign_inference_url = base_url
    tenant.sovereign_model_classifier = body.model_classifier
    tenant.sovereign_model_quality = body.model_quality
    tenant.sovereign_inference_token_enc = (
        encrypt_secret(body.endpoint_token) if body.endpoint_token else None
    )
    flags = dict(tenant.feature_flags or {})
    flags["sovereign_enabled"] = True
    tenant.feature_flags = flags
    await db.commit()

    row = (
        await db.execute(
            select(GpuTenant).where(GpuTenant.tenant_id == user.tenant_id)
        )
    ).scalar_one_or_none()
    await db.refresh(tenant)
    logger.info("onboarding.gpu.provisioned", tenant_id=str(user.tenant_id))
    return await _gpu_status(tenant, row)


@router.delete("/gpu", response_model=GpuStatusOut)
async def deprovision_gpu(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_admin),
):
    """Tear down the vLLM stack + clear the tenant's SOVEREIGN config.

    Best-effort controller teardown; the SOVEREIGN config is cleared
    regardless so the router stops sending traffic to a GPU being removed.
    """
    s = get_settings()
    if s.gpu_controller_url:
        from daalu_automation.core.service_tokens import mint_service_token

        token = mint_service_token(
            tenant_id=str(user.tenant_id),
            user_id=str(user.tenant_id),
            purpose="gpu-provision",
            ttl_seconds=120,
        )
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.delete(
                    f"{s.gpu_controller_url.rstrip('/')}/tenants/{user.tenant_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception as e:  # noqa: BLE001 — clearing config is what matters
            logger.warning("onboarding.gpu.teardown_call_failed", error=str(e))

    tenant = await db.get(Tenant, user.tenant_id)
    tenant.sovereign_inference_url = None
    tenant.sovereign_inference_token_enc = None
    tenant.sovereign_model_classifier = None
    tenant.sovereign_model_quality = None
    flags = dict(tenant.feature_flags or {})
    flags.pop("sovereign_enabled", None)
    tenant.feature_flags = flags
    await db.commit()
    await db.refresh(tenant)
    return await _gpu_status(tenant, None)

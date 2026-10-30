"""Map a ``ConfigManagerTenant`` row → upstream ``NVConfigManagerInstallConfig``.

The vendored NV-CM ``Deployer`` is config-driven: it consumes a Pydantic
``NVConfigManagerInstallConfig`` (the same model the upstream TUI/CLI produce)
and from it pre-creates secrets, renders Helm values, and runs ``helm``. This
module is the single translation point from Daalu's per-tenant row + settings
into that config — the Deployer-era analogue of :mod:`.values`.

Pure and side-effect free so it can be unit-tested without a cluster. The
Deployer is *driven* (and the few Daalu-specific Helm knobs the upstream
config can't express — shared GatewayClass, skipping the NodePort gateway
patch — are applied) in :mod:`.deployer_runner`.

See docs/design/nv-config-manager-integration.md and engineer chapter 64.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from daalu_automation.config import Settings, get_settings
from daalu_automation.config_manager_controller import local_admin
from daalu_automation.config_manager_controller.values import (
    DEFAULT_COMPONENTS,
    HARBOR_PROJECT,
    SHARED_GATEWAY_CLASS,
)
from daalu_automation.models import ConfigManagerTenant

# The vendored nv_config_manager_installer package is StrEnum-based (Python
# 3.11+, matching the controller's runtime image and CI). Import its schema
# lazily inside the functions that use it so merely importing this module — and
# therefore the controller app — still works on a 3.10 dev interpreter that
# only exercises the legacy HelmRunner path.
if TYPE_CHECKING:
    from nv_config_manager_installer.schema import (
        DeploySize,
        NVConfigManagerInstallConfig,
    )

# Daalu always installs the shared cluster-scoped GatewayClass (Tier-A) and
# references it read-only; a per-tenant release must NOT create its own. The
# upstream config schema has no field for this, so the runner injects it as a
# post-generate Helm values overlay — but we surface the class name here so the
# two stay in one place. See deployer_runner._gateway_overlay.
GATEWAY_CLASS = SHARED_GATEWAY_CLASS

# row.components key → upstream ServicesConfig attribute. Daalu's ``ui`` toggle
# has no upstream equivalent (the chart UI rides config-store), so it is
# dropped here.
_COMPONENT_TO_SERVICE: dict[str, str] = {
    "render": "render",
    "configStore": "config_store",
    "temporal": "temporal",
    "nautobot": "nautobot",
    "ztp": "ztp",
    "dhcp": "dhcp",
}


def release_name(row: ConfigManagerTenant) -> str:
    """Helm release name for a tenant — stable, namespace-independent.

    Matches the name the controller has always used so a switch to the
    Deployer adopts (``helm upgrade``) the existing release rather than
    orphaning it.
    """
    return f"cm-{row.tenant_id.hex[:12]}"


def _deploy_size(size_profile: str) -> DeploySize:
    from nv_config_manager_installer.schema import DeploySize

    try:
        return DeploySize(size_profile)
    except ValueError:
        return DeploySize.SMALL


def build_install_config(
    row: ConfigManagerTenant,
    *,
    settings: Settings | None = None,
) -> NVConfigManagerInstallConfig:
    """Build the upstream install config for this tenant's NV-CM release."""
    from nv_config_manager_installer.schema import (
        NV_CONFIG_MANAGER_IMAGE_KEYS,
        ImageOverride,
        ImageSource,
        NVConfigManagerInstallConfig,
        SecretsMethod,
        SSOProvider,
    )

    settings = settings or get_settings()
    components = {**DEFAULT_COMPONENTS, **(row.components or {})}

    cfg = NVConfigManagerInstallConfig()

    # ── Cluster ────────────────────────────────────────────────────────
    cfg.cluster.hostname = row.base_hostname
    cfg.cluster.namespace = row.namespace
    cfg.cluster.release_name = release_name(row)
    cfg.cluster.environment = "production" if settings.is_production else "staging"
    cfg.cluster.size = _deploy_size(row.size_profile)
    # Subcharts are vendored (Chart.lock + charts/*.tgz baked into the image),
    # so skip the Deployer's ``helm dependency update`` — it would otherwise try
    # to reach public Helm repos, which we don't want from the controller.
    cfg.cluster.airgapped = True

    # ── Secrets: always in-cluster (kubernetes), never ESO/Vault ───────
    # This is the whole point of adopting the Deployer: it pre-creates these
    # before helm so the secret-assembler pre-install hook doesn't deadlock.
    cfg.secrets.method = SecretsMethod.KUBERNETES

    # ── Service toggles ────────────────────────────────────────────────
    for comp_key, svc_attr in _COMPONENT_TO_SERVICE.items():
        setattr(cfg.services, svc_attr, bool(components.get(comp_key)))

    # ── Images → Harbor mirror ─────────────────────────────────────────
    # The chart pulls each app image as ``<repository>:<tag>`` from
    # ``global.images.<key>`` with no ``global.imageRegistry`` indirection, so
    # helm_values emits a per-image repository override for exactly the keys in
    # NV_CONFIG_MANAGER_IMAGE_KEYS. Point each at the Harbor mirror (same
    # ``<harbor>/<project>/<basename>`` path the mirror script pushes to); leave
    # tag empty to inherit the chart default. Parity with values.render_values.
    cfg.images.source = ImageSource.REGISTRY
    harbor = (settings.config_manager_harbor_registry or "").rstrip("/")
    if harbor:
        cfg.images.overrides = {
            key: ImageOverride(repository=f"{harbor}/{HARBOR_PROJECT}/{basename}")
            for key, basename in NV_CONFIG_MANAGER_IMAGE_KEYS
        }
    # Don't inject the upstream default ``regcred-nvcr`` pull secret unless one
    # is configured — the host cluster pulls from Harbor without a chart-level
    # imagePullSecret today, and a dangling reference would wedge pods.
    cfg.images.pull_secret.name = settings.config_manager_image_pull_secret or ""
    cfg.images.pull_secret.password = ""

    # ── OIDC: trust Daalu's Keycloak for machine-JWT validation on svc-* ──
    # Mirror values.render_values: enable issuer + audiences only (bearer-token
    # validation). Leave client_id/secret empty — the hub presents its own
    # service JWTs; we are not wiring an interactive oauth2-proxy login here.
    if settings.keycloak_issuer_url:
        cfg.sso.enabled = True
        cfg.sso.provider = SSOProvider.KEYCLOAK
        cfg.sso.issuer_url = settings.keycloak_issuer_url
        cfg.sso.audiences = settings.keycloak_token_audience or ""
        # The chart's gateway SecurityPolicy emits an OIDC redirect block that
        # REQUIRES a non-empty clientId (else `helm install` fails CRD
        # validation: "spec.oidc.clientID … should be at least 1 chars long").
        # The hub authenticates with bearer service JWTs (validated by the
        # jwt block, audiences above), but the interactive-login clientId must
        # still be populated for the release to install. Use the UI client id.
        cfg.sso.client_id = settings.keycloak_ui_client_id or "nv-config-manager-ui"
        # UI client secret — carried so deployer_runner can pre-create the
        # `oidc-client-secret` Secret the gateway OIDC redirect needs (else the
        # human <host> URLs 500). helm_values does NOT inline it; the chart
        # references it by Secret name. Empty → no Secret, no browser login.
        cfg.sso.client_secret = settings.keycloak_ui_client_secret or ""
        # In-cluster JWKS source for Envoy's svc-* JWT validation. Envoy in the
        # operator's cluster can't reach the external issuer host, so without
        # this every authed svc-* call 401s with "Jwks remote fetch is failed".
        # The token `iss` still uses issuer_url (external) above; only the JWKS
        # fetch uses this internal Keycloak Service URL (same realm keys).
        #
        # Only valid when the release lands in the operator's OWN cluster.
        # A customer cluster (target_cluster_tunnel_id NOT NULL) reached over
        # WireGuard runs no Keycloak, so the internal Service name doesn't
        # resolve there — leave it unset and let the external issuer be used.
        # Mirrors values.render_values; keep the two in step.
        if (
            settings.keycloak_internal_issuer_url
            and row.target_cluster_tunnel_id is None
        ):
            cfg.sso.internal_issuer = settings.keycloak_internal_issuer_url

    # ── Break-glass local admin (Nautobot) ────────────────────────────
    # Nautobot is the only component with its own login. Upstream generates the
    # superuser password per-install and never surfaces it, so an operator has
    # no way in when SSO itself is the thing that's broken. When the creator
    # chose a credential, stamp it here — ``adminPassword`` is the override the
    # installer checks before generating (secrets.py `_v("nautobot_app",
    # "adminPassword")`). An empty string keeps upstream's generated value, so
    # rows predating this feature are unaffected.
    # (username is a Helm values knob, set in values.render_values)
    _, _admin_pw = local_admin.from_row(row)
    if _admin_pw:
        cfg.secrets.k8s.nautobot_app.values["adminPassword"] = _admin_pw

    # ── Infrastructure ────────────────────────────────────────────────
    # TLS on (cert-manager issues per-tenant gateway certs). LoadBalancer stays
    # NONE — Daalu fronts the shared Envoy Gateway via traefik, so there is no
    # per-tenant LB. (The runner skips the Deployer's NodePort gateway patch
    # that NONE would otherwise trigger.)
    cfg.infrastructure.tls = True

    return cfg

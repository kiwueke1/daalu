"""Application configuration loaded from environment variables / .env file.

Layered settings so several deployments can
share infrastructure (same Postgres / Redis / Rook-Ceph / Anthropic account)
just by pointing them at the same secret material.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Pre-multi-tenant rows are stamped with this fixed UUID so single-operator
# installs keep working before auth is wired. Do NOT change once any row
# references it.
DEFAULT_TENANT_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000010")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ───────────────────────────────────────────────────────────────────
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"
    # Public URL the browser reaches the operator app at (e.g.
    # "https://ops.daalu.io"). Used to build absolute links for
    # outbound emails (invite URLs, password resets). Empty means
    # the email delivery stub will emit path-relative URLs.
    public_base_url: str = ""
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:8000",
    ]
    secret_key: str = "change-me"
    access_token_expire_minutes: int = 1440
    # Signing key for inter-service tokens (daalu-api ↔ inference-gateway,
    # daalu-api ↔ workspace-controller). Deliberately a separate key from
    # ``secret_key`` so a session-cookie key leak does not grant the
    # ability to mint service tokens. Set to a long random string in
    # prod. Empty means service-to-service auth is disabled (dev only).
    service_token_secret_key: str = ""
    # Shared secret webhooks send in `X-Daalu-Key` to POST events. Empty
    # disables the gate (Phase-1 behaviour). Set in prod to a long random
    # string so Alertmanager/Cloudmailin/etc. can POST without a user
    # account while everything else stays locked behind login.
    ingest_api_key: str = ""
    # Auth-cookie attributes. In prod we set HttpOnly + Secure + SameSite=Lax
    # so the cookie survives top-level navigation from external links but
    # not embedded cross-site requests.
    auth_cookie_name: str = "daalu_session"
    # Cookie ``Domain`` attribute. Empty → host-only (the cookie is sent
    # only to the exact host that set it, e.g. ops.daalu.io). Set to a
    # parent domain (e.g. ``.daalu.io``) so the session cookie is also sent
    # to sibling hosts — required for the NV-CM tool proxy on
    # ``*.cmtools.daalu.io`` to authenticate the browser via the hub
    # session. Only set this to a domain you fully control (every host
    # under it can then read the session cookie). eTLD+1 = daalu.io, so
    # SameSite=Lax still applies (same-site navigation).
    auth_cookie_domain: str = ""
    auth_cookie_secure: bool = True
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    # Comma-separated paths that bypass auth. /health is required by
    # k8s probes; /api/v1/auth/* is the login surface itself; /metrics
    # is scraped by Prometheus from the monitoring namespace (the
    # NetworkPolicy is what keeps it private to the cluster). Webhook
    # path is exempted via ingest_api_key, not via this allowlist.
    auth_public_paths: str = (
        "/health,/version,/metrics,/,/api/v1/auth/login,/api/v1/auth/logout,/api/v1/auth/me,"
        "/api/v1/auth/config,"
        "/api/v1/auth/oidc/login,/api/v1/auth/oidc/callback,/api/v1/auth/oidc/logout,"
        # `daalu login` device flow — the CLI has no session yet; the
        # device_code in the body is itself the secret (see api/routers/cli_auth).
        "/api/v1/cli/device/code,/api/v1/cli/device/token,"
        "/docs,/redoc,/openapi.json"
    )

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = (
        "postgresql+asyncpg://daalu:daalu_password@localhost:5432/daalu_automation"
    )
    database_sync_url: str = (
        "postgresql://daalu:daalu_password@localhost:5432/daalu_automation"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ── Redis / Celery ────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # Redis stream key used by the in-process event bus. Workers consume
    # from this stream; the API publishes to it. Kept as a single global
    # stream for now — partitioning by tenant/module is a Phase-2 concern.
    event_stream_key: str = "daalu.events"
    event_stream_group: str = "daalu-workers"

    # ── Object storage ────────────────────────────────────────────────────────
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "daalu_access_key"
    s3_secret_key: str = "daalu_secret_key"
    s3_bucket_reports: str = "daalu-reports"
    s3_bucket_events: str = "daalu-events"
    s3_bucket_attachments: str = "daalu-attachments"
    # AIPerf run artifacts (the profile_export_aiperf.csv/json + logs tree the
    # benchmark writes per ISL/OSL/concurrency). The gpu-controller's uploader
    # sidecar pushes them here under ``aiperf/{run_id}/``; the API streams them
    # back for download. Bucket is created on first use if absent.
    s3_bucket_aiperf: str = "daalu-aiperf"

    # ── LLM — Anthropic (primary) ────────────────────────────────────────────
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_model_fast: str = "claude-haiku-4-5"
    anthropic_cache_enabled: bool = True

    # ── LLM — OpenAI-compatible fallback ─────────────────────────────────────
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_model_classifier: str = "deepseek-chat"

    # ── GPU inference (optional) ─────────────────────────────────────────────
    runpod_api_key: str = ""
    runpod_endpoint_id: str = ""
    replicate_api_token: str = ""

    # ── Local GPU (vLLM on the workload cluster, via WG mesh) ────────────────
    # OpenAI-compatible base URL for the vLLM Service. Empty disables the
    # local-first path — routers fall through to llm_api_key /
    # anthropic_api_key. In production, with the workload cluster federated
    # via daalu-edge:
    #   http://10.200.0.2:8000/v1
    # If daalu-api runs in the same cluster as vLLM, the in-cluster DNS works:
    #   http://llm-classifier.daalu.svc.cluster.local/v1
    # In local dev you can port-forward and set http://localhost:8000/v1.
    llm_local_base_url: str = ""
    # vLLM endpoints don't enforce auth by default; this is sent only if set.
    llm_local_api_key: str = ""
    # Model id the server advertises. vLLM is launched with
    # `--served-model-name=meta/llama-3.1-8b-instruct` so this stays stable
    # across underlying HF repo swaps (Meta base, AWQ quant, Qwen fallback).
    llm_local_model_classifier: str = "meta/llama-3.1-8b-instruct"
    # Heavier model class — set when a 24GB+ card joins the pool.
    llm_local_model_quality: str = ""
    # Health-check path. vLLM serves `/health`. (NIM used `/v1/health/ready`;
    # if you ever re-introduce a NIM-style upstream, override per-deploy.)
    # The local-first router caches results for `llm_local_health_ttl_s`
    # seconds so a brief ISP wobble doesn't fail every call.
    llm_local_health_path: str = "/health"
    llm_local_health_ttl_s: int = 30
    # Per-million-tokens pricing of running the local vLLM, used to value
    # the tokens *we ourselves* manufacture. Defaults reflect amortised
    # cost of an RTX 2000 Ada (16GB, 70W) running 24/7 at ~70 % duty cycle.
    # Override in prod once you measure actual utilisation.
    llm_local_price_in_per_mtok: float = 0.10
    llm_local_price_out_per_mtok: float = 0.40

    # ── daalu-hosted inference gateway (operator-owned vLLM pool) ────────────
    # The inference-gateway service fronts a vLLM Deployment in the hub
    # cluster. ``daalu-api`` calls the gateway; the gateway proxies to
    # this upstream URL after quota + auth + metering. The URL must NOT
    # be reachable from outside the cluster — only the gateway's
    # NetworkPolicy allows it.
    daalu_hosted_upstream_url: str = ""
    daalu_hosted_upstream_api_key: str = ""
    # In the hub: http://inference-gateway.daalu-automation.svc.cluster.local
    # ``daalu-api`` uses this URL to reach the gateway. When the gateway
    # IS the receiving service it ignores this; only the caller cares.
    daalu_hosted_gateway_url: str = ""
    # Redis URL the gateway uses for the per-tenant token bucket. Falls
    # back to the same Redis as the main app if unset.
    daalu_hosted_redis_url: str = ""
    # Per-million-tokens pricing of the daalu-hosted tier — what we
    # *charge* tenants who use the pool. Higher than llm_local_price_*
    # because that's the COGS estimate; this is the retail price.
    daalu_hosted_price_in_per_mtok: float = 0.30
    daalu_hosted_price_out_per_mtok: float = 0.90
    # Platform take-rate on shared-GPU usage. Each shared-pool call credits
    # the GPU owner ``gross * (1 - platform_take_rate)`` in the
    # ``gpu_revenue_shares`` ledger; the platform keeps the rest. Default 0
    # so the sole-provider/operator nets the full amount (the credit and the
    # take both accrue to the same entity). Raise this once a *second* tenant
    # is granted is_gpu_provider and resells capacity. Range [0, 1].
    platform_take_rate: float = 0.0

    # ── Observability / Prometheus (AI Factory UI) ───────────────────────────
    # The hub queries kube-prometheus-stack's Prometheus directly for the
    # native GPU-metrics UI (docs/plans/nvidia-ai-factory/02-dcgm-observability.md).
    # NOT Thanos — it has no stores ([[agent_observability_data_plane]]). Empty
    # disables the AI-factory metrics endpoints gracefully (they report
    # "metrics unavailable"). In-cluster default form:
    #   http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090
    prometheus_base_url: str = ""
    prometheus_query_timeout_s: float = 8.0
    # Cache window for an instant query so UI polling doesn't hammer Prometheus.
    prometheus_cache_ttl_s: int = 15
    # Gate for the on-GPU diagnostic exec (dcgmi diag / nccl) the gpu-controller
    # reconcile runs. OFF by default so the (cluster-only, untested-in-CI) exec
    # path doesn't run until validated on a real cluster; when off, requested
    # dcgmi/nccl runs are marked error with a clear message. observability
    # validation (read-only Prometheus) always works regardless of this flag.
    gpu_diagnostics_exec_enabled: bool = False
    # Image used for the one-shot dcgmi-diag pod. The DCGM version must match
    # the node's GPU driver (too old → "Detected unsupported Cuda version").
    # Empty → the code default (diagnostics.DCGM_DIAG_IMAGE, currently DCGM 4.4.2).
    gpu_dcgm_diag_image: str = ""

    # ── AIPerf (load-test / SLO benchmarking) ────────────────────────────────
    # AIPerf (ai-dynamo/aiperf) is a pure OpenAI-compatible load generator that
    # produces the TTFT/ITL/throughput-vs-concurrency curve behind the pricing
    # SLOs (docs/plans/nvidia-ai-factory/04-aiperf.md). The AI-factory UI lets a
    # site **superuser** (any target) or a **GPU owner/provider** (their own
    # endpoint only) kick a sweep; the gpu-controller runs it as a one-shot
    # Job on the OPERATOR cluster. OFF by default because a full-concurrency
    # sweep IS load — against the single shared prod card it spikes live tenants
    # (run off-peak / against a candidate node). When off, requested runs are
    # marked error with a clear message.
    gpu_aiperf_exec_enabled: bool = False
    # AIPerf image (Apache-2.0; mirror to Harbor + pin before first use — no NGC
    # entitlement). Empty → the code default (aiperf.AIPERF_IMAGE).
    gpu_aiperf_image: str = ""
    # Uploader sidecar image — a MinIO client (``mc``) that pushes the AIPerf
    # artifact tree to object storage after the sweep. Mirror to Harbor + pin
    # before first use.
    gpu_aiperf_uploader_image: str = "minio/mc:latest"
    # AIPerf object storage — DISTINCT from the platform ``s3_*`` (reports/
    # events/attachments). AIPerf runs on the cluster that hosts the endpoint
    # under test (the workload/operator cluster), so its artifacts live in THAT
    # cluster's object store (e.g. its Ceph RGW), not the hub's. Two endpoints
    # because the writer and the reader sit on different sides of the tunnel:
    #   * ``..._s3_endpoint`` — the IN-CLUSTER RGW URL the uploader sidecar
    #     (which runs on the workload cluster) writes to.
    #   * ``..._s3_endpoint_hub`` — the SAME store as reached FROM the hub, over
    #     the WireGuard tunnel (a daalu-edge svcproxy), used by the controller +
    #     API to read artifacts back for the curve and downloads. Empty → the
    #     hub-side readback degrades to ``artifacts_error`` (uploads still work).
    # Creds are shared by both. All empty → fall back to the platform ``s3_*``.
    gpu_aiperf_s3_endpoint: str = ""
    gpu_aiperf_s3_endpoint_hub: str = ""
    gpu_aiperf_s3_access_key: str = ""
    gpu_aiperf_s3_secret_key: str = ""

    # ── NVSentinel (GPU fault auto-remediation) ──────────────────────────────
    # NVSentinel watches the DCGM/XID/ECC stream and (when promoted out of
    # observe mode) auto-cordons/drains/reboots a faulted GPU node — the BYO-GPU
    # SLA play (docs/plans/nvidia-ai-factory/03-nvsentinel-resiliency.md). It is
    # Helm-installed cluster-side (deploy/k8s/gpu/09-nvsentinel/) and exports its
    # own remediation metrics; the hub only READS them to render the AI-factory
    # Reliability panel. This is the Prometheus job name NVSentinel is scraped
    # under (its ServiceMonitor); empty → the panel reports "auto-remediation
    # not active" and falls back to raw DCGM health signals.
    nvsentinel_metrics_job: str = "nvsentinel"

    # ── cuda-checkpoint (CUDA checkpoint/restore) — LEGAL GATE ───────────────
    # cuda-checkpoint is PROPRIETARY NVIDIA software (NVIDIA EULA), an explicit
    # exception to the FOSS rule ([[foss_inference_pivot]]). It is free to use
    # but NOT redistributable without **legal sign-off** — see
    # deploy/k8s/gpu/09-nvsentinel/CUDA-CHECKPOINT-LEGAL.md. Hard-gated OFF: no
    # checkpoint/restore feature may run until this is deliberately enabled
    # AFTER legal clears it. The UI shows it as "gated — legal sign-off
    # required" while false.
    gpu_cuda_checkpoint_enabled: bool = False

    # ── Workspace controller (browser-IDE coding assistant) ──────────────────
    # In-cluster URL of the workspace-controller service. Empty disables
    # the /api/v1/ide/* routes (single-tenant deployments that don't
    # need the IDE leave this empty).
    workspace_controller_url: str = ""
    # Coding models a user may pick when creating a workspace. These are
    # catalog ids from ``core.model_catalog`` whose vLLM is *actually
    # deployed* on a present GPU. The UI greys out catalog entries not in
    # this list and ``create_session`` rejects a non-enabled pick with 422.
    # The agentic Qwen3-Coder-30B-A3B serves on the 48 GB RTX 6000 Ada (gpu-node-3).
    coding_models_enabled: list[str] = ["qwen3-coder-30b-a3b"]
    # The catalog id pre-selected in the UI and used when a request omits
    # ``model``. Must also appear in ``coding_models_enabled`` in prod.
    coding_model_default: str = "qwen3-coder-30b-a3b"
    # Raw vLLM endpoint that serves the *coding* models (the qwen-coder
    # stack), used by the public coding endpoint (the laptop ``daalu``
    # CLI). Kept deliberately separate from ``llm_local_base_url`` (the
    # llama *classifier* on :8001) and from ``daalu_hosted_gateway_url``
    # (the general-router hosted tier + workspace-availability flag) so
    # wiring the coder upstream never disturbs daalu-api's other LLM
    # routing — the same scoping the workspace-controller uses for the IDE
    # path. Prod: ``http://10.200.0.2:8000/v1`` (coder vLLM over the WG
    # tunnel). When empty the coding endpoint falls back to the gateway,
    # then to ``llm_local_base_url``.
    coding_local_base_url: str = ""

    # ── Nautobot controller (per-tenant hosted Nautobot) ─────────────────────
    # In-cluster URL of the nautobot-controller service. Empty disables
    # the hosted-Nautobot provisioning path (the wizard hides the
    # "Provision a hosted Nautobot" tile and the customer falls back to
    # BYO). See engineer chapter 60.
    nautobot_controller_url: str = ""

    # ── Config-management plane: NV-CM (network) + Tinkerbell (servers) ──────
    # In-cluster URL of the config-manager-controller service, which
    # provisions per-tenant NVIDIA Config Manager stacks via Helm over the
    # WireGuard tunnel. Empty disables the network-plane provisioning
    # wizard. See docs/design/nv-config-manager-integration.md §6/§7.
    config_manager_controller_url: str = ""
    # In-cluster URL of the gpu-controller service, which deploys the
    # vLLM GPU stack (deploy/k8s/gpu/*) onto the operator's or a joined
    # customer's cluster over the WireGuard tunnel and registers it as
    # the tenant's SOVEREIGN inference tier. Empty disables the GPU
    # onboarding wizard. See docs/plans/2026-06-02-gpu-onboarding.md.
    gpu_controller_url: str = ""
    # Keycloak issuer the hub trusts and mints machine tokens against for
    # NV-CM's svc-* (JWT-only) endpoints, e.g.
    # "https://keycloak.daalu.io/realms/daalu". Empty disables NV-CM calls.
    keycloak_issuer_url: str = ""
    # Explicit OIDC token endpoint; derived from the issuer when empty
    # (<issuer>/protocol/openid-connect/token).
    keycloak_token_url: str = ""
    # Default audience requested in the client-credentials grant; align
    # with NV-CM's oidc.audiences so its SecurityPolicy accepts the token.
    keycloak_token_audience: str = "nv-config-manager"
    # Keycloak *UI* client id (interactive browser login on the NV-CM gateway).
    # The chart's gateway SecurityPolicy emits an OIDC redirect block whenever
    # the gateway is enabled and REQUIRES a non-empty oidc.clientId, or
    # `helm install` fails CRD validation. Even though the hub itself uses
    # bearer service JWTs, this must be set for the release to install.
    keycloak_ui_client_id: str = "nv-config-manager-ui"
    # Client *secret* for keycloak_ui_client_id. The chart's gateway OIDC
    # redirect (Envoy's authorization-code flow) is a confidential client and
    # references a k8s Secret (``oidc-client-secret``, key ``client-secret``);
    # without it the human ``<host>`` URLs 500. When set, the Deployer
    # pre-creates that Secret per tenant so interactive browser login works.
    # Secret material → rides the SOPS-managed daalu-automation-secrets. Empty
    # → no Secret created (machine svc-* path is unaffected).
    keycloak_ui_client_secret: str = ""
    # In-cluster issuer URL Envoy uses to fetch the OIDC JWKS for svc-* JWT
    # validation. The token `iss` stays ``keycloak_issuer_url`` (external,
    # e.g. https://auth.daalu.io/realms/daalu), but Envoy on the WORKLOAD
    # cluster usually can't reach that external host (it 404s / isn't routed
    # internally) → "Jwks remote fetch is failed" → 401 on every authed svc-*
    # call. Point this at the workload-cluster-internal Keycloak Service URL
    # (the in-cluster ``keycloak`` service in the auth namespace, ``/realms/
    # <realm>``) — Envoy fetches JWKS there (same realm signing keys), while
    # the `iss` check still uses the external issuer. The concrete value is
    # set on the controller Deployment. Empty → Envoy uses the external issuer.
    keycloak_internal_issuer_url: str = ""

    # Public wildcard zone under which per-tenant NV-CM tool UIs are served
    # *through the hub*. Each tool gets a single-label host
    # ``<tool>-<slug>.<cmtools_base_domain>`` (the config-browser UI keeps the
    # bare ``<slug>.<cmtools_base_domain>``). One ``*.<domain>`` DNS record +
    # one wildcard cert on the hub cover every tenant. The hub terminates TLS
    # and reverse-proxies over the WireGuard tunnel (api/tool_proxy.py); tenant
    # clusters need no public DNS or certs.
    cmtools_base_domain: str = "cmtools.daalu.io"

    # ── Hub SSO: human login to ops.daalu.io via Keycloak OIDC ────────────────
    # When ``oidc`` the browser login flow redirects to Keycloak (the same
    # realm as ``keycloak_issuer_url``) instead of the local email+password
    # form, so a logged-in operator already holds a Keycloak session and the
    # "Open in Nautobot/NV-CM" deep links are seamless (no second prompt).
    # ``password`` keeps the legacy local login (rollback / dev). The cutover
    # is a single setting flip on the daalu-api Deployment. See engineer book
    # 10-security/59 (Keycloak hub SSO). Identity stays daalu-owned (User row carries
    # tenant_id/is_admin); Keycloak is only the credential + SSO authority.
    auth_mode: Literal["password", "oidc"] = "password"
    # Self-service local signup. When true, visitors can create a local
    # email+password account (``POST /auth/signup``) and — crucially — local
    # password login is permitted even while ``auth_mode == "oidc"``, so a
    # self-registered account can sign in alongside Google SSO. Off by default:
    # an SSO-only deployment stays SSO-only until an operator opts in. Each
    # signup lands in its **own isolated tenant** as a non-admin user and must
    # verify its email before the account activates (see ``core/signup.py``).
    self_signup_enabled: bool = False
    # Native email+password LOGIN, decoupled from public signup. When true,
    # local password login (``POST /auth/login``) works even while
    # ``auth_mode == "oidc"`` — WITHOUT opening public self-service signup
    # (``self_signup_enabled``). This is the "operator-provisioned + invited
    # accounts can sign in natively alongside Google SSO, but the public
    # cannot self-register" posture: accounts are created only via
    # ``daalu create-admin`` or accepted invites, then log in with a password.
    # ``self_signup_enabled`` still implies native login (kept for back-compat).
    native_password_login_enabled: bool = False
    # How long an email-verification link stays valid.
    signup_token_expire_hours: int = 48
    # Confidential OIDC client the hub uses for the authorization-code flow.
    # Provisioned declaratively in the platform repo (cluster-defs → realm
    # ``daalu``) so a fresh Keycloak install has it; the secret rides the
    # SOPS-managed ``daalu-automation-secrets``.
    keycloak_hub_client_id: str = "daalu-hub-ui"
    keycloak_hub_client_secret: str = ""
    # Absolute callback URL registered as a redirect URI on the hub client.
    # Empty → derived from the inbound request as
    # ``<scheme>://<host>/api/v1/auth/oidc/callback`` (works behind the
    # ops.daalu.io ingress which sets X-Forwarded-*).
    keycloak_hub_redirect_url: str = ""
    # Post-logout landing (RP-initiated logout sends the browser here after
    # Keycloak clears its SSO session). Empty → ``/login`` on the hub origin.
    keycloak_hub_post_logout_url: str = ""
    # Comma-separated Keycloak realm-role names that grant Nautobot/NV-CM
    # superuser (is_superuser+is_staff) on login — fed to each tenant's
    # ``nautobot.rbac.superuserGroups`` (matched against the JWT ``roles`` claim
    # by nv_config_manager_auth.jwt_authentication). Assign the role to admins
    # in Keycloak; daalu-hub-nvcm carries it via its realm-roles mapper.
    keycloak_nvcm_superuser_roles: str = "nvcm-superuser"
    # Comma-separated realm roles the ensure-keycloak-sso Job creates and
    # grants to the daalu-hub-nvcm SERVICE ACCOUNT — the identity every
    # tool-proxy request falls back to when there is no per-user token
    # (password-login sessions, dead krt). Without these, tools deny access
    # ("Access Denied" in Nautobot) even though the JWT authenticates fine.
    # Superuser roles above are ensured too; this list adds the NV-CM
    # machine-path roles.
    keycloak_nvcm_service_roles: str = "nvcm-deploy-execute,nvcm-superuser"
    # The confidential client the hub authenticates AS when it calls a
    # tenant's NV-CM (tool proxy, SoT client, executor). One shared service
    # identity across tenants — ``setup-keycloak.sh`` creates it and grants it
    # ``keycloak_nvcm_service_roles``.
    keycloak_nvcm_client_id: str = "daalu-hub-nvcm"
    # Client *secret* for keycloak_nvcm_client_id. Secret material → rides the
    # SOPS-managed daalu-automation-secrets. Set this and every provision
    # stamps the credential onto the tenant's ``config_manager`` Integration
    # automatically; leave it empty and the credential has to be written by
    # hand after each provision, which surfaces as "integration has no
    # Keycloak service credentials" on every tool console.
    keycloak_nvcm_client_secret: str = ""
    # When true an OIDC login whose email has no daalu User row is rejected
    # (the safe default — users come from invites + the mirror). Set true only
    # if you want first-OIDC-login to auto-create a User (needs a default
    # tenant); leave false in multi-tenant prod.
    oidc_auto_provision_users: bool = False

    # ── Keycloak Admin (provision SSO objects + mirror users) ─────────────────
    # Admin credentials the hub uses to ensure the ``daalu-hub-ui`` client +
    # the Google identity provider (``daalu ensure-keycloak-sso``, run as a
    # Helm post-install/upgrade Job so a fresh install self-provisions) and to
    # mirror daalu users into the realm (invite-redemption + the
    # ``backfill-keycloak-users`` task). Two auth modes, checked in order:
    #   1. client-credentials — a confidential service-account client holding
    #      ``manage-users``/``manage-clients``/``manage-identity-providers``
    #      (set ``keycloak_admin_client_id`` + ``_secret``); or
    #   2. password grant — a realm admin user via ``admin-cli`` (set
    #      ``keycloak_admin_username`` + ``_password``), matching how the
    #      platform bootstrap authenticates.
    # Empty (both) disables admin ops (the realm is then managed out of band).
    # Realm + base URL derive from ``keycloak_issuer_url`` when left empty
    # (``https://<host>/realms/<realm>``); ``keycloak_admin_realm`` is the
    # realm the *admin* authenticates against (``master`` for a master admin).
    keycloak_admin_client_id: str = ""
    keycloak_admin_client_secret: str = ""
    keycloak_admin_username: str = ""
    keycloak_admin_password: str = ""
    keycloak_admin_realm: str = "master"
    keycloak_admin_cli_client_id: str = "admin-cli"
    keycloak_realm: str = ""
    keycloak_admin_base_url: str = ""
    # Mirror users into Keycloak when they redeem an invite. Independent of
    # ``auth_mode`` so the mirror can be warmed up before the OIDC cutover.
    oidc_mirror_users: bool = False

    # ── Google sign-in (Keycloak identity-provider brokering) ─────────────────
    # When both are set, ``daalu ensure-keycloak-sso`` registers a Google IdP
    # on the realm so the Keycloak login page shows "Sign in with Google".
    # Create the OAuth client at console.cloud.google.com with the authorized
    # redirect URI ``<issuer-host>/realms/<realm>/broker/google/endpoint``.
    # Secret rides the SOPS-managed daalu-automation-secrets. Empty → no IdP.
    keycloak_google_client_id: str = ""
    keycloak_google_client_secret: str = ""
    # Directory holding the vendored pinned NV-CM charts (one subdir per
    # version: ``nv-config-manager-<version>/``). Relative paths resolve
    # against the repo root / controller working dir.
    config_manager_chart_dir: str = "deploy/charts"
    # Pinned chart version the controller installs by default (the
    # vendored subdir is ``nv-config-manager-<this>``). Per-tenant rows
    # may override via their ``chart_version`` column.
    config_manager_default_chart_version: str = "1.3.0"
    # Harbor registry the NV-CM images are mirrored into; overrides the
    # chart's upstream ``registry.example.com/nvidia`` placeholders. Empty
    # uses the chart defaults (dev only).
    config_manager_harbor_registry: str = ""
    # Optional chart-level imagePullSecret name for the NV-CM release. Empty =
    # no pull secret injected (the host cluster pulls from Harbor without one
    # today; a dangling reference would wedge pods). Used by deployer_config.
    config_manager_image_pull_secret: str = ""
    # Install NV-CM via the vendored upstream Deployer (pre-creates secrets +
    # namespace before helm, breaking the secret-assembler pre-install-hook
    # deadlock) instead of the legacy bare ``helm upgrade --install``. Default
    # False so the switch is opt-in per environment; flip True once the host
    # cluster's Harbor mirror + Tier-A singletons are in place. See engineer
    # chapter 64 and docs/design/nv-config-manager-integration.md.
    config_manager_use_deployer: bool = False
    # Skip the Tier-A ``host_cluster_ready`` precheck (Envoy Gateway /
    # cert-manager / CNPG must be present before a tenant install). Leave
    # False in production; set True only for dev clusters where you
    # knowingly accept a partial Tier-A. See engineer chapter 64 §64.2/§64.6.
    config_manager_skip_host_precheck: bool = False
    # Anthropic wholesale rates (May 2026 list pricing — Sonnet 4.6).
    # Used to value externally-bought tokens; not the price we *charge*
    # tenants. Tenant-facing pricing lives on the Sku rows.
    llm_anthropic_price_in_per_mtok: float = 3.00
    llm_anthropic_price_out_per_mtok: float = 15.00
    llm_anthropic_haiku_price_in_per_mtok: float = 0.80
    llm_anthropic_haiku_price_out_per_mtok: float = 4.00
    # OpenAI-compatible classifier rates (DeepSeek default — adjust if you
    # repoint llm_base_url at Together/DeepInfra/etc.).
    llm_openai_compat_price_in_per_mtok: float = 0.60
    llm_openai_compat_price_out_per_mtok: float = 2.40

    # ── Hugging Face ─────────────────────────────────────────────────────────
    # Read token from https://huggingface.co/settings/tokens, used by vLLM
    # to download the gated Meta Llama 3.1 8B Instruct AWQ weights. Mounted
    # as a Kubernetes Secret into the vLLM Deployment; never read by the
    # daalu-api itself. The previous `ngc_api_key` field was removed when
    # the stack moved off NIM — no NGC entitlement is required any more.
    hf_token: str = ""

    # ── Briefings ────────────────────────────────────────────────────────────
    daily_briefing_cron: str = "0 7 * * 1-5"
    briefing_lookback_hours: int = 24

    # ── Monitoring ingest ────────────────────────────────────────────────────
    # Cadence (seconds) for the Celery-beat task that pulls active firing
    # alerts from each tenant's Alertmanager (the `prometheus` integration)
    # and emits `infra.alert.fired` events. Re-fires of an already-open alert
    # are cheap (the agent bumps the occurrence count without re-running LLM
    # triage), so a steady poll is fine. Tenants without a `prometheus` row
    # are skipped by the adapter (no-op). 120s keeps the Alerts page within
    # ~2 min of Alertmanager without flooding AlertOccurrence rows.
    prometheus_ingest_period_s: int = 120

    # ── Notifications ────────────────────────────────────────────────────────
    slack_webhook_url: str = ""
    slack_briefing_channel: str = "#operations"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "ops@daalu.io"

    # ── Integration: IT / SRE ────────────────────────────────────────────────
    prometheus_url: str = ""
    pagerduty_api_token: str = ""
    pagerduty_routing_key: str = ""
    datadog_api_key: str = ""
    datadog_app_key: str = ""

    # ── WireGuard tunnel coordinator ─────────────────────────────────────────
    # Public hostname (no scheme, no port) customer-side edges hit to
    # start the WireGuard handshake. Should resolve to the node running
    # the wireguard-hub Deployment. Baked into the install snippet as
    # hub.endpoint. Empty disables UI-driven onboarding (a noisy 503 with
    # a "set wireguard_public_endpoint" hint, vs. a silent half-broken row).
    wireguard_public_endpoint: str = ""
    # Public URL the customer-side edge POSTs its bootstrap callback to.
    # Distinct from wireguard_public_endpoint (which is the UDP hub host
    # for WireGuard traffic) — this is the HTTPS ingress for the daalu-api
    # Service, baked into the install snippet as --set operator.apiUrl=.
    # Include scheme; no trailing slash. Empty disables onboarding with a
    # clear 503 (same pattern as wireguard_public_endpoint).
    operator_api_public_url: str = ""
    wireguard_listen_port: int = 51820
    wireguard_hub_address: str = "10.200.0.1"
    # Fallback used by clusters._load_hub_pubkey when the in-cluster
    # Secret read fails. Kept in sync with the actual hub keypair via
    # the SOPS-encrypted Secret (WIREGUARD_HUB_PUBLIC_KEY). Empty means
    # the install snippet shows `<hub-pubkey-unavailable>` instead.
    wireguard_hub_public_key: str = ""
    # Health classifier windows used by the beat task to map
    # last_handshake_at age to ClusterTunnelStatus.
    wireguard_connected_window_s: int = 180
    wireguard_degraded_window_s: int = 600

    # ── Source of Truth / reconciler ─────────────────────────────────────────
    # Cadence (seconds) for the Celery-beat drift detector that compares
    # observed device state to SoT-intended state. Global across tenants
    # for v1; per-tenant cadence is a PR-2 concern.
    sot_reconcile_period_s: int = 300
    # Required scope claim on executor JWTs. ChangeProposal.execute() refuses
    # any actor whose scope doesn't match — that's the gate that keeps the
    # engine (which mints user-scope tokens) from ever pushing config to a
    # device. Disjoint from user JWTs so prompt-injection on the LLM agent
    # cannot smuggle execute-rights into its own session.
    executor_jwt_scope: str = "executor"
    # Cadence (seconds) for the executor beat task that polls approved-
    # unexecuted proposals. 30s = bounded 5-60s latency between approve and
    # execute, acceptable because humans approve and the bound is set by
    # this knob. Lower values increase load on Postgres without delivering
    # meaningful UX gain.
    executor_period_s: int = 30
    # Maximum number of approved proposals the executor processes per tick.
    # Bounds worst-case tick duration; the next tick picks up the rest.
    executor_batch_size: int = 25
    # Celery queue name dedicated to the executor pool. Routed via
    # task_routes in celery_app.py so the main worker pool (consuming the
    # default "celery" queue) cannot accidentally execute these tasks even
    # if its code imports them. The executor Deployment's worker process
    # is the only thing subscribed to this queue.
    executor_queue_name: str = "executor"
    # NETCONF commit-confirmed timeout, in seconds. Junos and IOS-XR
    # adapters issue ``commit confirmed <N>`` then immediately re-confirm
    # with a plain commit — if the second call never arrives the device
    # auto-rolls back after this window. 600 gives plenty of slack for
    # queue latency on a slow link; tighten if on-call wants a faster
    # blast-radius bound.
    commit_confirmed_timeout_s: int = 600

    # ── Managed Nautobot (hosted SoT for tenants that opt in) ───────────────
    # When set, the /onboarding/provision-nautobot route is enabled and the
    # backend will provision a Nautobot Tenant + ObjectPermission +
    # APIToken in this instance, then write the resulting URL + token
    # into the tenant's Integration(provider="nautobot") row. Tenants
    # who BYO their own Nautobot bypass this entirely and just fill in
    # the wizard's url + token fields directly. Empty disables the
    # hosted-provisioning path with a clear 503.
    managed_nautobot_url: str = ""
    # Admin token used to create Tenants / ObjectPermissions / APITokens
    # in the managed Nautobot. Needs Nautobot's admin scope. Rotate
    # alongside any change to the managed Nautobot's super-user.
    managed_nautobot_admin_token: str = ""
    # Slug of the Nautobot user the provisioning route mints tokens *for*.
    # Each per-tenant ObjectPermission is attached to this user, so the
    # token we hand back to the tenant carries that user's perms (which
    # we have just narrowed via the ObjectPermission to their own
    # Nautobot Tenant slug). The user must already exist in Nautobot —
    # creating it is a one-time operator setup, not per-tenant.
    managed_nautobot_service_user: str = "daalu-platform"

    # ── Observability ────────────────────────────────────────────────────────
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "daalu-automation-api"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Runtime configuration — env-var-only, project-agnostic."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Temporal ─────────────────────────────────────────────────────────
    temporal_host: str = "localhost:7233"
    temporal_namespaces: str = "default"
    # Optional pretty names shown in the UI: `default:Ajani Workflows,other:Project B`
    namespace_labels: str = ""
    # TLS — set cert/key paths or leave empty for plaintext intra-cluster.
    temporal_tls_cert: str = ""
    temporal_tls_key: str = ""

    # ── Branding ─────────────────────────────────────────────────────────
    brand_name: str = "Workflows"
    brand_subtitle: str = "orchestration console"
    brand_accent: str = "#7aa2ff"
    # Python format-string; `{workflow_id}` is substituted at link time.
    # Empty disables the per-workflow "Open in app" button.
    deep_link_template: str = ""

    # ── OIDC (Keycloak) ──────────────────────────────────────────────────
    # Empty issuer disables auth — ONLY for local dev. In-cluster always
    # requires OIDC.
    oidc_issuer: str = ""
    oidc_client_id: str = "temporal-console"
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://localhost:8000/auth/callback"
    # Minimum required realm role. Empty = any authenticated user.
    oidc_required_role: str = ""

    # ── App ──────────────────────────────────────────────────────────────
    session_secret: str = "change-me-in-prod"
    environment: str = "development"
    log_level: str = "INFO"

    # ── Derived ──────────────────────────────────────────────────────────
    @property
    def namespaces(self) -> list[str]:
        return [n.strip() for n in self.temporal_namespaces.split(",") if n.strip()]

    @property
    def namespace_label_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in self.namespace_labels.split(","):
            if ":" not in pair:
                continue
            ns, label = pair.split(":", 1)
            ns, label = ns.strip(), label.strip()
            if ns and label:
                out[ns] = label
        return out

    @property
    def auth_enabled(self) -> bool:
        return bool(self.oidc_issuer)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def namespace_display(self, ns: str) -> str:
        return self.namespace_label_map.get(ns, ns)

    def deep_link_for(self, workflow_id: str) -> str | None:
        if not self.deep_link_template:
            return None
        try:
            return self.deep_link_template.format(workflow_id=workflow_id)
        except Exception:
            return None


@lru_cache
def get_settings() -> Settings:
    return Settings()

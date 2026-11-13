{{/* vim: set filetype=mustache: */}}

{{- define "daalu.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "daalu.labels" -}}
app.kubernetes.io/name: {{ include "daalu.fullname" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/part-of: daalu-automation
{{- end -}}

{{- define "daalu.backendImage" -}}
{{ .Values.image.registry }}/{{ .Values.image.backend.repository }}:{{ .Values.image.backend.tag }}
{{- end -}}

{{- define "daalu.frontendImage" -}}
{{ .Values.image.registry }}/{{ .Values.image.frontend.repository }}:{{ .Values.image.frontend.tag }}
{{- end -}}

{{- define "daalu.envFrom" -}}
- secretRef:
    name: {{ .Values.existingSecret }}
{{- end -}}

{{- define "daalu.commonEnv" -}}
- name: ENVIRONMENT
  value: {{ .Values.config.environment | quote }}
- name: LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: API_V1_PREFIX
  value: {{ .Values.config.apiV1Prefix | quote }}
- name: CORS_ORIGINS
  value: {{ .Values.config.corsOrigins | quote }}
- name: DAILY_BRIEFING_CRON
  value: {{ .Values.config.dailyBriefingCron | quote }}
- name: BRIEFING_LOOKBACK_HOURS
  value: {{ .Values.config.briefingLookbackHours | quote }}
# Routes the LOCAL tier through the per-tenant-quota gateway when
# set. Empty = single-tenant fallback (direct call to
# llm_local_base_url). See docs/design/daalu-hosted-gpu.md.
- name: DAALU_HOSTED_GATEWAY_URL
  value: {{ .Values.config.daaluHostedGatewayUrl | default "" | quote }}
- name: DAALU_HOSTED_UPSTREAM_URL
  value: {{ .Values.config.daaluHostedUpstreamUrl | default "" | quote }}
# Workspace controller URL (browser-IDE coding assistant). Empty
# disables the /api/v1/ide/* routes.
- name: WORKSPACE_CONTROLLER_URL
  value: {{ .Values.config.workspaceControllerUrl | default "" | quote }}
# GPU controller (AI Factory → Add GPU). Empty disables the deploy path.
- name: GPU_CONTROLLER_URL
  value: {{ if .Values.gpuController.enabled }}{{ printf "http://daalu-gpu-controller.%s.svc.cluster.local:8084" .Release.Namespace | quote }}{{ else }}""{{ end }}
# Coding models offered in the Create-Workspace UI (JSON list of catalog
# ids) + the default. Govern which vLLM-backed models are selectable.
- name: CODING_MODELS_ENABLED
  value: {{ .Values.config.codingModelsEnabled | default "[\"qwen2.5-coder-14b\"]" | quote }}
- name: CODING_MODEL_DEFAULT
  value: {{ .Values.config.codingModelDefault | default "qwen2.5-coder-14b" | quote }}
# SERVICE_TOKEN_SECRET_KEY is loaded via envFrom from the app secret
# (deploy/scripts/install.sh:113 mints the secret from .env). Don't
# duplicate it here — an explicit env: entry would override envFrom
# even if empty.
# ── Keycloak / hub SSO (human login via OIDC) ──────────────────────────
# Non-secret config only. The secrets — KEYCLOAK_HUB_CLIENT_SECRET,
# KEYCLOAK_ADMIN_PASSWORD, KEYCLOAK_ADMIN_CLIENT_SECRET,
# KEYCLOAK_GOOGLE_CLIENT_SECRET — are keys in {{ .Values.existingSecret }}
# and arrive via envFrom; do NOT list them here (an empty explicit env:
# would shadow the secret value).
- name: KEYCLOAK_ISSUER_URL
  value: {{ .Values.config.keycloakIssuerUrl | default "" | quote }}
- name: AUTH_MODE
  value: {{ .Values.config.authMode | default "password" | quote }}
- name: KEYCLOAK_HUB_CLIENT_ID
  value: {{ .Values.config.keycloakHubClientId | default "daalu-hub-ui" | quote }}
- name: KEYCLOAK_HUB_REDIRECT_URL
  value: {{ .Values.config.keycloakHubRedirectUrl | default "" | quote }}
- name: KEYCLOAK_HUB_POST_LOGOUT_URL
  value: {{ .Values.config.keycloakHubPostLogoutUrl | default "" | quote }}
- name: OIDC_MIRROR_USERS
  value: {{ .Values.config.oidcMirrorUsers | default false | quote }}
- name: KEYCLOAK_REALM
  value: {{ .Values.config.keycloakRealm | default "" | quote }}
- name: KEYCLOAK_ADMIN_BASE_URL
  value: {{ .Values.config.keycloakAdminBaseUrl | default "" | quote }}
- name: KEYCLOAK_ADMIN_REALM
  value: {{ .Values.config.keycloakAdminRealm | default "master" | quote }}
- name: KEYCLOAK_ADMIN_USERNAME
  value: {{ .Values.config.keycloakAdminUsername | default "" | quote }}
- name: KEYCLOAK_GOOGLE_CLIENT_ID
  value: {{ .Values.config.keycloakGoogleClientId | default "" | quote }}
{{- end -}}

{{- define "daalu.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 1000
runAsGroup: 1000
fsGroup: 1000
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "daalu.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end -}}

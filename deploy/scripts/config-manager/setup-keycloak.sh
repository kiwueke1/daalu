#!/usr/bin/env bash
#
# setup-keycloak.sh — create the two Keycloak clients NV-CM go-live needs.
#
# Engineer chapter 64 §64.2 step 4. Idempotent: re-running updates the
# clients in place. Uses the Keycloak admin CLI (kcadm.sh), which ships in
# the Keycloak container ("kubectl exec ... -- /opt/keycloak/bin/kcadm.sh")
# or the keycloak-admin-cli download.
#
#   1. <HUB_CLIENT_ID>  — confidential, client-credentials (service account)
#      grant. Its service-account is granted a realm role whose name lands
#      in the token's "roles" claim and maps to NV-CM's RBAC *execute* role
#      for DeployWorkflow. This is the client whose id/secret you paste onto
#      each tenant's Integration(provider="config_manager") (§64.3 step 9:
#      keycloak_client_id + keycloak_client_secret_ciphertext).
#   2. <UI_CLIENT_ID>   — public, standard browser OIDC flow, for the NV-CM
#      UI at https://<slug>.cmtools.example.com (served through the hub tool proxy).
#
# Both get audience == $AUDIENCE (must equal the hub's
# KEYCLOAK_TOKEN_AUDIENCE and NV-CM's oidc.audiences, default
# nv-config-manager) via a hardcoded-claim audience mapper.
#
#   Usage:
#     KC_URL=https://keycloak.daalu.io KC_REALM=daalu \
#     KC_ADMIN=admin KC_ADMIN_PASSWORD=... \
#     UI_REDIRECT='https://*.cmtools.example.com/*' \
#     ./setup-keycloak.sh
#
set -euo pipefail

KC_URL="${KC_URL:?set KC_URL e.g. https://keycloak.daalu.io}"
KC_REALM="${KC_REALM:-daalu}"
KC_ADMIN="${KC_ADMIN:?set KC_ADMIN}"
KC_ADMIN_PASSWORD="${KC_ADMIN_PASSWORD:?set KC_ADMIN_PASSWORD}"
KCADM="${KCADM:-kcadm.sh}"                 # path to kcadm.sh
AUDIENCE="${AUDIENCE:-nv-config-manager}"
HUB_CLIENT_ID="${HUB_CLIENT_ID:-daalu-hub-nvcm}"
UI_CLIENT_ID="${UI_CLIENT_ID:-nv-config-manager-ui}"
EXECUTE_ROLE="${EXECUTE_ROLE:-nvcm-deploy-execute}"   # realm role -> roles claim
UI_REDIRECT="${UI_REDIRECT:-https://*.cmtools.example.com/*}"

kc() { "$KCADM" "$@"; }

echo "logging into $KC_URL realm=$KC_REALM"
kc config credentials --server "$KC_URL" --realm master \
  --user "$KC_ADMIN" --password "$KC_ADMIN_PASSWORD"

# Realm role that becomes the NV-CM execute role via the roles claim.
kc create roles -r "$KC_REALM" -s name="$EXECUTE_ROLE" \
  -s 'description=NV-CM DeployWorkflow execute (hub service identity)' 2>/dev/null \
  && echo "created role $EXECUTE_ROLE" || echo "role $EXECUTE_ROLE exists"

upsert_client() {
  local cid="$1"; shift
  local existing
  existing="$(kc get clients -r "$KC_REALM" -q clientId="$cid" --fields id --format csv --noquotes 2>/dev/null | tail -n +1 | head -1 || true)"
  if [[ -n "$existing" ]]; then
    echo "updating client $cid ($existing)"
    kc update "clients/$existing" -r "$KC_REALM" "$@"
    echo "$existing"
  else
    echo "creating client $cid"
    kc create clients -r "$KC_REALM" -s clientId="$cid" "$@" -i
  fi
}

# 1) Hub service client — confidential, client-credentials + token-exchange.
#    standard.token.exchange.enabled lets the hub exchange a user's Keycloak
#    token into an nv-config-manager-audience *user* token (carrying the user's
#    identity) so the tool proxy logs the user into NV-CM tool UIs as themselves
#    (api/tool_proxy.py get_user_nvcm_token). Without it the exchange 400s and
#    the proxy falls back to the shared service identity.
HUB_UUID="$(upsert_client "$HUB_CLIENT_ID" \
  -s enabled=true -s protocol=openid-connect \
  -s publicClient=false -s serviceAccountsEnabled=true \
  -s standardFlowEnabled=false -s directAccessGrantsEnabled=false \
  -s 'attributes."standard.token.exchange.enabled"=true' | tail -1)"

# Audience mapper so NV-CM's SecurityPolicy accepts the token.
kc create "clients/$HUB_UUID/protocol-mappers/models" -r "$KC_REALM" \
  -s name=nvcm-audience -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper \
  -s 'config."included.client.audience"='"$AUDIENCE" \
  -s 'config."access.token.claim"=true' 2>/dev/null \
  && echo "  + audience mapper" || echo "  audience mapper exists"

# Realm-roles mapper → emits a top-level "roles" claim on the per-user
# token-exchange result. nv_config_manager_auth.jwt_authentication reads this
# claim and grants Nautobot superuser when a role matches the tenant's
# nautobot.rbac.superuserGroups (config.keycloak_nvcm_superuser_roles, e.g.
# nvcm-superuser). Without it the exchanged token only has realm_access.roles
# and the plugin sees no groups → every SSO user lands RBAC-stripped.
kc create "clients/$HUB_UUID/protocol-mappers/models" -r "$KC_REALM" \
  -s name=realm-roles -s protocol=openid-connect \
  -s protocolMapper=oidc-usermodel-realm-role-mapper \
  -s 'config."claim.name"=roles' \
  -s 'config."jsonType.label"=String' \
  -s 'config.multivalued=true' \
  -s 'config."access.token.claim"=true' 2>/dev/null \
  && echo "  + realm-roles mapper" || echo "  realm-roles mapper exists"

# Grant the execute realm role to the hub's service account.
SVC_USER="service-account-${HUB_CLIENT_ID}"
kc add-roles -r "$KC_REALM" --uusername "$SVC_USER" --rolename "$EXECUTE_ROLE" \
  && echo "  + granted $EXECUTE_ROLE to $SVC_USER" || true

HUB_SECRET="$(kc get "clients/$HUB_UUID/client-secret" -r "$KC_REALM" --fields value --format csv --noquotes 2>/dev/null | tail -1 || true)"

# 2) UI client — public, browser flow.
upsert_client "$UI_CLIENT_ID" \
  -s enabled=true -s protocol=openid-connect \
  -s publicClient=true -s standardFlowEnabled=true \
  -s "redirectUris=[\"$UI_REDIRECT\"]" \
  -s 'attributes."pkce.code.challenge.method"=S256' >/dev/null

echo
echo "================ paste onto the tenant's config_manager Integration ================"
echo "  keycloak_client_id     = $HUB_CLIENT_ID"
echo "  keycloak_client_secret = ${HUB_SECRET:-<read from Keycloak UI: Clients > $HUB_CLIENT_ID > Credentials>}"
echo "  (hub settings) KEYCLOAK_ISSUER_URL    = $KC_URL/realms/$KC_REALM"
echo "  (hub settings) KEYCLOAK_TOKEN_AUDIENCE = $AUDIENCE"
echo "===================================================================================="

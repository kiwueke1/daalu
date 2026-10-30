# Part XIII — Auth Integrations

How a logged-in `ops.example.com` operator reaches a tenant's **private** NV-CM
tool UIs (Nautobot, Render, Workflow, Config-store, the config-browser UI)
*through the hub* and lands there **logged in as themselves** — the "cmtools"
reverse-proxy plane and the per-user Keycloak token-exchange that powers it.

This part **consolidates** the cmtools/auth-integration story and
cross-references — rather than duplicates — the chapters that already own the
pieces it builds on: hub auth ([5](../02-backend-architecture/05-auth-system.md),
[56](../10-security/56-auth-flows.md), [59](../10-security/59-keycloak-hub-sso.md)),
tenant isolation ([57](../10-security/57-tenant-isolation.md)), executor and
machine-token identity ([17](../04-source-of-truth/17-executor-identity.md),
[21](../04-source-of-truth/21-nvcm-network-plane.md),
[23](../04-source-of-truth/23-nvcm-internals-and-operator.md)), the WireGuard
tunnel ([24](../05-cluster-federation/24-wireguard-hub.md),
[25](../05-cluster-federation/25-daalu-edge.md)), and the TLS/DNS substrate
([53](../09-operations-runbooks/53-tls-and-dns.md)).

If you're debugging a cmtools `401`/`403`/`502` in production, start at
chapter 67's map, then jump to the owning chapter.

---

## Chapters

67. [Auth integrations — overview & map](67-auth-integrations-overview.md)
    — which identity authenticates which surface, where each lives, and the
    cmtools request flow at a glance; the navigational opener for the part.
68. [NV-CM tool SSO through the hub (cmtools)](68-nvcm-tool-sso-through-the-hub.md)
    — the flagship: the flat `*.cmtools.example.com` scheme, the `CmtoolsHostRouter`
    outer ASGI middleware, `tool_proxy.py` (parse/authz/resolve), the tenant
    check, tunnel forwarding via `X-Daalu-Forward-Host`, and the per-tenant
    gateway `SecurityPolicy`.
69. [Per-user token-exchange](69-per-user-token-exchange.md)
    — the downstream Bearer: refresh through `daalu-hub-ui`, Keycloak Standard
    Token Exchange through `daalu-hub-nvcm` (no `audience` param), where `krt`
    is stored, token caching, and the service-token fallback.
70. [Keycloak realm, clients & provisioning](70-keycloak-realm-clients-and-provisioning.md)
    — the single source of truth for what must exist in realm `daalu`: the three
    clients, the two audience-mappers, `standard.token.exchange.enabled`, redirect
    URIs, and how `setup-keycloak.sh` / `ensure-keycloak-sso` make them durable.
71. [Reachability: DNS, TLS, firewall & the tunnel](71-auth-reachability-dns-tls-tunnel.md)
    — the network/PKI substrate: the `*.cmtools.example.com` wildcard DNS + LE
    wildcard cert, `auth.example.com` proxied (NAT-hairpin), the host
    firewall, and the restart-safe `daalu-edge` (persistent PVC, the 409 re-peer).
73. [Native password login & operator accounts](73-native-password-login-and-operator-accounts.md)
    — the email+password login that runs **alongside** Google SSO without
    opening public signup: the three independent switches (`auth_mode`,
    `native_password_login_enabled`, `self_signup_enabled`), how accounts are
    provisioned (`daalu create-admin` + accepted invites), and the `/accept-invite` public-route fix.

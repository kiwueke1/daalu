# 79. Live fabric topology & the in-browser device terminal

The `/network/topology` page renders the tenant's switch/server fabric as a
live, clickable graph: nodes from the source of truth, adjacency from an
observed **LLDP sweep over SSH**, health from device reachability — with a
node drawer showing live neighbours and a **click-to-SSH terminal** straight
into a fabric device. Shipped as PR #105 and hardened by #128/#129/#131/#133/#134
during the first live sweep against a Spectrum-X containerlab fabric.

Everything is tenant-scoped and gated behind
`tenant.feature_flags["network_topology_enabled"]`
(`api/routers/topology.py:29`, `TOPOLOGY_FLAG`) — like the rest of the network
plane it is opt-in per tenant.

| Piece | Path |
|---|---|
| Graph composition | `core/topology/service.py` (`build_topology`) |
| LLDP sweep | `core/topology/lldp.py` (`collect_observations`) |
| Graph/link models | `core/topology/models.py` |
| REST surface | `api/routers/topology.py` (`GET /topology`, `GET /topology/nodes/{id}`) |
| SSH terminal | `api/device_terminal.py` (Starlette `WebSocketRoute`) |
| Tunnel-aware SSH dial | `core/cluster_proxy.py:open_connect_socket`, `core/change_proposals.py:_device_tunnel_proxy` |
| Frontend | `frontend/app/network/topology/page.tsx` (SVG graph + xterm.js) |
| Intended cabling | `core/sot/nautobot.py:list_cabling` (line 522) |

---

## 79.1 Graph composition: intended ⨯ observed

`build_topology` (`core/topology/service.py:118`) composes four inputs:

1. **Nodes** — Nautobot devices via the SoT adapter (chapter 15). `_tier_for`
   places each in a fabric tier (`gateway`/`spine`/`leaf`/`server`) by
   role/tags/name/platform, best-effort, for layout.
2. **Intended cabling** — `NautobotSoT.list_cabling()`, a best-effort GraphQL
   read of Nautobot's cable objects. This baseline is what lets a missing link
   be called *down* rather than merely *unseen*.
3. **Observed adjacency** — the LLDP sweep (§79.2).
4. **Telemetry** — per-node reachability from the same sweep, folded onto the
   node as a heartbeat (`reachable`/`lldp_ok`, or `None` = "not probed", drawn
   neutral).

Edges are canonicalized (`_canonical`/`_link_id`) so the two directions of one
cable collapse into one link, then frozen into a status
(`core/topology/models.py:19`, `LinkStatus`):

| Status | Meaning (`service.py:190-211`) |
|---|---|
| `up` | observed **and** intended (or no cabling plan exists at all) |
| `mgmt` | observed, not intended, and **both** endpoint interfaces are management ports |
| `mismatch` | observed on the wire but not in the cabling plan ("miscabled") |
| `down` | intended, both endpoints probed reachable, no LLDP neighbour observed |
| `unknown` | not observed and an endpoint wasn't probeable — neutral, never red |

The design rule underneath: **every external read is best-effort, and a probe
outage degrades to intended inventory — never to a false-red fabric outage.**
Nautobot down → empty graph with a `degraded` reason; a device unreachable →
its links go `unknown`, not `down`.

### The `mgmt` status (PR #133)

The shared out-of-band management network (eth0/mgmt0/… on every node) shows
up in LLDP as *real* adjacency between arbitrary node pairs — early versions
painted those as `mismatch`, which read as a miswired fabric ("phantom
spine↔server cables") when the fabric was clean. `_is_mgmt_edge`
(`service.py:48`) classifies an observed-not-intended edge whose **both**
endpoint interfaces match `_MGMT_IF_RE`
(`^(eth0|mgmt\d*|ma1|fxp0|em0|management\d*)$`, case-insensitive — Cumulus/
SONiC/generic Linux, SONiC `ma1`, Junos `fxp0`/`em0`, EOS/NX-OS
`Management*`) as `mgmt`: rendered faint short-dash with its own legend entry
and stats count. Requiring *both* ends keeps a genuinely miscabled
fabric-port↔mgmt-port link classified as `mismatch`.

The legend is **style-aware** (also #133): each status is drawn with the
graph's exact stroke treatment (dotted animated = up, dashed = down, solid =
miscabled, faint short-dash = mgmt), not a bare colour chip — the dash pattern
carries as much meaning as the hue, and colour-only legends were misread.

---

## 79.2 The LLDP sweep

`core/topology/lldp.py:collect_observations` SSHes every device and reads
`lldpctl -f keyvalue` — the most parse-stable lldpd output, one
`lldp.<localif>.<key>=<value>` per line. Cumulus/NVUE, SONiC and plain Linux
hosts all run lldpd, so one code path covers switches *and* servers.

Operationally bounded by construction (`lldp.py:36-42`): all devices probed
concurrently behind a semaphore (`_MAX_CONCURRENCY=12`), hard per-host timeout
(`_PER_HOST_TIMEOUT_S=6.0`), and a per-tenant TTL cache
(`_CACHE_TTL_S=8.0`) so the UI's ~10 s polling never triggers more than one
underlying SSH sweep per TTL — and a page load never hangs on one dead box.

Credentials come from the standard resolver
(`core/change_proposals.py:resolve_credentials` via `_ssh_creds`,
`lldp.py:146-166`), filtered to genuinely SSH-shaped results — a
`config_manager`-transport device resolves to an NV-CM API handle (port 443,
`nvcm_conn` in `extra`) which you can't `lldpctl` over, so it's skipped rather
than dialled wrong.

Two live-sweep findings, both now handled:

- **`sudo -n` fallback (PR #131).** `lldpctl` is root-gated for the login user
  on Cumulus, and real Cumulus ships NOPASSWD sudo for the admin user. When
  the plain run yields nothing and `creds.sudo` is set, the probe retries
  `sudo -n lldpctl -f keyvalue` (`lldp.py:124-133`) — matching the
  `linux_ssh` adapter's existing sudo convention. Hosts whose user can read
  lldpd directly (the `_lldpd` group) never hit the retry; hosts with no sudo
  fall through to "reachable, but no adjacency" with the stderr recorded.
- **Non-interactive PATH has no `/usr/sbin`** (commit 3cfe6c7). The probe's
  non-interactive SSH session gets a PATH without `/usr/sbin`, so `lldpctl`
  was "command not found" on lab hosts even with group access in place. The
  lab symlinks it into `/usr/bin` in the clab `exec` stanzas; on real gear
  `lldpctl` is normally in the default PATH.

A reachable device with no LLDP output is reported `reachable=True` with
empty neighbours and the stderr as `error` — the graph treats that as "no
adjacency data", not a red link.

---

## 79.3 Tunnel-aware SSH: `Credentials.proxy_url` + `open_connect_socket` (PR #128)

Devices onboarded into a tunneled NV-CM environment carry private IPs (e.g. a
containerlab mgmt net `172.20.20.0/24`) that only route *inside* the
customer cluster — the hub's LLDP probe, device terminal and `linux_ssh`
adapter all used to dial them directly and could never reach them, so
topology links stayed neutral and the terminal always failed.

The daalu-edge forward proxy already splices raw TCP via HTTP `CONNECT` (and
its allowlist covers RFC1918); SSH just never used it. Two pieces close the
gap:

- **`core/cluster_proxy.py:open_connect_socket(proxy_url, host, port)`**
  (line 52) — performs the `CONNECT` handshake on a **raw socket** and hands
  the spliced socket to `asyncssh.connect(sock=...)`. The reply headers are
  read **byte-wise**: the upstream is an SSH server, which *speaks first*, so
  its banner can share a packet with the proxy's `200` — a buffered read
  would swallow it. Non-200 (e.g. the edge's allowlist 403) raises
  `ConnectionError`; the socket is always closed on failure.
- **`resolve_credentials` stamps `Credentials.proxy_url`**
  (`core/device/models.py:46`) for `linux_ssh` and network-NOS devices via
  `_device_tunnel_proxy` (`core/change_proposals.py:531-570`), when **both**:
  the device's primary IP is private (`ipaddress.ip_address(...).is_private` —
  public IPs keep the direct dial; the edge allowlist would reject them
  anyway), **and** the tenant's `config_manager` environment has a
  **connected** tunnel. A NULL-tunnel (hub-side) environment keeps the direct
  dial, which reaches it.

Consumers get this for free: the LLDP probe (`lldp.py:115-121`), the device
terminal, and the `linux_ssh` adapter — so **executor pushes and drift checks
gain the same reachability**, not just the graph.

---

## 79.4 The two breaks that kept the sweep dead (PR #129)

Even with #128 in place, the first live sweep stayed dead. Two independent
bugs:

### Credential seal-on-write

The onboarding panel's SSH form submits plaintext `password`/`private_key`
(browsers can't encrypt client-side); `upsert_tenant_config` stored them
verbatim — but the resolvers read **only** the `*_ciphertext` shape. A
UI-saved credential was therefore simultaneously (a) unencrypted at rest,
(b) invisible to the resolver ("no SSH credentials to probe devices" on the
topology page), and (c) echoed back **un-redacted** on GET.

Fix: `_SEAL_ON_WRITE` (`api/routers/integrations.py:97-108`) maps plaintext →
ciphertext keys for the `ssh_credentials` / `redfish_credentials` /
`network_credentials` providers; `_seal_credential_config` encrypts on write
(`core/crypto.encrypt_secret`), and reads redact **both** shapes. The
pre-existing prod row was hand-sealed. If you add a new credential provider,
add its mapping here — the resolver-vs-storage shape mismatch is otherwise
silent.

### The edge `CONNECT` handler was dead code

`deploy/edge/proxy.py`'s CONNECT support had two latent bugs that meant it
had *never* run:

1. A `CONNECT` request's authority-form target (`host:port`, no leading
   slash) never matches an aiohttp wildcard route `/{tail:.*}` — aiohttp
   404s before the dispatch closure runs. Fix: intercept `CONNECT` in a
   **middleware** (`proxy.py:213-225`), which runs even on route-resolution
   failure, and take the authority from `request.raw_path`
   (`proxy.py:157-159`; `path_qs` comes up empty — there is no path to
   parse).
2. The handler read `protocol.reader`, which aiohttp's `RequestHandler`
   doesn't expose. Fix: detach aiohttp by swapping the transport's protocol
   for a plain `asyncio.StreamReaderProtocol` (`proxy.py:189-194`) before
   splicing, so post-CONNECT bytes arrive raw instead of dying in aiohttp's
   parser buffer.

Verified against the real module: 200 + server-first banner preserved +
bidirectional bytes + allowlist 403. The lesson for reviewers: an aiohttp
"route" for CONNECT compiles and deploys fine and simply never fires — test
the actual method, not the handler in isolation.

---

## 79.5 The in-browser device terminal

`api/device_terminal.py` bridges the browser's xterm.js to an `asyncssh` PTY
on a fabric device. It is a **Starlette `WebSocketRoute`** on the API main
app — `WebSocketRoute("/ws/device-terminal/{device_id}", …)`
(`api/main.py:331`) — not a FastAPI route, because the WS upgrade needs the
raw ASGI surface (same reason as `/ide`, chapter 75).

Security posture (a browser shell into customer gear — deliberately tight,
`device_terminal.py:_authorize`): valid session cookie → **tenant admin**
(`is_admin`) → `network_topology_enabled` flag → the device must resolve in
*that tenant's own* SoT (cross-tenant → 1008 close) → SSH-shaped credentials
required (NV-CM-only devices have no shell and are refused). The wire
protocol is JSON text frames browser→server (`{"t":"i","d":…}` input,
`{"t":"r","c":…,"r":…}` resize) and raw text frames server→browser.

### The `/ws` ingress rule (PR #134)

Because the route lives at `/ws/...` — *not* under `/api` — it needs its own
ingress path, exactly like `/ide`. It didn't have one: the WS upgrade fell
into the `/` catch-all → the Next.js frontend → 404, so the topology SSH
button fails in prod while working fine in dev. The `/ws` prefix
is now routed to `daalu-api` in `deploy/k8s/frontend/ingress.yaml`. If you
maintain a kustomize overlay that patches the ingress, remember a patch
**replaces the path list wholesale** (kustomize patch semantics), so a new
path added only to the base silently vanishes in the overlay.

---

## 79.6 Frontend

`frontend/app/network/topology/page.tsx`: an SVG fabric graph (emerald = up,
critical = down, warning = miscabled, faint = mgmt/unknown), tier-based
layout, a node drawer (SoT facts + live LLDP neighbours from
`GET /topology/nodes/{id}` — served from the same cached sweep, so opening
the drawer costs no extra SSH), and the xterm.js terminal. Topology is not a
sidebar entry: it belongs to one NV-CM environment, so it is reached from
**Managed infra → Network & servers → (environment) → Topology**
(`internalLinks` in `frontend/components/managed-infra/network-view.tsx`,
which appends `?env=<slug>&env_name=<name>`) or from the "Onboard your
devices" panel. `/network/topology` still resolves directly. The xterm imports are **type-only** (erased at build; the runtime
module is dynamically imported) because `next lint` doesn't register
`@typescript-eslint` rules — plus a `*.css` ambient module declaration for
xterm's stylesheet under `moduleResolution: bundler`.

The env detail card's console tiles are related but separate — see
[68 §68.9](../13-auth-integrations/68-nvcm-tool-sso-through-the-hub.md) for
the `ui` URL-key fix (PR #135).

---

## 79.7 Pointing it at the lab

The feature was built against a Spectrum-X containerlab fabric. Lab-specific
facts that bite during bring-up — encode them in the clab `exec` stanzas so
boot-time redeploys keep them:

- CVX switch images (unlike real Cumulus) ship **no NOPASSWD sudo** — the
  exec stanzas add a sudoers entry for the `cumulus` user.
- The GPU-host image had root-only login: the stanzas create the `cumulus`
  user (the tenant-wide credential), grant `_lldpd` group access (no sudo on
  that image), and start `lldpd`.
- `lldpctl` is symlinked into `/usr/bin` (non-interactive PATH lacks
  `/usr/sbin`).
- The clab mgmt net `172.20.20.0/24` is **host-local to the lab host** — the hub
  never routes to it directly; every dial goes through the edge CONNECT
  proxy (§79.3). On host reboot, redeploy the lab with
  `containerlab deploy --reconfigure` (containers use restart-policy `no`
  and their veths die; `docker start` does not recover them).

---

## 79.8 Failure-mode cheat sheet

| Symptom | Cause / where to look |
|---|---|
| Every link neutral, nodes "not probed" | No SSH credentials resolve — check the seal-on-write shapes (§79.4) and that the onboarding panel saved into `*_ciphertext` |
| `reachable=False` on everything private-IP'd | Edge CONNECT path — tunnel down, or an old edge image predating the #129 middleware fix |
| Reachable but zero neighbours, stderr "command not found" | `lldpctl` not in the non-interactive PATH (§79.2) |
| Reachable but zero neighbours, empty output | Root-gated `lldpctl` and no `sudo -n` (§79.2); check `creds.sudo` |
| Spine↔server "Miscabled" links that make no sense | Pre-#133 build — mgmt adjacency now classifies as `mgmt` |
| Terminal button 404s in prod, works in dev | `/ws` ingress path missing (§79.5) — check any kustomize **overlay patch**, not just the base |
| Terminal closes 1008 immediately | Not a tenant admin, flag off, cross-tenant device, or an NV-CM-only device |
| `GET /sot/devices` 500 / `topology.inventory_failed` | Nautobot 2.x GraphQL rename — see chapter 15's 2.x compatibility section |

---

## What's next

Chapter 15 covers the SoT adapter (inventory, the Nautobot 2.x quirks, the
gateway-dial machine path this feature reads through); chapter 29 the edge
data plane the CONNECT proxy belongs to; and
[68](../13-auth-integrations/68-nvcm-tool-sso-through-the-hub.md) the sibling
*human* path into the tools.

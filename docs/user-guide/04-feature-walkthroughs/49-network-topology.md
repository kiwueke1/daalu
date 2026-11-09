# 49. Fabric topology — the live network map

**Topology** (`https://ops.example.com/network/topology`, opened from
**Managed infra → Network & servers → your environment → Consoles**) is a
live map of your network fabric: every device in your source of truth,
drawn as a graph whose links are coloured by what Daalu can actually
*observe* on the wire — not just what the cabling records say should be
there. Click any node to inspect it, and open an SSH session to it without
leaving the browser.

If your devices aren't onboarded yet, the page tells you so — start with
the **Onboard your devices** panel (chapter 27, §27.9) and come back.

---

## Where the picture comes from

The graph merges three sources:

1. **Intended inventory and cabling** from your source of truth — the
   nodes, and the links that *should* exist.
2. **Observed adjacency** — Daalu periodically connects to your devices
   over SSH and reads their live LLDP neighbour tables. This is what turns
   a static diagram into a statement about reality.
3. **Reachability telemetry** — whether each device answered its last
   probe.

Every external read is best-effort: if a probe times out or a device is
briefly unreachable, the graph degrades to the intended inventory (with an
amber note explaining what's stale) — it never paints a false outage.

Devices with **private management IPs** are probed through your cluster's
secure tunnel, the same path the executor uses — you don't need to expose
anything.

---

## Reading the graph

The header shows totals — *N nodes · M links · K up* — and the legend
draws each link status **with the exact stroke the graph uses**, because
the dash pattern carries as much meaning as the colour:

| Status | Stroke | Meaning |
|---|---|---|
| **Up** | dotted, animated | Adjacency observed by LLDP *and* matching intent — traffic-carrying and correct. |
| **Down** | dashed | An intended link with no observed adjacency — a dead or disconnected cable. |
| **Miscabled** | solid | Observed adjacency that *doesn't match* the intended cabling — a cable in the wrong port. |
| **Mgmt network** | faint, short-dash | Out-of-band management adjacency (see below). |
| **Unknown** | neutral | Not yet probed. |

**About the Mgmt status.** Every device's management port (`eth0`,
`mgmt0`, …) sits on a shared out-of-band network, so LLDP legitimately
sees "adjacency" between arbitrary pairs of devices there — a spine and a
server, say. That is not a miswired fabric; it's the management network
doing its job. Daalu classifies edges whose *both* endpoints are
management ports as **Mgmt** and renders them faintly, with their own
legend entry and count, so they never masquerade as miscabling.

The graph refreshes on its own every few seconds; the **Refresh** button
forces a sweep.

---

## The node drawer

Click a node and a drawer opens on the right:

- **Reachability badge** — *reachable* / *unreachable* / *not probed*,
  from the latest live probe.
- **Mgmt IP** and **Transport** — how Daalu talks to this device.
- **LLDP neighbours** — the device's live neighbour table, one row per
  local interface: `swp1 → spine01 :swp3`. This is the raw observation
  the link colours are computed from.
- **SSH into <device>** — opens the in-browser terminal (below). Greyed
  out if the device has no SSH transport.
- **Open in source of truth** — jumps to the device's inventory page.

---

## The in-browser device terminal

**SSH into <device>** opens a real terminal in a panel — a live SSH
session to the device, authenticated with your tenant's saved device
credential, streamed over a secure WebSocket through Daalu.

- You never handle the SSH key or password in the browser; Daalu injects
  the stored credential server-side.
- Devices on private management networks are reached through your cluster
  tunnel — the same reachability the config executor has.
- The terminal refuses any device outside your own tenant's source of
  truth, and access is gated by your tenant's feature flags and role.

Use it for the quick look — `nv show interface`, a ping, a log tail —
without breaking your flow into a jump host. Configuration *changes*
should still go through the proposal loop (chapter 18), where they're
diffed, approved, and auditable.

---

## Enablement

The topology plane is gated by the `network_topology_enabled` feature
flag on your tenant. If it isn't on, the page says *"Live topology isn't
enabled for this workspace yet"*, explains that a tenant admin can turn
it on, and links to **See your workspace admins in Settings**. Ask an
admin, or your Daalu contact, to enable it.

---

## See also

- Chapter 27 — provisioning the management stack and onboarding devices.
- Chapter 13 — the source-of-truth model (intent, drift, reconciliation).
- Chapter 18 — Operations: proposals and the approval loop.

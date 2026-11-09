# 30. Network source of truth (Nautobot)

If you operate a real network, Nautobot is probably already
your source of truth — or Daalu is about to run one for you.
This chapter covers both paths and what you get once connected.

**Two paths:**

1. **Bundled (zero setup).** Every management environment Daalu
   provisions (Chapter 27) includes its own Nautobot, isolated
   per customer, running in your own cluster — your inventory
   data sits at rest on storage you control. It is wired to
   Daalu **automatically**: the platform stamps the API
   credential itself and dials the service over your cluster
   tunnel. Nothing to configure, no token to paste. The *Onboard
   your devices* panel on the environment shows the live wiring
   status.
2. **Bring your own (BYO).** You already operate a Nautobot and
   want Daalu to use it. The rest of this chapter is mostly
   about this path.

---

## BYO — what you need

- A Nautobot installation reachable from Daalu (either directly
  via HTTPS, or through a federation tunnel if it lives behind
  your firewall).
- A Nautobot API token with read+write permissions for the
  models Daalu uses: `dcim.device`, `dcim.interface`,
  `ipam.ip_address`, `extras.tag`. Read-only is possible but
  limits Daalu's writes back.
- Daalu admin role to configure the integration.

---

## BYO — setup

1. *Managed infra → Integrations → Source of Truth → Add →
   Nautobot.*
2. Enter:
   - **Base URL** — e.g., `https://nautobot.acme.io`. Use your
     Nautobot's real address. If you're on the bundled path,
     don't paste the browser console's address here — that
     console authenticates with your Daalu session, not an API
     token, and the tester will refuse it with an explanation.
     (You don't need this form at all on the bundled path.)
   - **API token** — paste the token.
   - **Friendly name** — for your reference.
   - **Reachable via federation tunnel?** Tick if Daalu can't
     hit it directly.
3. Daalu probes Nautobot with a `GET /api/dcim/devices/?limit=1`
   call. If 200, the integration goes green.
4. Daalu kicks off an initial inventory sync. Depending on
   fleet size, this takes 30 seconds to 10 minutes.

---

## What gets imported

After the initial sync, Daalu has:

- **Sites.** Each site as a record.
- **Devices.** Hostname, vendor, model, role, site, serial,
  asset tag.
- **Interfaces.** Per-device, with attached IPs.
- **IPs and prefixes.** The IPAM tree.
- **Tags.** Used in alert routing and queries.
- **Custom fields.** Imported as-is for surfacing in the UI.

These are mirrored, not authoritative — Nautobot is still the
SoT. Daalu reads on a schedule (every 15 minutes) and writes
only via proposals.

---

## Reconciliation

The reconciler agent uses this integration to detect drift:

1. Pull intended config from Nautobot for each device.
2. Connect to the device via its driver (NAPALM, scrapli,
   netmiko depending on vendor).
3. Compare live config to intended.
4. If different, write a `kind=drift` change proposal.

Configuration per device — including how Daalu talks to it
(SSH key vs. password, jump host, port, vendor-specific
driver) — lives in Nautobot's secrets/credentials model, which
Daalu reads.

---

## Writing back

Two write paths:

- **Apply intended to device.** The standard fix when drift is
  unintentional. Executor pushes the Nautobot-intended config
  to the box.
- **Update Nautobot to match device.** When the device-side
  change was deliberate. Executor patches Nautobot to record
  what's actually there.

Both go through change proposals.

---

## Webhooks (optional but recommended)

To keep Daalu's mirror fresh between scheduled syncs,
configure Nautobot to POST webhooks to Daalu on object
changes.

1. Daalu shows the webhook URL in the integration detail
   page (e.g.,
   `https://ops.example.com/api/v1/sot/webhooks/<your-slug>`)
   along with a shared secret.
2. In Nautobot, *Extensibility → Webhooks → Add* with that
   URL, the secret in the HMAC field, and the object types
   you want pushed.
3. From then on, Daalu's mirror is event-driven and the
   scheduled sync becomes a safety net.

---

## Per-tenant secrets

Daalu's executor needs device credentials to push config. Two
options:

- **Use Nautobot's credentials store.** Daalu reads Nautobot
  secrets on demand. Simpler if you're already there.
- **Configure credentials in Daalu.** *Settings → Tenant →
  Device credentials.* Useful if Nautobot doesn't have the
  secret you need.

In both cases, secrets are encrypted at rest and never logged.

---

## Limitations and roadmap

Today:

- Nautobot only; Netbox support is on the roadmap but
  currently deferred.
- Vendor drivers: Cisco IOS/NX-OS, Arista EOS, Juniper Junos,
  Nokia SROS. Others via Napalm community drivers; YMMV.
- Config diff is text-based, not structural. We're working on
  structural diffs for major vendors; for now, the diff is
  what your eyes are used to seeing.

---

## Troubleshooting

- **"401 Unauthorized"** — token wrong or revoked.
- **"403 Forbidden"** — token's user doesn't have read on the
  needed object types.
- **Slow initial sync** — large inventories take a while.
  Watch the integration detail page; "Running" with a
  progress count is expected.
- **Reconciler says drift but UI shows the device matches** —
  usually a normalization issue. Check *Settings →
  Reconciliation → Normalization rules.*

---

## What's next

Chapter 31 covers observability integrations — Prometheus,
Grafana, Loki, Thanos, OpenSearch.

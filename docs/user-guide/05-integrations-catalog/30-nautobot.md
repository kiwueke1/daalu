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

1. *Managed infra → Integrations → Guided setup → Source of
   Truth (Nautobot)*, then pick **I already run Nautobot**.
2. Enter:
   - **Nautobot base URL** — e.g., `https://nautobot.acme.io`.
     Use your Nautobot's real address. If you're on the bundled
     path, don't paste the browser console's address here — that
     console authenticates with your Daalu session, not an API
     token, and the tester will refuse it with an explanation.
     (You don't need this form at all on the bundled path.)
   - **API token** — paste the token.
   - **Webhook secret (optional)** — a string you choose,
     needed only if you want webhooks; see
     [Webhooks](#webhooks-optional-but-recommended) below.
3. Click **Test connection**. Daalu calls Nautobot's status API
   with the token; "Nautobot accepted the token" means it
   worked. **Save & continue** stays disabled until a test
   passes.
4. The last step sets up the **SSH credentials** Daalu uses to
   reach your devices: an **SSH user**, a **Port**, and either a
   private key or a **Password**.

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

1. The webhook URL is
   `https://ops.example.com/api/v1/sot/webhooks/<your-slug>`,
   where `<your-slug>` is your organisation's slug (the one
   you sign in with).
2. In Nautobot, *Extensibility → Webhooks → Add* with that
   URL, the same string you entered as **Webhook secret** in
   the secret field, and the object types you want pushed.
   Daalu only accepts webhooks once a secret is set, and
   rejects any whose signature doesn't match.
3. From then on, Daalu's mirror is event-driven and the
   scheduled sync becomes a safety net.

---

## Per-tenant secrets

Daalu's executor needs device credentials to push config.
They're one tenant-wide SSH credential, set in Daalu:

- BYO: the **SSH credentials** step of the guided setup
  (above).
- Bundled: the *Onboard your devices* panel on the environment
  (*Managed infra → Network & servers → your environment →
  Devices*).

Secrets are encrypted at rest and never logged.

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
- **Reconciler says drift but UI shows the device matches** —
  usually a formatting difference between the intended and
  live config. Check the diff on the proposal (*Operations →
  Drift*); if it's only cosmetic, reject it with a note, or
  bring the intended config in Nautobot into line with how the
  device renders it.

---

## What's next

Chapter 31 covers observability integrations — Prometheus,
Grafana, Loki, Thanos, OpenSearch.

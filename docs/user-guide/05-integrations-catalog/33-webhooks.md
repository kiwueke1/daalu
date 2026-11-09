# 33. Webhooks and custom sources

When none of Daalu's typed integrations fit, webhooks are how
you connect anything else. CI pipelines, homegrown alerters,
internal scripts, IoT — all can write to Daalu's timeline by
POSTing to a webhook URL.

---

## Inbound webhooks

You configure a webhook endpoint in Daalu; you point something
at it; it POSTs events; Daalu writes them to the timeline.

### Setup

1. *Managed Infra → Webhooks → Add inbound webhook.*
2. Pick a name. A friendly slug becomes part of the URL
   (e.g., `acme-ci`).
3. Daalu generates:
   - **URL** — `https://ops.example.com/api/v1/webhooks/acme-ci`
   - **Secret** — used to sign requests (HMAC-SHA256).
4. Use the URL and secret in whatever's going to POST.

### Request format

POST a JSON body to the URL with the HMAC of the body in the
`X-Daalu-Signature` header. The body shape is:

```json
{
  "source": "github-actions",
  "kind": "deploy.failed",
  "severity": "warning",
  "labels": { "service": "api", "env": "prod" },
  "payload": {
    "commit": "abc123",
    "actor": "ci-bot",
    "url": "https://github.com/..."
  }
}
```

`source` is freeform but should be stable for a given sender.
`kind` is freeform but should follow a `<system>.<verb>` shape
for queryability. `severity` is one of `info|warning|critical`.

### What happens

The event is recorded. If an alert rule matches it, an alert
fires. If not, it's a queryable record on the timeline.

### Signing

The signing is to prevent random callers from injecting events
into your tenant. Compute the HMAC of the body using the
secret and the SHA-256 algorithm; put it in `X-Daalu-Signature`
as a hex string.

Sample bash:

```bash
SECRET="<from the wizard>"
BODY='{"source":"my-script","kind":"check.failed","severity":"warning","labels":{},"payload":{}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -X POST https://ops.example.com/api/v1/webhooks/acme-ci \
  -H 'Content-Type: application/json' \
  -H "X-Daalu-Signature: $SIG" \
  -d "$BODY"
```

The signature is required. Unsigned POSTs are rejected with
401.

---

## Outbound webhooks

The reverse: Daalu POSTs to your URL on configured triggers.

### Setup

1. *Managed Infra → Webhooks → Add outbound webhook.*
2. Pick the trigger — what kind of events fire it (alerts of
   severity X, proposals of kind Y, etc.).
3. Provide your URL.
4. Daalu generates a secret you'll use to verify the HMAC of
   incoming POSTs.

### Request format

The same shape as inbound — Daalu POSTs JSON with a signature
header. You're the receiver; verify and act.

### Use cases

- Pipe Daalu events into your warehouse.
- Trigger your own automation from a Daalu alert.
- Mirror change proposals into your ticket system.

### Retries

If your receiver returns non-2xx, Daalu retries with exponential
backoff for up to 1 hour. After that, the delivery is marked
failed and surfaces on the webhook detail page.

---

## Webhook detail page

For each webhook (in or out):

- **URL / endpoint.**
- **Secret** — masked with a "Reveal" button.
- **Last delivery** — timestamp, status, request/response
  pair.
- **Recent history** — 100 latest deliveries with timing.
- **Rotate secret** — generates a new one. Old one continues
  working for 24 hours to allow rollout.

---

## Limits

- **Body size:** 1 MiB per POST.
- **Rate:** 10 POSTs per second per webhook (inbound); 50 per
  second outbound to your URL.
- **Retention:** 30 days of delivery history.

Need more? Email support.

---

## Patterns from the field

A few useful ones:

### "Deploy started" event from CI

A GitHub Action POSTs `kind=deploy.started` when each prod
deploy begins. The alert-explainer agent uses these events to
correlate "the latency spike at 14:32 coincided with deploy
xyz at 14:30."

### "Heartbeat" from an internal scheduler

A cron in your env POSTs `kind=heartbeat.X` every minute.
Daalu has a rule that fires alerts if these stop arriving.
Cheap dead-man's-switch.

### Mirroring to a warehouse

An outbound webhook on every event fires to your
Snowflake/BigQuery ingest URL. You now have a long-term
record of every Daalu event for your own analysis.

### Cross-tool plumbing

When a Daalu change proposal is approved, an outbound webhook
opens a Jira ticket so your existing ticketing workflow stays
in sync.

---

## What's next

Part VI dives into advanced topics. Chapter 34 walks through
deploying daalu-edge to your own cluster — the manual end of
cluster federation.

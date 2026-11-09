# 33. Webhooks and custom sources

When none of Daalu's typed integrations fit, the events API is
how you connect anything else. CI pipelines, homegrown
alerters, internal scripts — all can write to Daalu's event
feed by POSTing to one URL.

---

## Sending events to Daalu

There's a single endpoint for every custom source:

```
POST https://ops.example.com/api/v1/events
```

It authenticates with your organisation's **ingest key**, sent
in the `X-Daalu-Key` header. The ingest key is separate from
personal access tokens (Chapter 37): it can only write events,
nothing else. There's no page in the app for it; ask Daalu
support for your organisation's key, or to replace it if it
leaks.

### Request format

POST a JSON body:

```json
{
  "type": "infra.deployment.failed",
  "module": "infra",
  "source": "github-actions",
  "summary": "api deploy failed on prod",
  "severity": "warning",
  "payload": {
    "commit": "abc123",
    "actor": "ci-bot",
    "namespace": "prod",
    "deployment": "api"
  }
}
```

- `type`, `module`, `source` and `summary` are required.
- `severity` is one of `info|warning|critical` (default
  `info`).
- `payload` is any JSON object. Kubernetes-style keys such as
  `namespace` and `deployment` help Daalu tell repeat firings
  of the same problem apart from new ones.

Any `tenant_id` in the body is ignored; the ingest key decides
which organisation the event belongs to.

Sample bash:

```bash
curl -X POST https://ops.example.com/api/v1/events \
  -H 'Content-Type: application/json' \
  -H "X-Daalu-Key: $DAALU_INGEST_KEY" \
  -d '{"type":"infra.alert.fired","module":"infra","source":"my-script","summary":"disk almost full on build-01","severity":"warning","payload":{}}'
```

A missing or wrong key is rejected with 401.

### What happens

The event is recorded and shows in the event feed. Some event
types also open an **alert** the AI investigates:

| `type` | Effect |
|---|---|
| `infra.alert.fired` | Opens an alert (or counts another occurrence of an open one). |
| `infra.deployment.failed` | Same as `infra.alert.fired`. |
| `infra.alert.resolved` | Resolves the matching open alert (same `summary` and identifying payload keys). |
| anything else | Recorded on the event feed only. |

---

## Sending events out of Daalu

Daalu has no outbound webhooks. To get data out:

- **Scheduled reports** — deliver a saved query to Slack or
  email on a schedule (*Reports → Schedules*, Chapter 23).
- **The API** — poll the events, alerts or proposals endpoints
  with a personal access token (Chapter 37).

---

## Patterns from the field

### "Deploy failed" event from CI

A CI job POSTs `type=infra.deployment.failed` when a prod
deploy fails. Daalu opens an alert and the AI starts
investigating, with the commit and actor from the payload in
front of it.

### "Deploy started" context

A CI job POSTs a custom `type` (for example `ci.deploy.started`)
when each prod deploy begins. It doesn't open an alert, but it's
on the event feed next to the alerts that follow it.

---

## What's next

Part VI dives into advanced topics. Chapter 34 walks through
deploying daalu-edge to your own cluster — the manual end of
cluster federation.

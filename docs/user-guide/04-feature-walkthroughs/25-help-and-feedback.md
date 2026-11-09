# 25. Help and feedback

The *Help & Feedback* page is where you go to find documentation,
check what version of Daalu you're running, and write to the
team. It's deliberately small.

URL: `https://ops.example.com/help`.

---

## What's on the page

### System status

A small panel showing:

- **Version** — the Daalu version this tenant is on (e.g.,
  `2026.05.24`).
- **Commit** — the short identifier of the deployed backend build.
- **Built** — when the image was built.

A value Daalu doesn't have reads *Not recorded* rather than a blank.
If the version can't be read at all, the panel says *Version info
unavailable* and that Daalu itself is unaffected — it's not an
outage.

When you write to support about a bug, including the version
and commit makes the conversation 10× faster.

### Documentation

Links into this User Guide, in three groups:

- **Getting started** — how Daalu works, connecting your
  observability stack, the source of truth.
- **Using Daalu** — integrations, alerts & incidents, local GPU &
  billing.
- **Support** — troubleshooting, FAQ, glossary.

### Send feedback

A simple form: pick a type (**Bug**, **Idea**, **Praise** or
**General**), write **Your message**, and click **Send feedback**.
The button stays disabled until there's a message. A short note
confirms it reached the team, or says why it wasn't sent.

The feedback goes to an inbox the Daalu engineering team reads.
It's not anonymized — your name and email come along — so we
can follow up if needed.

### Support contacts

How to reach a human:

- **Your operator** — whoever runs your Daalu deployment.
- **GitHub issues** — `https://github.com/kiwueke1/daalu/issues`
  for bugs and feature requests in the open-source project.

The page has three parts — documentation, the feedback form, and
system status. There's no in-app chat, shortcuts page or
changelog. For GPU sizing, see Chapter 35.

---

## Other ways to reach us

- **Your operator** — for anything you need a person for,
  including account changes that aren't in the app yet
  (removing users, changing roles, deleting a tenant).
- **GitHub issues** — `https://github.com/kiwueke1/daalu/issues`
  for the open-source project.

---

## When to use which channel

| Situation | Channel |
|---|---|
| Found a bug, want it fixed | Feedback form |
| Need help right now | Ask your operator |
| Production-impacting issue | Ask your operator, saying it's affecting production |
| Want a new feature | Feedback form |
| Want to write us a thank-you note | Yes please, feedback form |
| Security issue | Private GitHub security advisory (see `SECURITY.md`) |

Feedback-form items go to your operator's inbox.

---

## What's next

Part V starts the integrations catalog. Each chapter walks
through setup, troubleshooting, and capabilities of one specific
class of integration.

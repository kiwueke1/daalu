# 25. Help and feedback

The *Help & Feedback* page is where you go to find documentation,
check what version of Daalu you're running, and write to the
team. It's deliberately small.

URL: `https://ops.example.com/help`.

---

## What's on the page

### Build info

A small panel showing:

- **Version** — the Daalu version this tenant is on (e.g.,
  `2026.05.24`).
- **Commit** — the short git SHA of the deployed backend.
- **Built at** — when CI built the image.
- **Region** — where your tenant's data is hosted.

When you write to support about a bug, including the version
and commit makes the conversation 10× faster. Click **Copy**
to put both on your clipboard.

### Documentation links

Links to:

- The *User Guide* (this book).
- Per-feature quickstarts.
- The integrations catalog.
- The API reference.
- The status page.

### Feedback form

A simple form: pick a category (bug, feature request, kind
words, other), write a message, optionally attach a screenshot.
Submit.

The feedback goes to an inbox the Daalu engineering team reads.
It's not anonymized — your name and email come along — so we
can follow up if needed. If you'd prefer anonymous, leave the
"Email me back" box unticked.

### Support contacts

How to reach a human:

- **Your operator** — whoever runs your Daalu deployment.
- **GitHub issues** — `https://github.com/kiwueke1/daalu/issues`
  for bugs and feature requests in the open-source project.

### Keyboard shortcuts

A full list of every shortcut available in the app, grouped by
page. The same content as the `?` overlay but in a printable
format.

### Changelog

A list of recent releases. Click any to see the user-facing
release notes for that version.

### Hardware compatibility

For the GPU-curious. A matrix of NVIDIA GPUs, models served,
recommended VRAM, and expected throughput. Useful when sizing
your local-inference hardware.

---

## When to use which channel

| Situation | Channel |
|---|---|
| Found a bug, want it fixed | Feedback form |
| Need help right now | In-product chat |
| Production-impacting issue | In-product chat → escalates |
| Want a new feature | Feedback form |
| Want to write us a thank-you note | Yes please, feedback form |
| Security issue | Private GitHub security advisory (see `SECURITY.md`) |

Feedback-form items go to your operator's inbox.

---

## What's next

Part V starts the integrations catalog. Each chapter walks
through setup, troubleshooting, and capabilities of one specific
class of integration.

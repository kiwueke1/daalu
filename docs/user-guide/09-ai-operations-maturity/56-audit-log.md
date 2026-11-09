# 56. The audit log

Once an AI can change your infrastructure, "who did what, and why?" needs an
answer you can trust. The **Audit log** records every read the AI makes,
every fix it proposes, every approval and rejection, every execution and its
verification, every policy change, verdict and runbook publish — in order.
Each entry carries a fingerprint of the one before it, so if anyone edits or
deletes a past entry, the chain breaks and Daalu can tell you exactly where.

URL: `https://daalu.example.com/audit` (sidebar: **Insights → Audit log**).

---

## Reading the log

The table shows the newest events first:

| Column | Meaning |
|---|---|
| **Seq** | The event's sequence number in your tenant's chain: 1, 2, 3, … with no gaps. |
| **Time** | When it was recorded. |
| **What happened** | The event's kind in words, for example "Ran automatically" or "Root cause graded" (table below). Fixes the policy ran without a person approving carry an **Auto** chip. |
| **Actor** | Who or what did it. |
| **Subject** | What it was about — the alert's title when the event has one, otherwise the subject itself (a long id is shortened to its last eight characters; hover for the whole thing). For policy events it's the version or the rule. |
| **Hash** | The start of the event's fingerprint (hover for the whole thing). |

Click a row to expand it and see the raw kind (for example
`action.auto_executed`), the full subject, the event's full data — for a
proposal, the tool and its arguments and the policy decision; for an
approval, the list of approvers; for a policy change, the whole YAML — and
the hash of the event before it. On a phone the table becomes a stack of
cards that expand the same way.

The page loads 100 events at a time. **Load older events** at the bottom
fetches the next 100.

### Filters

- **Kind** — **All kinds**, any single kind (listed in words, such as
  **Kill switch changed**), or **Any action** / **Any policy change** for a
  whole family.
- **Actor** — an exact match, for example `jane@acme.com`, `agent` or
  `policy:dev-restarts`.
- **Subject** — paste an alert id to see everything that happened on one
  alert, in order.

Click **Apply filters** to filter, **Clear filters** to reset.

---

## Who the actors are

| Actor | Meaning |
|---|---|
| `agent` | Daalu's AI, during an investigation. |
| an email address | A person on your team (approving, rejecting, changing the policy, grading, publishing). |
| `executor` | The step that runs a human-approved fix. |
| `policy:<rule>` | A fix the policy ran automatically under that rule (`policy:defaults` when no rule matched). |
| `verifier` | The check after every fix, and the circuit breaker it can trip. |
| `mcp:<email>` | A fix proposed by an external agent over MCP, acting as that person. |

---

## Every kind of event

The page shows each kind in words; the raw kind appears when you expand a
row.

| Kind | Shown as | Recorded when | Actor |
|---|---|---|---|
| `tool.read` | Read data | The AI ran a read tool during an investigation. | `agent` |
| `action.proposed` | Proposed an action | A write was attached to an alert (with its policy decision). | `agent` or `mcp:<email>` |
| `action.denied` | Blocked by policy | The policy refused a write; no approve card was created. | `agent` or `mcp:<email>` |
| `action.approval_recorded` | Approval recorded | Someone approved a fix that needs more approvals. | approver's email |
| `action.approved` | Approved | The last required approval landed; the fix is starting. | approver's email |
| `action.rejected` | Rejected | Someone rejected a fix. | their email |
| `action.executed` | Ran after approval | A human-approved fix ran. | `executor` |
| `action.auto_executed` | Ran automatically | A fix ran automatically under the policy. | `policy:<rule>` |
| `action.verified` | Fix verified | The check after a fix finished (resolved or not). | `verifier` |
| `policy.updated` | Policy changed | A new policy version was saved, or an admin reset a circuit breaker (subject `breaker-reset`). | their email |
| `policy.kill_switch` | Kill switch changed | The kill switch was turned on or off. | their email |
| `policy.breaker_tripped` | Breaker tripped | An automatic fix failed verification and its rule was demoted. | `verifier` |
| `investigation.verdict` | Root cause graded | Someone graded an investigation. | their email |
| `runbook.published` | Runbook published | A runbook was published, or a published one was edited on the Runbooks page. | their email |
| `runbook.drafted` | Runbook drafted | The AI drafted a runbook after a verified fix. | `agent` |
| `mcp.call` | Tool call (MCP) | An external agent called any MCP tool. | the caller's email |
| `eval.recorded` | Eval recorded | An eval run was recorded. | the email of the token's owner |

Events are written in the same step as the change they describe, so you
never see an approval for something that didn't happen. If writing an audit
event ever fails, the change itself still goes through (a failed audit write
must never turn a successful fix into an error) and the failure is logged on
Daalu's side.

---

## Verifying the chain

Click **Verify chain** (top right). Daalu walks your entire chain from event
1 and recomputes every fingerprint.

- **"Chain intact — *N* events verified."** Nothing has been changed or
  removed.
- **"Chain broken at seq *X* (*N* events verified before it)."** with one of
  these reasons:
  - *sequence gap* — an event is missing (deleted);
  - *prev_hash does not match the previous event* — an event was removed or
    reordered;
  - *event content does not match its hash (edited)* — an event was altered.

Everything before seq *X* is still trustworthy; *X* is where to look.

The **Maturity** page runs the same check every time it loads and shows it on
the **Audit chain** tile. An intact chain is a Level 5 criterion.

### What to do if the chain is broken

Nobody can break the chain through the Daalu UI or API — events can only be
added, never edited. A break means someone changed the database directly.

1. Note the seq number and reason.
2. Use **Load older events** (or the Subject filter) to look at the events around
   it.
3. Treat it as a security incident: ask who has direct database access and
   check your database's own logs.
4. Ask your operator if you need help.

---

## Using the log day to day

- **Post-incident review:** filter by the alert id as Subject to replay
  everything the AI read, proposed and ran, and who approved what.
- **Policy review:** filter **Kind** to **Any policy change** to see every policy change,
  kill-switch use and breaker trip, with the YAML of each version.
- **External agents:** filter **Kind** to **Tool call (MCP)** (`mcp.call`) to
  see what MCP clients have been doing.

---

## What's next

Chapter 57 shows how to benchmark the AI SRE with **evals** before you trust
a model with more autonomy.

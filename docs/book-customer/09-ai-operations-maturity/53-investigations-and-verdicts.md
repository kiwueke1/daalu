# 53. Investigations and verdicts

Every time the AI investigates an alert, Daalu keeps a record of it: when it
started, how long it took to name a root cause, what it proposed, what
happened to each proposal, how the alert ended, and — once someone on your
team has looked — whether the AI was right. That record is an
**investigation**. Grading it is the single most useful thing you can do to
move from Level 2 to Level 4: it's how "the AI is usually right" becomes a
number instead of a feeling. That number is **agreement rate**, one of the
five TRACE metrics (chapter 55).

URL: `https://daalu.example.com/investigations` (sidebar: **AI Ops →
Investigations**).

---

## When an investigation starts

There is one investigation per alert. It is created the first time the AI
works on the alert:

- when someone opens the alert and the AI runs its first triage pass,
- when someone clicks **Re-triage** or writes in the alert chat,
- or, if the autonomy policy sets `investigate_automatically: true`, as soon
  as the alert arrives — nobody has to open it.

To turn on automatic investigation, add `investigate_automatically: true` to
your policy on the **Autonomy policy** page (chapter 52) and save. All three
starter policies already set it.

---

## The list

Filters across the top:

- **Window** — 7, 30 or 90 days (by when the investigation started).
- **Any resolution** — or pick one of the resolutions below.
- **Needs a verdict** — only investigations that have a root cause but no
  verdict yet. This is your grading queue.

Columns:

| Column | Meaning |
|---|---|
| **Alert** | The alert's title, with its severity underneath. |
| **Started** | When the AI started investigating. |
| **To triage** | Time from the alert firing to the AI's root cause; "—" if it hasn't named one. |
| **Actions P / A / R / Auto** | Fixes the AI **P**roposed, that people **A**pproved, that people **R**ejected, and that ran **Auto**matically. |
| **Resolution** | How the alert ended (below). |
| **Verdict** | Your team's grade, a "needs verdict" chip if it's waiting for one, or "—" if there's nothing to grade yet. |

The list refreshes every 30 seconds. Click a row to open the drawer.

### What each resolution means

| Label | Meaning |
|---|---|
| **Open** | Nothing has resolved it yet. |
| **Auto-resolved** | A fix that ran automatically under the policy was verified to have worked. |
| **Resolved (approved fix)** | A fix a person approved was verified to have worked. |
| **Resolved manually** | The alert was closed while the investigation was still open — someone clicked **Resolve manually**, or the alert cleared on its own because the condition went away. No fix through Daalu was verified. |
| **Unresolved** | A fix ran but verification showed the workload had not recovered, and nothing has fixed it since. |

"Verified" means Daalu checked after the fix: it waited for the deployment to
settle (up to three minutes) and looked at the pods. A fix that errored, or
left the deployment short of ready replicas, counts as not verified.

---

## The drawer

Opening a row slides in the detail drawer. You can also link straight to it:
`https://daalu.example.com/investigations?open=<id>`.

- The alert's severity, the resolution chip, and **fix verified** or **fix
  did not verify** when there's an outcome. **Open alert** jumps to the alert
  page.
- **Alert fired**, **Time to triage**, **Time to resolve**, and **Model**
  (which model wrote the investigation).
- **Root cause** — the AI's root-cause write-up, or "The AI hasn't named a
  root cause yet."
- The verdict buttons (once there is a root cause).
- **Actions** — every fix proposed for this alert, with its arguments and
  status, and chips for: the policy mode (or **auto-executed**), the rule it
  matched ("rule `defaults`" when no rule did), who approved it, an external
  agent's identity if it came over MCP (`mcp:<email>`), and **verified** or
  **verification failed**.

Press **Esc** or click outside to close it.

---

## How Daalu knows the AI has triaged an alert

The AI writes its findings with a **Root cause** section (the same text you
see on the alert's **Root cause** tab). The first time it posts one, Daalu
records the time — that's the triage time — and keeps that text as the
investigation's root cause. Later messages don't overwrite it, so the
time-to-triage stays honest.

If the AI never writes a root cause, the investigation stays untriaged and
there's nothing to grade.

---

## Giving a verdict

In the drawer, or on the alert page itself:

> Was the AI's root cause right? Your answer feeds the agreement-rate metric
> on the Maturity page.

Click one of:

- **Correct** — it named the real cause.
- **Partly right** — on the right track but incomplete or imprecise.
- **Wrong** — it missed.

Add an optional note ("what was actually wrong?") in the box underneath
before you click — the note is saved with the verdict.

Once saved, the panel shows "Your team said: **Correct**", by whom and when,
and the note. Click **Change** to re-grade; the newest verdict replaces the
old one (both are kept in the audit log as `investigation.verdict` events).
Anyone on the team can give or change a verdict.

### How verdicts are counted

- **Agreement rate** = (correct + half of partly right) ÷ all verdicts. Two
  correct and one partly right is 83%.
- **Verdict coverage** = investigations with a verdict ÷ all investigations.

Level 4 needs verdicts on at least 30% of investigations; Level 5 needs
agreement of at least 80%. You don't need to grade everything — a steady
sample is enough — but grade the ones that went badly as well as the ones
that went well, or the agreement number flatters the AI.

The verdict itself is a record for your team and for the metrics. If you
want a mistake the AI made to become a regression test, write it up as an
eval scenario (chapter 57) — that step isn't automatic.

---

## On the alert page

Every alert with an investigation shows an **Investigation record** panel
under the **Root cause** tab: the resolution, **Fix verified** / **Fix did
not verify**, time to triage and resolve, the action counts, how many
runbooks were used, and the verdict buttons. **All investigations** opens the
same record in the drawer here. Before the AI has named a root cause the
panel says "You can grade the root cause once the AI has named one."

---

## A weekly routine

1. Tick **Needs a verdict**, 7-day window.
2. Open each row, read the root cause against what you know happened, click
   a verdict, add a note where it was wrong.
3. Filter **Unresolved** and look at each failed fix — that's where policy
   rules should be narrowed (chapter 52).
4. Check the **Maturity** page's agreement rate and change-failure tiles.

---

## What's next

Chapter 54 covers **Runbooks** — writing your team's fixes down where the AI
reads them.

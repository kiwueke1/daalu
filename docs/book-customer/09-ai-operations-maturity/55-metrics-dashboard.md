# 55. The metrics dashboard

DevOps teams measure delivery with four numbers: deployment frequency, lead
time, change-failure rate and time to restore. AI operations need their own
set. The AI-Ops Maturity Model leads with **five TRACE metrics**: two for speed
(time to triage, time to resolve), two for quality (agreement rate,
change-failure rate) and one for autonomy (escalation rate); the name is their
initials (**T**ime to triage, time to **R**esolve, **A**greement, **C**hange failure,
**E**scalation). **Supporting
metrics** explain them: how much of the alert load the AI sees, how much of
its work you grade, how often you accept its fixes, and whether the learning
practices and the audit record are in place. Daalu computes all of them from
what actually happened. This chapter explains each one, then shows how they
map to the levels.

Where they appear: the stat tiles and daily trend on the **Maturity** page
(`https://daalu.example.com/maturity`, chapter 51), and the **Criteria** table on
the same page, which compares them with each level's thresholds.

---

## The window

Every number covers a window you pick at the top of the page: **7 days**,
**30 days** or **90 days**.

- **Alerts and investigations**: alerts *created* in the window, and the
  investigations of those alerts.
- **Fixes (actions)**: write actions *proposed* in the window.
- **Eval runs**: runs *started* in the window.
- **Runbooks**: current totals, not windowed.

A rate shows as **—** when there's nothing to divide by — for example the
approval rate before anyone has approved or rejected anything. "—" means
"unknown", not zero, and an unknown rate never meets a criterion.

---

## The five TRACE metrics

Read them together. A fast diagnosis that is usually wrong doesn't help, and
automatic fixes with a high failure rate are unattended damage. A healthy
team sees the two times fall while agreement rate stays high and the
change-failure rate stays low, with escalation rate falling on top.

### Time to triage

**What:** the median time from an alert firing to the AI posting its root
cause. Undiagnosed alerts aren't included.
**Tile:** **Time to triage** (median).
**Read it:** seconds to a few minutes is normal with automatic investigation.
Long times usually mean the alert sat unopened before anyone triggered the
AI.
**Gates:** no level. It is tracked, not thresholded (see below).

### Time to resolve (MTTR)

**What:** the median time from an alert firing to its investigation being
resolved — by a verified fix, or by the alert being closed (manually or
because it cleared).
**Tile:** **MTTR** (median time to resolve).
**Read it:** this is the end-to-end number your on-call feels. It should fall
as more fixes are proposed, approved and, later, run automatically.
**Gates:** no level. It is tracked, not thresholded (see below).

### Agreement rate

**What:** how often your team agreed with the AI's root cause: (correct + half
of partly right) ÷ all verdicts.
**Tile:** **Agreement rate**, with verdict coverage underneath.
**Healthy:** Level 5 needs 80% or more. It only means something once you have
a reasonable number of verdicts — see verdict coverage below.

### Change-failure rate

**What:** of the fixes that ran (automatic or approved), the share that
failed verification — the workload didn't come back.
**Tile:** **Change-failure rate**, with "*n* of *m* executed failed
verification". It turns red above 15%.
**Healthy:** Level 3 needs 15% or less. This is the number to watch when you
widen automatic rules.

### Escalation rate

**What:** of the investigations that ended resolved (automatically, by an
approved fix, or manually), the share that needed a human: approved or
resolved manually. It's the inverse of the share resolved automatically.
**Tile:** **Escalation rate**, with the auto-resolved count.
**Healthy:** Level 5 needs 50% or less.

---

## Supporting metrics

These explain the TRACE metrics and show whether the practices behind them are
in place.

### AI triage rate

**What:** the share of alerts in the window that the AI investigated.
**Tile:** **AI triage rate**, with "*n* of *m* alerts" underneath.
**Healthy:** 50% or more is the Level 1 bar. With
`investigate_automatically: true` in your policy it should be close to 100%.
If it's low, people are only opening some alerts, or automatic investigation
is off.

### Verdict coverage

**What:** investigations with a verdict ÷ all investigations.
**Shown:** under the agreement rate tile and in the criteria table.
**Healthy:** Level 4 needs 30% or more.

### Approval rate

**What:** of the fixes that waited for a human and got a decision, the share
that were approved. Automatic fixes aren't counted; fixes still waiting
aren't counted either.
**Tile:** **Approval rate**, with approved / rejected counts.
**Read it:** a high rate on a particular kind of fix is the signal that it's a
candidate for an `auto_after_trust` rule. A low rate means the AI's proposals
need work — read the rejected ones.

### Audit chain

**What:** whether your audit log's hash chain verifies end to end.
**Tile:** **Audit chain** — **Intact** or **Broken**, with a link to the audit
log. Level 5 needs it intact. See chapter 56.

### Runbooks published / generated

**What:** how many runbooks are published, and how many the AI has drafted
in total. Current totals.
**Shown:** the **Published runbooks** row of the criteria table. Level 4
needs at least one published.

### Eval runs and mean score

**What:** eval runs started in the window and their average score.
**Shown:** the **Eval runs in the window** row of the criteria table; the
details are on the **Evals** page (chapter 57). Level 4 needs at least one.

---

## How the metrics map to the levels

Each level is a set of thresholds on these metrics, plus a few facts about
what ran and what your saved policy allows (chapter 51 has the full list).
From the metric side:

| Metric | Kind | Gates | Threshold |
|---|---|---|---|
| Time to triage | Key | — | Tracked at every level |
| Time to resolve (MTTR) | Key | — | Tracked at every level |
| Change-failure rate | Key | Level 3 | 15% or less |
| Escalation rate | Key | Level 5 | 50% or less |
| Agreement rate | Key | Level 5 | 80% or more |
| Alerts in the window | Supporting | Level 1 | 5 or more |
| AI triage rate | Supporting | Level 1 | 50% or more |
| Approval rate | Supporting | Level 2 | Known (at least one decision) |
| Verdict coverage | Supporting | Level 4 | 30% or more |
| Published runbooks | Supporting | Level 4 | 1 or more |
| Eval runs | Supporting | Level 4 | 1 or more |
| Audit chain | Supporting | Level 5 | Intact |

The two speed metrics gate no level, on purpose. How fast a team diagnoses
and resolves depends on its systems and its alert mix, so no single threshold
is fair to everyone. They are what climbing the levels is meant to improve:
compare them with your own history, for example the 30-day window against
the 90-day one.

---

## The action counts

Behind the approval and failure rates are these counts (in the hints under
the tiles):

| Count | Meaning |
|---|---|
| proposed | Every write the AI (or an external agent) attached to an alert. Denied writes never become proposals. |
| approved | Human-gated fixes that reached their approvals, including ones that then ran. |
| rejected | Human-gated fixes someone rejected. |
| auto | Fixes the policy let run automatically. |
| executed | Fixes that ran (successfully or with an error). |
| failed verification | Executed fixes whose verification showed no recovery. |

---

## The daily trend

The chart under the tiles plots, per day across the window:

- **Alerts** — alerts created that day.
- **Investigated** — investigations started that day.
- **Auto-resolved** — investigations resolved automatically that day.
- **Resolved by approved fix** — investigations resolved by an approved fix
  that day (dashed line).

What a healthy climb looks like: the **Investigated** line hugging the
**Alerts** line (Level 1), then **Resolved by approved fix** appearing
(Level 2), then **Auto-resolved** taking over from it for routine alerts
(Level 3 onwards).

---

## Questions people ask

**Why did our level drop?** Levels are recomputed for the window. If you
switch to 7 days and there were no eval runs this week, the Level 4 eval
criterion isn't met. Look at the criteria table for the first "not yet".

**Why is the approval rate "—"?** No human-gated fix has been approved or
rejected in the window.

**An alert cleared by itself — does that count against the AI?** It counts as
"Resolved manually". It doesn't affect the change-failure rate, but it does
push the escalation rate up because it's a resolution the AI didn't
make.

---

## What's next

Chapter 56 covers the **Audit log**: the tamper-evident record behind every
number on this page.

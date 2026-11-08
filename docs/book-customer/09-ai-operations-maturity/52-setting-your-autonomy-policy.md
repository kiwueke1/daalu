# 52. Setting your autonomy policy

The autonomy policy decides, for every change the AI wants to make, whether
the change is **denied**, **waits for approval** (and from how many people),
or **runs on its own**. It is a short YAML document, versioned like code. It
is the single control that moves your team from Level 2 (every change
approved) to Level 3 and beyond, and the one that moves the
**escalation rate** and, if a rule is too broad, the
**change-failure rate** (chapter 55).

URL: `https://daalu.example.com/autonomy` (sidebar: **AI Ops → Autonomy policy**).

---

## What the policy covers — and what it doesn't

The policy only governs **writes**: the tools that change something.

- `rollout_undo`
- `scale_deployment`
- `restart_deployment`
- `delete_pod`
- `patch_resource`
- `call_external_api` with any method other than GET

**Read tools always run.** Pod logs, events, describe, deployment status,
rollout history, recent changes, Prometheus and Loki queries, cloud reads —
the AI uses them freely while it investigates, whatever the policy says.
Reading can't break anything, and an AI that can't look can't diagnose.

**No saved policy = Level 2.** Until someone saves a policy, Daalu uses a
built-in default: every write needs one human approval, and alerts are not
investigated automatically. That's exactly how Daalu behaved before the
maturity model existed, so turning the feature on changes nothing until you
choose to.

---

## The four modes

| Mode | What happens to the write |
|---|---|
| `deny` | Refused. The AI is told the policy denied it and is asked to propose something else or tell you which manual steps to run. No approve card appears. |
| `approve` | An approve card appears on the alert. It runs once the required number of people have approved it. |
| `auto` | Runs straight away, then is verified exactly like an approved fix. |
| `auto_after_trust` | Waits for approval until the rule has earned trust (enough verified successes, a low enough failure rate), then behaves like `auto`. |

`auto` is always subject to the guardrails below; a guardrail can turn an
`auto` into an `approve`, never the other way round. Nothing in the
guardrails ever relaxes a `deny`.

---

## How a decision is made

For each write, in this order:

1. **Find the rule.** Rules are checked top to bottom; **the first matching
   rule wins**. If none matches, the `defaults` apply.
2. **`deny` or `approve`?** If that's the rule's mode, that's the answer.
3. **`auto_after_trust`?** Check the rule's track record. Not enough verified
   successes yet, or a failure rate above the limit → `approve`.
4. **Kill switch on?** → `approve`.
5. **Proposed by an external agent over MCP** and the rule doesn't set
   `allow_external_auto: true`? → `approve`.
6. **Circuit breaker tripped** for this rule? → `approve` until it cools down.
7. **Inside a change-freeze window?** → `approve`.
8. **Hourly cap reached?** → `approve`.
9. Otherwise → `auto`.

Every decision comes with a plain-English reason, which you see on the
approve card ("Why: …") and in the simulator.

---

## Every field, explained

```yaml
version: 1
investigate_automatically: true
defaults:
  mode: approve
  approvers: 1
guardrails:
  max_auto_actions_per_hour: 10
  circuit_breaker:
    cooldown_minutes: 60
  freeze_windows:
    - days: [sat, sun]
      start: "00:00"
      end: "23:59"
      tz: UTC
rules:
  - id: scale-earned
    description: Scaling runs automatically once it has a clean track record.
    match: {tools: [scale_deployment], clusters: ["*"], namespaces: ["*"], severities: [warning, critical]}
    mode: auto_after_trust
    approvers: 1
    trust: {min_successes: 10, max_failure_rate: 0.05, window_days: 30}
    allow_external_auto: false
```

| Field | Meaning |
|---|---|
| `version` | Always `1`. |
| `investigate_automatically` | `true`: the AI starts investigating every new alert the moment it arrives, without anyone opening it. Default `false`. |
| `defaults.mode` | The mode for writes no rule matches. Default `approve`. |
| `defaults.approvers` | How many distinct people must approve those writes (1–5). Default 1. |
| `guardrails.max_auto_actions_per_hour` | Once this many automatic actions have started in the last hour, further `auto` writes wait for approval. Leave it out for no cap. |
| `guardrails.circuit_breaker.cooldown_minutes` | When an automatic action fails verification, its rule is demoted to `approve` for this many minutes (default 60). Leave `circuit_breaker` out and nothing trips. |
| `guardrails.freeze_windows` | Times when every `auto` becomes `approve`. Each window has `days` (`mon` … `sun`; default all days), `start` and `end` as `HH:MM` (defaults `00:00` and `23:59`), and `tz` (an IANA time zone such as `Europe/London`; default `UTC`). A window whose `end` is earlier than its `start` crosses midnight: `start: "22:00", end: "06:00"` covers the night. |
| `rules[].id` | A unique name for the rule. Shown on approve cards, in the trust table and the audit log. |
| `rules[].description` | Optional note for humans. |
| `rules[].match.tools` | Which write tools the rule covers. |
| `rules[].match.clusters` | Which clusters (by the cluster name on the alert). |
| `rules[].match.namespaces` | Which namespaces (the namespace the write targets). |
| `rules[].match.severities` | Which alert severities: `info`, `warning`, `critical`. |
| `rules[].mode` | `deny`, `approve`, `auto` or `auto_after_trust`. |
| `rules[].approvers` | Distinct approvals required when the write ends up in `approve` (1–5, default 1). |
| `rules[].trust.min_successes` | For `auto_after_trust`: verified successes needed before it runs on its own. Default 10. |
| `rules[].trust.max_failure_rate` | …and the highest acceptable failure rate, 0–1. Default 0.05. |
| `rules[].trust.window_days` | How far back the track record is counted. Default 30. |
| `rules[].allow_external_auto` | Let writes proposed by an external agent over MCP run automatically under this rule. Default `false` (see chapter 58). |

Match values are glob patterns: `dev-*` matches `dev-eu` and `dev-us`, `*`
matches anything. **A match key you leave out matches everything**, so
`match: {namespaces: [kube-system]}` covers every write tool on every cluster
in `kube-system`. Unknown keys are rejected when you save, so a typo can't
silently widen a rule.

**What counts as a success.** A rule's track record counts the fixes made
under it in the trust window that actually ran and were then verified: the
workload came back (success) or didn't (failure). Fixes approved by people
while the rule was still earning trust count too — that is how an
`auto_after_trust` rule earns its way to automatic.

---

## Walking through the page

### Kill switch

The card at the top. When it's off it reads "Kill switch is off" with a red
**Pause all automation** button. When it's on the card turns red and reads
"Kill switch is ON — all automatic actions are paused", with who turned it on
and when, and a **Turn off** button that asks you to confirm with **Resume
automation**. See [The kill switch](#the-kill-switch) below.

### Breaker banner

If an automatic action that used the policy *defaults* (rather than a named
rule) failed verification, a red **breaker tripped** banner appears here:
"Auto actions under the defaults are demoted to approve until …". Admins see
a **Reset breaker** button. Breakers on named rules show in the trust table
instead.

### The editor

The header says **Built-in default (not saved yet)** until someone saves a
policy, then **Version** *N* with when it was saved and the note that went
with it.

- **Starter policies…** — loads one of the three starters below into the
  editor (it doesn't save anything) and pre-fills the note.
- The YAML editor itself.
- A note field: "What changed and why (saved with the version)".
- **Discard** — throw away your edits and reload the saved version.
- **Save new version** — validates the YAML and saves it as the next version.
  Only tenant admins can save; everyone else can still edit a draft and test
  it. If the YAML is invalid you get "The policy doesn't validate:" followed
  by every problem, with its location (for example
  `rules.0.mode: Input should be 'deny', 'approve', 'auto' or 'auto_after_trust'`).

Every save is a new version. Nothing is ever overwritten.

### Test a write

The simulator on the right answers "what would the policy decide for this
write, right now?" Fill in:

- **Tool** — one of the write tools.
- **Cluster**, **Namespace** — leave blank to mean "not set".
- **Alert severity** — `(any)`, info, warning or critical.
- **From an external agent (MCP)** — tick to see what an MCP client would get.
- **Test against the draft in the editor** — ticked by default, so you can
  try a change before saving it. Untick to test the saved policy.

Click **Run test**. The answer shows:

- **Decision** — a `deny` / `approve` / `auto` chip. If a guardrail changed
  the outcome you also see "(rule says `auto`)", for example.
- How many approvers, when it's `approve`.
- **Rule** — the matching rule's id, or "defaults".
- The reason in plain English.
- The live context: "Kill switch on/off · *n* auto actions in the last hour".

The simulator uses the real state — kill switch, breakers, the hourly count
and each rule's track record — so it shows today's answer, not a theoretical
one.

### Trust by rule

One row per rule in the saved policy:

| Column | Meaning |
|---|---|
| **Rule** | The rule id. |
| **Mode** | The configured mode. |
| **Successes** / **Failures** | Verified outcomes in the rule's trust window. |
| **Failure rate** | Failures as a share of outcomes; "—" when there are none. For `auto_after_trust` rules the limit is shown too. |
| **Earned** | For `auto` and `auto_after_trust` rules: "earned" or "not yet". "n/a" for `deny` and `approve` rules. |
| **Breaker** | "closed", or a red chip with the time it's tripped until. Admins get a **Reset** button. |

### Version history

Every saved version, newest first, with who saved it and when; the active
one is marked **active**. **View** / **Hide** shows the YAML. **Restore into
editor** loads an old version into the editor (with the note "Restore v*N*")
— saving it creates a new version, so history is never rewritten.

---

## The three starter policies

Pick one from **Starter policies…**, adjust the cluster and namespace names to
yours, test it, and save.

### Level 2 — every change approved

*The AI proposes, a human approves every write. Nothing runs on its own.*

```yaml
# Level 2 (Proposing): every write the AI proposes needs one human approval.
version: 1
investigate_automatically: true
defaults:
  mode: approve
  approvers: 1
guardrails:
  max_auto_actions_per_hour: 10
  circuit_breaker:
    cooldown_minutes: 60
  freeze_windows: []
rules:
  - id: no-writes-in-kube-system
    description: Never touch the control plane's own namespace.
    match: {namespaces: [kube-system]}
    mode: deny
```

### Level 3 — dev runs itself

*Restarts and pod deletes run automatically on dev clusters; scaling earns
auto after a track record; prod rollbacks need two approvers.*

```yaml
# Level 3 (Trusted): low-risk, proven actions run automatically.
version: 1
investigate_automatically: true
defaults:
  mode: approve
  approvers: 1
guardrails:
  max_auto_actions_per_hour: 10
  circuit_breaker:
    cooldown_minutes: 60
  freeze_windows:
    - days: [sat, sun]
      start: "00:00"
      end: "23:59"
      tz: UTC
rules:
  - id: no-writes-in-kube-system
    match: {namespaces: [kube-system]}
    mode: deny
  - id: dev-restarts
    description: Restarting or recycling pods on dev clusters is safe to automate.
    match: {tools: [restart_deployment, delete_pod], clusters: ["dev-*"]}
    mode: auto
  - id: scale-earned
    description: Scaling runs automatically once it has a clean track record.
    match: {tools: [scale_deployment]}
    mode: auto_after_trust
    trust: {min_successes: 10, max_failure_rate: 0.05, window_days: 30}
  - id: prod-rollback
    match: {tools: [rollout_undo], namespaces: ["prod-*", "default"]}
    mode: approve
    approvers: 2
```

### Level 5 — trusted, with guardrails

*Routine fixes run everywhere once earned; patches and external calls stay
human-gated; kill switch, breaker, freeze window and rate cap all on.*

```yaml
# Level 5 (Autonomous): the AI resolves routine incidents; humans own the
# policy and the exceptions. Guardrails narrow auto back to approve.
version: 1
investigate_automatically: true
defaults:
  mode: approve
  approvers: 1
guardrails:
  max_auto_actions_per_hour: 20
  circuit_breaker:
    cooldown_minutes: 120
  freeze_windows:
    - days: [fri]
      start: "16:00"
      end: "23:59"
      tz: UTC
rules:
  - id: no-writes-in-kube-system
    match: {namespaces: [kube-system]}
    mode: deny
  - id: external-calls-two-approvers
    match: {tools: [call_external_api]}
    mode: approve
    approvers: 2
  - id: patches-need-review
    match: {tools: [patch_resource]}
    mode: approve
  - id: dev-anything-routine
    match: {tools: [restart_deployment, delete_pod, scale_deployment, rollout_undo], clusters: ["dev-*", "staging-*"]}
    mode: auto
    allow_external_auto: true
  - id: prod-routine-earned
    match: {tools: [restart_deployment, delete_pod, scale_deployment]}
    mode: auto_after_trust
    trust: {min_successes: 20, max_failure_rate: 0.02, window_days: 30}
  - id: prod-rollback-earned
    match: {tools: [rollout_undo], severities: [warning, critical]}
    mode: auto_after_trust
    trust: {min_successes: 10, max_failure_rate: 0.05, window_days: 60}
```

Rule order matters: in all three, the `kube-system` deny comes first so no
later rule can let a write through there.

---

## Multiple approvers

When a rule sets `approvers: 2` (or more), the approve card shows "0 / 2
approvals" and the first person's button reads **Add my approval**. After you
click it you see "Your approval is recorded, waiting for 1 more." The fix
runs only when the count is reached, and only with **distinct** people: if
you try to approve the same action twice, Daalu refuses ("you have already
approved this action").

Approvals are recorded per person in the audit log
(`action.approval_recorded`, then `action.approved` when the last one lands).

---

## The kill switch

**What it does.** With the kill switch on, every write that would have run
automatically waits for a human approval instead. Writes the policy denies
stay denied; writes that need approval still need approval. Read tools keep
running, so the AI keeps investigating. Fixes that are already running are
not interrupted.

**Who can use it.** Anyone on your team can turn it **on** — stopping
automation should never wait for an admin. Only a tenant admin can turn it
back **off**.

**When to use it.**

- An automatic fix did something you didn't expect and you want to stop and
  look before anything else runs.
- A wider incident or migration where you want every change in human hands.
- Testing it, so everyone on call knows where it is (a Level 3 practice).

Each change is written to the audit log as `policy.kill_switch`, with who
did it.

---

## When the circuit breaker trips

**What tripped means.** Every fix — automatic or approved — is verified after
it runs: Daalu waits for the deployment to settle and checks the pods. If an
**automatic** fix fails that check, the rule it ran under is demoted to
`approve` for `cooldown_minutes`. The AI is also asked to look again and
propose its next step, which will now wait for approval under that rule.

**Where you see it.**

- **Trust by rule → Breaker** shows a red chip with the time it's tripped
  until (or, for the policy defaults, the banner at the top of the page).
- The **Test a write** simulator reports "circuit breaker for rule … is
  tripped until …".
- The audit log has a `policy.breaker_tripped` event.

**What to do.**

1. Open **Investigations**, set **Resolution** to **Unresolved**, and open the
   alert whose fix failed. The drawer shows the action with a "verification
   failed" chip.
2. Decide whether the fix was wrong for this situation, or the rule is too
   broad. Typical changes: narrow the rule's `match` (fewer namespaces or
   clusters, or add `severities`), switch it from `auto` to
   `auto_after_trust` so it has to re-earn trust, or set it to `approve`.
3. Save the new policy version with a note saying why.
4. Either wait out the cooldown, or have an admin click **Reset** in the trust
   table (or **Reset breaker** on the defaults banner) once you're happy.

If your policy has no `circuit_breaker`, nothing trips: a failed automatic fix
is still recorded and still counts against the rule's failure rate, but the
rule keeps running automatically. That's why the Level 5 criteria require a
breaker.

---

## What's next

Chapter 53 covers the **Investigations** page: how to review what the AI
found, what each resolution means, and how to grade it.

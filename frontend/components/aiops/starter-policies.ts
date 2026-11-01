/**
 * Example autonomy policies for the /autonomy "Starter policies" picker.
 * Each is valid against the policy schema (core/aiops/policy.py PolicyDoc,
 * spec/autonomy-policy.schema.json): extra keys are rejected there, so keep
 * these to the documented fields.
 */

export const WRITE_TOOLS = [
  "rollout_undo",
  "scale_deployment",
  "restart_deployment",
  "delete_pod",
  "patch_resource",
  "call_external_api",
] as const;

export interface StarterPolicy {
  id: string;
  label: string;
  level: number;
  description: string;
  yaml: string;
}

export const STARTER_POLICIES: StarterPolicy[] = [
  {
    id: "level-2-default",
    label: "Level 2 — every change approved",
    level: 2,
    description: "The AI proposes, a human approves every write. Nothing runs on its own.",
    yaml: `# Level 2 (Proposing): every write the AI proposes needs one human approval.
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
`,
  },
  {
    id: "level-3-dev-auto",
    label: "Level 3 — dev runs itself",
    level: 3,
    description:
      "Restarts and pod deletes run automatically on dev clusters; scaling earns auto after a track record; prod rollbacks need two approvers.",
    yaml: `# Level 3 (Trusted): low-risk, proven actions run automatically.
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
`,
  },
  {
    id: "level-5-guardrails",
    label: "Level 5 — trusted, with guardrails",
    level: 5,
    description:
      "Routine fixes run everywhere once earned; patches and external calls stay human-gated; kill switch, breaker, freeze window and rate cap all on.",
    yaml: `# Level 5 (Autonomous): the AI resolves routine incidents; humans own the
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
`,
  },
];

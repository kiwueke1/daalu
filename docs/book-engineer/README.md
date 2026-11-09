# Daalu Automation — Engineering & Operations Manual

The internal companion to the customer-facing *User Guide*. This
manual is for the people who build, deploy, and operate Daalu Automation
itself.

You should read this if you are:

* Adding a new feature to the backend or frontend.
* On call for an incident on the production cluster.
* Debugging a customer report by reading code, logs, and metrics.
* Onboarding to the engineering team and trying to form a mental model.
* Doing a security or architecture review.

Code references in this manual use the `path:line` convention so you can
open them directly in your editor. They were accurate at the time of
writing — if a reference is stale, treat the live code as the source of
truth and send a patch to the manual.

Chapters live in part-numbered folders. Chapter numbers stay sequential
inside each section so cross-references in earlier chapters still
resolve.

---

## Part I — Orientation

1. [The platform in one page](01-orientation/01-platform-in-one-page.md)
2. [Code layout](01-orientation/02-code-layout.md)
3. [Tech stack and the rationale behind each choice](01-orientation/03-tech-stack.md)

## Part II — Backend Architecture

4. [The FastAPI application](02-backend-architecture/04-fastapi-app.md)
5. [Authentication — JWT, PATs, the auth gate, login modes](02-backend-architecture/05-auth-system.md)
6. [Database schema and migrations](02-backend-architecture/06-database-schema.md)
7. [The Celery worker pool](02-backend-architecture/07-celery-workers.md)
8. [The module system](02-backend-architecture/08-module-system.md)
9. [Core libraries — kube_tools, LLM router, billing](02-backend-architecture/09-core-libraries.md)

## Part III — Frontend Architecture

10. [Next.js app router layout](03-frontend-architecture/10-frontend-layout.md)
11. [Page-by-page tour](03-frontend-architecture/11-frontend-pages.md)
12. [The API client](03-frontend-architecture/12-frontend-api-client.md)
13. [Shared components and the design system](03-frontend-architecture/13-frontend-components.md)

## Part IV — Source of Truth & Config Management

14. [Reconciler architecture](04-source-of-truth/14-sot-reconciler.md)
15. [The Nautobot adapter](04-source-of-truth/15-sot-nautobot-adapter.md)
16. [Change proposals — lifecycle and approval](04-source-of-truth/16-change-proposals.md)
17. [Executor identity and the JWT scope gate](04-source-of-truth/17-executor-identity.md)
19. [Per-tenant Nautobot (nautobot-controller)](04-source-of-truth/19-per-tenant-nautobot.md)
20. [Config-management planes: NV-CM + Tinkerbell](04-source-of-truth/20-config-management-planes.md)
21. [The NV-CM network plane](04-source-of-truth/21-nvcm-network-plane.md)
22. [The Tinkerbell server plane](04-source-of-truth/22-tinkerbell-server-plane.md)
23. [NV-CM internals & the controller (operator) integration](04-source-of-truth/23-nvcm-internals-and-operator.md)
79. [Live fabric topology & the in-browser device terminal](04-source-of-truth/79-network-topology-and-device-terminal.md)
96. [Config rendering, the template tree, and the render_enabled gate](04-source-of-truth/96-config-rendering-and-render-gate.md)
97. [Config backups, the backup scheduler, and version diffs](04-source-of-truth/97-config-backups-and-diffs.md)
98. [Cable validation and zero-touch provisioning (ZTP)](04-source-of-truth/98-cable-validation-and-ztp.md)
99. [NV-CM's MCP server and its workflow allow-list (not `daalu mcp` — see 90)](04-source-of-truth/99-nvcm-mcp-server-and-workflow-allow-list.md)

## Part V — Cluster Federation

24. [The WireGuard hub](05-cluster-federation/24-wireguard-hub.md)
25. [Deploying daalu-edge to a workload cluster](05-cluster-federation/25-daalu-edge.md)
26. [The join flow — from invite to handshake](05-cluster-federation/26-join-flow.md)
27. [Cross-cluster API access and scraping](05-cluster-federation/27-cross-cluster.md)
28. [Daalu Private — edge-side agents](05-cluster-federation/28-daalu-private.md)
29. [The edge data plane — customer-managed Postgres](05-cluster-federation/29-edge-data-plane.md)
78. [The managed-infra console pattern](05-cluster-federation/78-managed-infra-console.md)

## Part VI — GPU and LLM Serving

30. [vLLM on the workload cluster](06-gpu-and-llm-serving/30-vllm-deployment.md)
31. [The LLM router](06-gpu-and-llm-serving/31-llm-router.md)
32. [NVIDIA-specific notes](06-gpu-and-llm-serving/32-nvidia-notes.md)
33. [The classifier and routing decisions](06-gpu-and-llm-serving/33-classifier.md)
34. [The inference-gateway service](06-gpu-and-llm-serving/34-inference-gateway.md)
80. [Zero-touch GPU onboarding & the AI-factory data plane](06-gpu-and-llm-serving/80-zero-touch-gpu-onboarding.md)
93. [AIPerf load testing — the benchmark runner behind AI Factory](06-gpu-and-llm-serving/93-aiperf-load-testing.md)
94. [GPU sharing — time-slicing, MPS, and the per-tenant memory fraction](06-gpu-and-llm-serving/94-gpu-sharing-time-slicing-and-mps.md)
95. [GPU health — the `dcgmi diag` runner, the Reliability panel, and NVSentinel in observe mode](06-gpu-and-llm-serving/95-gpu-health-dcgmi-diag-and-nvsentinel.md)

The coding-assistant chapters (35, 36, 73) moved to their own section —
see **Part XIV — The Coding Agent & Workspace IDE** below.

## Part VII — Observability

37. [kube-prometheus-stack — values and customizations](07-observability/37-kube-prom-stack.md)
38. [Loki and Promtail](07-observability/38-loki-promtail.md)
39. [Service and Pod monitors](07-observability/39-service-monitors.md)
40. [Dashboards and alerts (incl. the native Grafana-backed dashboards plane)](07-observability/40-dashboards-and-alerts.md)
41. [Onboarding observability stacks into Daalu (UI + API)](07-observability/41-onboarding-stacks-into-daalu.md)
92. [The one-click observability installer (OpenSearch, Fluent Bit, Grafana)](07-observability/92-one-click-observability-installer.md)

## Part VIII — Deployment and CI/CD

42. [Local development setup](08-deployment-and-cicd/42-local-dev.md)
43. [Building images (incl. the requirements.lock policy)](08-deployment-and-cicd/43-building-images.md)
44. [The Helm chart](08-deployment-and-cicd/44-helm-chart.md)
45. [install.sh and rollout mechanics](08-deployment-and-cicd/45-install-sh.md)
47. [Secrets and SOPS](08-deployment-and-cicd/47-secrets-and-sops.md)
91. [The Docker Compose install (open-source distribution)](08-deployment-and-cicd/91-docker-compose-install.md)

## Part IX — Operations Runbooks

49. [Common incidents and their fixes](09-operations-runbooks/49-common-incidents.md)
50. [Database migrations in production](09-operations-runbooks/50-prod-migrations.md)
51. [Tenant operations](09-operations-runbooks/51-tenant-ops.md)
52. [Image pulls and GHCR](09-operations-runbooks/52-image-pulls.md)
53. [Cert-manager, TLS, and DNS](09-operations-runbooks/53-tls-and-dns.md)
54. [Debugging NetworkPolicies](09-operations-runbooks/54-networkpolicy-debug.md)

## Part X — Security

55. [Threat model](10-security/55-threat-model.md)
56. [Auth flows in depth](10-security/56-auth-flows.md)
57. [Tenant isolation guarantees](10-security/57-tenant-isolation.md)
58. [Secrets management](10-security/58-secrets-mgmt.md)
59. [Keycloak hub SSO](10-security/59-keycloak-hub-sso.md) — *security* chapter 59; not to be confused with the testing chapter of the same number below

## Part XI — Testing

59. [Test layout](11-testing/59-test-layout.md) — *testing* chapter 59 (the number collides with the Keycloak-SSO chapter above; the folder disambiguates)
60. [Integration tests](11-testing/60-integration-tests.md)
61. [End-to-end verification (incl. the hosted demo lab)](11-testing/61-e2e-verification.md)

## Part XII — Contributing

62. [Adding a new domain module](12-contributing/62-new-domain.md)
63. [Adding a new integration adapter](12-contributing/63-new-integration.md)
64. [Adding a new agent](12-contributing/64-new-agent.md)
65. [Database migration patterns](12-contributing/65-migration-patterns.md)
66. [Cutting a release](12-contributing/66-cutting-a-release.md)

## Part XIII — Auth Integrations

Start with the [section overview](13-auth-integrations/README.md).

67. [Auth integrations — overview & map](13-auth-integrations/67-auth-integrations-overview.md)
68. [NV-CM tool SSO through the hub (cmtools)](13-auth-integrations/68-nvcm-tool-sso-through-the-hub.md)
69. [Per-user token-exchange](13-auth-integrations/69-per-user-token-exchange.md)
70. [Keycloak realm, clients & provisioning](13-auth-integrations/70-keycloak-realm-clients-and-provisioning.md)
71. [Reachability: DNS, TLS, firewall & the tunnel](13-auth-integrations/71-auth-reachability-dns-tls-tunnel.md)
73. [Native password login & operator accounts](13-auth-integrations/73-native-password-login-and-operator-accounts.md)

## Part XIV — The Coding Agent & Workspace IDE

The browser-based VS Code workspace, the **Daalu Coding Agent** that ships
inside it, and the laptop `daalu` CLI. Start with the
[section overview](14-coding-agent-and-ide/README.md).

35. [Coding-assistant architecture](14-coding-agent-and-ide/35-coding-assistant.md)
36. [Coding-assistant — operator runbook](14-coding-agent-and-ide/36-coding-assistant-operator.md)
73. [Coding-model catalog & multi-model serving](14-coding-agent-and-ide/73-coding-model-catalog-and-serving.md) — *coding* chapter 73 (Part XIII also has a 73)
74. [The Daalu Coding Agent extension — internals](14-coding-agent-and-ide/74-coding-agent-extension.md)
75. [The workspace platform internals](14-coding-agent-and-ide/75-workspace-platform-internals.md)
76. [Private-repo authentication](14-coding-agent-and-ide/76-private-repo-authentication.md)
77. [The Daalu CLI — bringing the agent to the laptop](14-coding-agent-and-ide/77-daalu-cli.md)

## Part XV — The AI-Ops Maturity Model

The AI-Ops Maturity Model (AOMM): five TRACE metrics (time to triage, time
to resolve, agreement rate, change-failure rate, escalation rate) and
the levels they score, then the practices that move them: autonomy policy as
code, guardrails and automatic execution, investigations and verdicts,
runbooks as code, the audit chain, evals and the MCP server. Start with the
[section overview](15-ai-ops-maturity/README.md).

81. [The maturity model](15-ai-ops-maturity/81-the-maturity-model.md)
82. [Autonomy policy as code](15-ai-ops-maturity/82-autonomy-policy.md)
83. [Guardrails and automatic execution](15-ai-ops-maturity/83-guardrails-and-auto-execution.md)
84. [Investigations and verdicts](15-ai-ops-maturity/84-investigations-and-verdicts.md)
85. [Runbooks as code](15-ai-ops-maturity/85-runbooks-as-code.md)
86. [Change context and previews](15-ai-ops-maturity/86-change-context-and-previews.md)
87. [The audit log](15-ai-ops-maturity/87-audit-log.md)
88. [Metrics and maturity scoring](15-ai-ops-maturity/88-metrics-and-maturity-scoring.md)
89. [The eval harness](15-ai-ops-maturity/89-eval-harness.md)
90. [The MCP server and open specs](15-ai-ops-maturity/90-mcp-server-and-open-specs.md)

---

**Conventions used in this manual**

- Code references: `src/daalu_automation/api/main.py:142`.
- Shell commands are shown with the kubeconfig env var explicit:
  `kubectl --kubeconfig "$KUBECONFIG_OPS" …`. The conventions for that
  variable are set in Part VIII.
- "We" means the engineering team. "You" means whoever is reading.
- When a section explains a design decision, it tries to also explain
  what the rejected alternative looked like and why we didn't take it —
  these are the most expensive details to recover from `git log` later.

**A note on chapter numbers.** Chapters keep their original sequential
numbers even after being moved into section folders, so cross-references
written in earlier chapters still resolve. That renumbering history has
left two duplicate pairs — **59** (Keycloak hub SSO in Part X vs Test
layout in Part XI) and **73** (Part XIII auth chapter vs Part XIV
coding chapter) — always disambiguate by folder when cross-referencing.
New chapters added after the original outline slot into the part they
thematically belong to and take the next free number at the end of the
manual: **78** (managed-infra console), **79** (fabric topology & device
terminal), **80** (zero-touch GPU onboarding). Chapters **81–90** are the
AI-Ops Maturity Model and live together in Part XV. Chapters **91–99** fill
gaps for features that predate them (Compose install, observability
installer, AIPerf, GPU sharing, GPU health, NV-CM rendering, backups, cable
validation/ZTP, NV-CM's MCP server) and sit in Parts IV, VI, VII and VIII.

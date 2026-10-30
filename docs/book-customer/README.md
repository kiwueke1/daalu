# Daalu Automation — The Operator's Handbook

A complete reference for using Daalu Automation. Start at the beginning if
you're new; jump to any chapter if you're looking for one specific feature.

This handbook covers the **product** — what Daalu does, how to use it,
and how to onboard your infrastructure. It deliberately
contains no code, no `kubectl`, and no internal architecture details
(beyond the two `helm install` commands you run to connect your own
cluster). For that material, see the *Engineering & Operations Manual*.

Chapters live in part-numbered folders so the book opens predictably in
a file browser; chapters keep their sequential numbers inside the
folders.

---

## Part I — Introduction

1. [What is Daalu Automation?](01-introduction/01-what-is-daalu.md)
2. [How it works](01-introduction/02-how-it-works.md)
3. [Who Daalu is for](01-introduction/03-who-its-for.md)

## Part II — Getting Started

4. [Signing up and creating your tenant](02-getting-started/04-signing-up.md)
5. [Your first five minutes on the home dashboard](02-getting-started/05-home-dashboard.md)
6. [Inviting your team](02-getting-started/06-inviting-your-team.md)
7. [Connecting your first cloud account](02-getting-started/07-first-cloud-account.md)
8. [Onboarding your observability stack](02-getting-started/08-onboarding-your-observability-stack.md)

## Part III — Core Concepts

9. [Tenants, users, and access control](03-core-concepts/09-tenants-and-access.md)
10. [Integrations and adapters](03-core-concepts/10-integrations-and-adapters.md)
11. [Events, alerts, and notifications](03-core-concepts/11-events-alerts-notifications.md)
12. [The AI Assistant](03-core-concepts/12-ai-assistant.md)
13. [Source of Truth — your network devices](03-core-concepts/13-source-of-truth.md)
14. [Cluster federation — managing your own infrastructure](03-core-concepts/14-cluster-federation.md)
15. [Local GPU and the LLM router](03-core-concepts/15-local-gpu-and-llm-router.md)

## Part IV — Feature Walkthroughs

16. [The Landing page](04-feature-walkthroughs/16-landing-page.md)
17. [Managed Infrastructure](04-feature-walkthroughs/17-managed-infrastructure.md)
18. [Operations — devices, drift, change proposals](04-feature-walkthroughs/18-operations.md)
19. [Agents — autonomous helpers](04-feature-walkthroughs/19-agents.md)
20. [Alerts and incident response](04-feature-walkthroughs/20-alerts-and-incidents.md)
21. [Workflows and automations](04-feature-walkthroughs/21-workflows.md)
22. [Billing and spend](04-feature-walkthroughs/22-billing-and-spend.md)
23. [Reports](04-feature-walkthroughs/23-reports.md)
24. [Settings — profile, preferences, API tokens](04-feature-walkthroughs/24-settings.md)
25. [Help and feedback](04-feature-walkthroughs/25-help-and-feedback.md)
26. [The Coding Assistant](04-feature-walkthroughs/26-coding-assistant.md)
27. [Network & server management](04-feature-walkthroughs/27-network-and-server-management.md)
47. [Using Daalu from your terminal (the `daalu` CLI)](04-feature-walkthroughs/47-daalu-cli-terminal.md)
48. [Dashboards — your Grafana boards, rendered natively](04-feature-walkthroughs/48-dashboards.md)
49. [Fabric topology — the live network map](04-feature-walkthroughs/49-network-topology.md)
50. [AI Factory — GPU observability and operations](04-feature-walkthroughs/50-ai-factory.md)

## Part V — Integrations Catalog

28. [Cloud providers (AWS, GCP, Azure)](05-integrations-catalog/28-cloud-providers.md)
29. [Notifications (Slack, PagerDuty, email)](05-integrations-catalog/29-notifications.md)
30. [Network source of truth (Nautobot)](05-integrations-catalog/30-nautobot.md)
31. [Observability (Prometheus, Grafana, Loki, Thanos, OpenSearch)](05-integrations-catalog/31-observability-integrations.md)
32. [Reserved](05-integrations-catalog/32-crm-integrations.md)
33. [Webhooks and custom sources](05-integrations-catalog/33-webhooks.md)

## Part VI — Advanced Topics

34. [Deploying daalu-edge to your own cluster](06-advanced-topics/34-deploying-daalu-edge.md)
35. [Connecting your own GPU — end-to-end](06-advanced-topics/35-local-gpu-skus.md)
36. [How the LLM router decides](06-advanced-topics/36-llm-router-decisions.md)
37. [Personal access tokens — the automation API](06-advanced-topics/37-personal-access-tokens.md)
38. [The Daalu-hosted AI tier](06-advanced-topics/38-daalu-hosted-ai.md)
39. [Daalu Private](06-advanced-topics/39-daalu-private.md)

## Part VIII — Support

44. [Troubleshooting](08-support/44-troubleshooting.md)
45. [Frequently asked questions](08-support/45-faq.md)
46. [Glossary](08-support/46-glossary.md)

---

**Conventions used in this book**

- Bold UI element names — *click **Save changes***.
- Inline `code style` for things you'd type in a field — usernames,
  hostnames, search queries.
- Quoted "exact label" — for menu items and button text.

**A note on chapter numbers.** Chapters keep their sequential numbers
(01–50) even when they live inside a section folder, so links written in
earlier chapters still resolve and the table of contents reads like a
single book. Chapters added after the original outline was finalised
(47–50) slot into the section they thematically belong to rather than
the numeric end of the book — which is why Part IV's numbering jumps
from 27 to 47.

"""Daalu Automation — AI Operations Command Center.

Event-driven operational intelligence platform. Continuously ingests
operational signals from across a company's systems, reasons over them
with LLMs, and turns them into briefings, alerts, recommendations, and
automated actions.

The platform is built around a small set of reusable primitives:

- ``core.events``      — typed events flowing through a Redis stream bus
- ``core.agents``      — long-running AI workers that observe events and act
- ``core.briefings``   — pluggable AI-generated reports (morning/weekly/ad-hoc)
- ``core.integrations``— adapters that pull/push to external systems
- ``modules/``         — per-domain implementations (infra, …)

New domains ship as a new ``modules/<name>/`` package that registers
events, agents, briefings, and an API router. See ``docs/extending.md``.
"""

__version__ = "0.1.0"

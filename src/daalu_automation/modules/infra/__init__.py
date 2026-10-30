"""IT / Infrastructure / SRE module.

Provides:

- ``InfraAgent``             — listens for ``infra.alert.fired`` / ``infra.incident.opened``,
                              triages, summarises, recommends remediation.
- ``InfraBriefingGenerator``  — daily morning infrastructure briefing.
- ``PrometheusAdapter``       — pulls firing alerts from Alertmanager.
- ``PagerDutyAdapter``        — pulls open incidents from PagerDuty.
- ``incident_coordination_workflow`` — opens an incident, summarises logs,
                                       drafts a status update, pings #incidents.

Importing this package registers everything with the core registries.
"""

from daalu_automation.modules.infra import agent, briefing, integrations, workflows  # noqa: F401

"""Smoke tests that don't need infrastructure.

These verify the package imports cleanly + every module registers its
agent/briefing/integration on import. Useful pre-flight before booting
the full stack.
"""

from __future__ import annotations


def test_package_imports():
    import daalu_automation
    import daalu_automation.api.main  # noqa: F401
    import daalu_automation.modules  # noqa: F401

    assert daalu_automation.__version__ == "0.1.0"


def test_agents_registered():
    import daalu_automation.modules  # noqa: F401
    from daalu_automation.core.agents import list_agents

    names = {d.name for d in list_agents()}
    assert "infra-agent" in names


def test_briefings_registered():
    import daalu_automation.modules  # noqa: F401
    from daalu_automation.core.briefings import list_briefings
    from daalu_automation.models import BriefingChannel

    channels = set(list_briefings())
    assert BriefingChannel.infra in channels


def test_integrations_registered():
    import daalu_automation.modules  # noqa: F401
    from daalu_automation.core.integrations import list_integrations

    providers = {d.provider for d in list_integrations()}
    # Synthetic adapters always register so the platform demos cleanly.
    assert "synthetic-infra" in providers


def test_workflows_registered():
    import daalu_automation.modules  # noqa: F401
    from daalu_automation.core.workflows import list_workflows

    names = {n for n, _ in list_workflows()}
    assert "infra.incident.coordinate" in names

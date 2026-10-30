"""DeviceAdapter abstract base.

Adapter implementations sit in sibling modules (``linux_ssh.py``,
future ``redfish.py``, future ``scrapli_iosxr.py`` …) and self-register
into :mod:`daalu_automation.core.device.registry` at import time.

Hard rule (enforced by the calling code in
:mod:`daalu_automation.core.change_proposals`): adapters expose
``execute()`` but **nothing outside change_proposals.execute() may
call it**. That's how we keep the engine — and any other code path
that talks to a DeviceAdapter directly — from bypassing the approval
gate.
"""

from __future__ import annotations

import abc
from typing import ClassVar

from daalu_automation.core.device.models import (
    ConfigDiff,
    Credentials,
    ExecutionResult,
    RenderedConfig,
)
from daalu_automation.core.sot.models import DeviceFacts


class DeviceAdapter(abc.ABC):
    transport: ClassVar[str]

    @abc.abstractmethod
    async def collect(
        self,
        creds: Credentials,
        intended_hint: DeviceFacts | None = None,
    ) -> DeviceFacts:
        """Read the device's current state.

        ``intended_hint`` lets the adapter scope its observation to keys
        that actually appear in intended config — never enumerate every
        installed package or every sysctl, only check the ones we manage.

        Each adapter expects (and returns) the specific facts subtype
        matching its transport (``LinuxSSHAdapter`` works in
        :class:`LinuxFacts`, ``RedfishAdapter`` in :class:`RedfishFacts`).
        The :class:`DeviceFacts` union is what the dispatch layer
        (reconciler / executor) trades in.
        """

    @abc.abstractmethod
    async def render(self, intended: DeviceFacts) -> RenderedConfig: ...

    @abc.abstractmethod
    async def diff(
        self, observed: DeviceFacts, intended: DeviceFacts
    ) -> ConfigDiff: ...

    @abc.abstractmethod
    async def execute(
        self, creds: Credentials, rendered: RenderedConfig
    ) -> ExecutionResult: ...

"""Device adapter family — collect / render / diff / execute against a real machine.

Adapters self-register at import time into
:mod:`daalu_automation.core.device.registry`. Importing
:mod:`daalu_automation.core.device.linux_ssh` (which this module does
side-effectfully) is what makes ``get_device_adapter("linux_ssh")``
resolve.
"""

# Side-effect imports: each registers an adapter into the registry.
from daalu_automation.core.device import (
    config_manager,  # noqa: F401 — NV-CM-backed switch executor
    eos,  # noqa: F401
    iosxr,  # noqa: F401
    junos,  # noqa: F401
    linux_ssh,  # noqa: F401
    redfish,  # noqa: F401
)
from daalu_automation.core.device.base import DeviceAdapter
from daalu_automation.core.device.models import (
    ConfigDiff,
    Credentials,
    ExecutionResult,
    RenderedConfig,
)
from daalu_automation.core.device.registry import (
    get_device_adapter,
    list_device_adapters,
    register_device_adapter,
)

__all__ = [
    "ConfigDiff",
    "Credentials",
    "DeviceAdapter",
    "ExecutionResult",
    "RenderedConfig",
    "get_device_adapter",
    "list_device_adapters",
    "register_device_adapter",
]

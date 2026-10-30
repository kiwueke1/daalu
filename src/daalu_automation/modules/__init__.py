"""Domain modules.

Each module is a self-contained package that wires together:

- briefing generator(s)
- agent(s)
- integration adapter(s)
- workflow(s)
- API router

Modules register themselves with the core registries at import time, so
``import daalu_automation.modules`` is the only thing the bootstrap has
to call to make every module available.
"""

from daalu_automation.modules import infra  # noqa: F401  (side-effect imports)

"""Client for driving an existing Tinkerbell stack via its CRDs.

Servers are provisioned by **Tinkerbell** (Tink + SMEE + Hegel + Rufio),
which the lower-level ``daalu`` bare-metal project already deploys on the
mgmt cluster. daalu-automation does **not** deploy Tinkerbell — it talks to
its Kubernetes CRDs over the WireGuard tunnel (apply/watch), exactly as the
``daalu`` project does locally.

CRDs used (verified in
``daalu/src/daalu/bootstrap/mgmt/tinkerbell_installer.py``):
* ``tinkerbell.org/v1alpha1`` — Hardware, Template, Workflow
* ``bmc.tinkerbell.org/v1alpha1`` — Machine, Job (Rufio BMC power/boot)

See docs/design/nv-config-manager-integration.md §14–§18.
"""

from __future__ import annotations

from daalu_automation.core.tinkerbell.client import (
    TinkerbellClient,
    TinkerbellError,
)

__all__ = ["TinkerbellClient", "TinkerbellError"]

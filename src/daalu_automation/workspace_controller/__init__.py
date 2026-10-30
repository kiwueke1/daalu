"""Workspace controller for the browser-IDE coding assistant.

Reconciles the desired ``workspaces`` table against actual K8s state:
one Deployment + one PVC per row in ``state='provisioning'`` or
``'active'``, scaled to 0 for ``'paused'``, deleted for
``'destroyed'``.

The controller has its own entry point (``daalu workspace-controller``)
and runs as a separate Deployment so its privileged K8s permissions
(creating Deployments + PVCs) don't bleed into daalu-api's blast
radius.

See ``docs/design/coding-assistant.md`` for the lifecycle diagram and
the tenant-isolation contract.
"""

"""Fault injection for lab clusters and fabrics (``daalu fault``).

Break a Kubernetes cluster, its nodes, an OpenStack-on-Kubernetes install, or
a network fabric on purpose, in a way that is always revertible, so Daalu has
a real incident to find and fix. Design: ``docs/fault-injection.md``.

This is deliberately *not* part of the Daalu API or UI. It runs on the
operator's machine with the operator's own kubeconfig / SSH / docker access and
never talks to Daalu, so:

* the product never gains a "break this cluster" capability that a tenant
  admin, a leaked token or the AI agent itself could reach;
* the AI under test can't learn what was injected from the platform it runs on.
"""

"""nautobot-controller — per-tenant Nautobot lifecycle manager.

Replaces the pre-2026 shared-Nautobot-with-ObjectPermission-slice
model with one fully-isolated Nautobot stack per Daalu tenant.

Two deployment targets, picked per row via the
``target_cluster_tunnel_id`` foreign key on the ``nautobot_tenants``
table:

* **Operator-cluster (default).** Stack lives in the hub cluster,
  alongside daalu-api. ``<slug>.sot.daalu.io`` ingress per tenant,
  cert-manager HTTP-01 cert each.
* **Customer-cluster** (set ``target_cluster_tunnel_id``). Stack
  lives in the customer's federated cluster, reached via WireGuard.
  No public ingress — daalu-api talks to it via the wg tunnel using
  the kubeconfig stored on the ``Integration(provider="kubernetes")``
  row referenced by the cluster_tunnel.

The controller has its own entry point (``daalu nautobot-controller``)
and is meant to run as a single-replica Deployment in
``daalu-automation`` with namespaced RBAC into any namespace it
materialises (operator-cluster mode) plus the ability to read
kubeconfigs from cluster_tunnels (customer-cluster mode).
"""

from __future__ import annotations

"""gpu-controller — per-tenant local-GPU (vLLM) lifecycle.

Reconciles ``gpu_tenants`` rows into the existing ``deploy/k8s/gpu``
vLLM serving stack, applied onto the operator's cluster or a joined
customer cluster over the WireGuard tunnel. Mirrors
``nautobot_controller``; reuses its generic K8s apply helpers.

vLLM serving itself is NOT reimplemented here — this package only
*deploys and tracks* the upstream manifests.
"""

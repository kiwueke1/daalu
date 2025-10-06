# src/daalu/helm/errors.py
class HelmError(RuntimeError):
    """Base class for Helm-related failures."""

class HelmDiffError(HelmError):
    """Raised when helm diff fails for non-diff reasons."""

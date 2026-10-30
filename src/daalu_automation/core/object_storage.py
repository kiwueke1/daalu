"""Platform object storage (MinIO / S3 / Rook-Ceph RGW) — the operator's own
bucket store, distinct from ``core.cloud_aws`` (which holds *tenant* AWS creds).

Thin boto3 wrapper around ``settings.s3_*`` for the platform's internal blobs.
First consumer: AIPerf run artifacts — the gpu-controller's uploader sidecar
pushes the ``profile_export_aiperf.csv/json`` + log tree here, and the API
streams them back for download (``api/routers/gpu_metrics``).

Everything here is **synchronous** boto3; async callers wrap in
``asyncio.to_thread``. The client is cached per (endpoint, key) so we don't
rebuild a session per object.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from daalu_automation.config import get_settings


@lru_cache(maxsize=4)
def _client(endpoint: str, access_key: str, secret_key: str) -> Any:
    import boto3  # local import keeps the cold path light
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",  # MinIO/RGW ignore it; boto3 requires one
        # Path-style addressing — MinIO/RGW don't do virtual-host buckets.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def s3_client() -> Any:
    s = get_settings()
    return _client(s.s3_endpoint_url, s.s3_access_key, s.s3_secret_key)


def aiperf_client() -> Any:
    """Client for the AIPerf artifact store as reached FROM THE HUB.

    AIPerf artifacts live in the workload cluster's object store; the hub reads
    them back over the tunnel via ``gpu_aiperf_s3_endpoint_hub``. Each field
    falls back to the platform ``s3_*`` when unset (single-cluster / dev).
    """
    s = get_settings()
    return _client(
        s.gpu_aiperf_s3_endpoint_hub or s.s3_endpoint_url,
        s.gpu_aiperf_s3_access_key or s.s3_access_key,
        s.gpu_aiperf_s3_secret_key or s.s3_secret_key,
    )


def ensure_bucket(bucket: str) -> None:
    """Create ``bucket`` if it does not already exist (idempotent)."""
    client = s3_client()
    try:
        client.head_bucket(Bucket=bucket)
        return
    except Exception:  # noqa: BLE001 — head 404/403 both mean "create it"
        pass
    try:
        client.create_bucket(Bucket=bucket)
    except Exception:  # noqa: BLE001 — already-exists race is fine
        pass


def list_objects(
    bucket: str, prefix: str, client: Any | None = None
) -> list[dict[str, Any]]:
    """List objects under ``prefix`` → ``[{key, size}]`` (sorted by key)."""
    client = client or s3_client()
    out: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            out.append({"key": obj["Key"], "size": int(obj.get("Size", 0))})
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    out.sort(key=lambda o: o["key"])
    return out


def get_object_bytes(bucket: str, key: str, client: Any | None = None) -> bytes:
    """Fetch a single object's full body."""
    resp = (client or s3_client()).get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def iter_object(
    bucket: str, key: str, chunk_size: int = 1 << 16, client: Any | None = None
) -> Iterator[bytes]:
    """Stream an object's body in chunks (for a download response)."""
    resp = (client or s3_client()).get_object(Bucket=bucket, Key=key)
    body = resp["Body"]
    try:
        while True:
            chunk = body.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        body.close()

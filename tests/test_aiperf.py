"""AIPerf benchmarking + reliability (NVSentinel) surface.

Pure-function coverage (arg/job builders, output + artifact parsers,
concurrency normalisation) runs without a DB. The HTTP-surface tests prove the
access model: a site superuser benchmarks freely and sees every run; a GPU
owner/provider benchmarks only their own endpoint and sees only their own runs;
everyone else gets 403. They also cover the artifact list/download endpoints.
Prometheus is unconfigured in tests, so the reliability endpoint exercises its
graceful "unknown"/"n/a" paths.
"""

from __future__ import annotations

import json

from daalu_automation.core import object_storage
from daalu_automation.gpu_controller import aiperf
from daalu_automation.models.aiperf_run import AiperfRun, AiperfRunState
from daalu_automation.models.gpu_pool import GpuPool
from daalu_automation.models.gpu_tenant import GpuTenant, GpuTenantState
from daalu_automation.models.tenant import Tenant
from tests.conftest import auth_headers

# ── pure: builders + normalisation ─────────────────────────────────────────


def test_normalise_concurrency():
    assert aiperf.normalise_concurrency("1,2,4,8") == "1,2,4,8"
    # spaces tolerated, dupes dropped, order preserved
    assert aiperf.normalise_concurrency(" 1, 2 ,2, 4 ") == "1,2,4"
    # garbage falls back to the default sweep
    assert aiperf.normalise_concurrency("abc") == aiperf.DEFAULT_CONCURRENCY
    assert aiperf.normalise_concurrency("") == aiperf.DEFAULT_CONCURRENCY
    # per-level ceiling
    assert aiperf.normalise_concurrency("99999") == "1024"


def test_aiperf_args_shape():
    args = aiperf.aiperf_args(
        model="m",
        url="http://svc:80",
        concurrency="1,2,4",
        request_count=200,
        input_tokens=512,
        output_tokens=256,
    )
    assert args[0] == "profile"
    assert "--model=m" in args
    assert "--url=http://svc:80" in args
    assert "--streaming" in args
    assert "--concurrency=1,2,4" in args
    # bounds clamp
    big = aiperf.aiperf_args(
        model="m",
        url="u",
        concurrency="1",
        request_count=10_000,
        input_tokens=99_999,
        output_tokens=99_999,
    )
    assert "--request-count=2000" in big
    assert "--synthetic-input-tokens-mean=8192" in big
    assert "--output-tokens-mean=8192" in big
    # auth header only when a token is given
    assert all("Authorization" not in a for a in args)
    authed = aiperf.aiperf_args(
        model="m",
        url="u",
        concurrency="1",
        request_count=1,
        input_tokens=1,
        output_tokens=1,
        auth_token="tok",
    )
    assert any(a == "--header=Authorization: Bearer tok" for a in authed)


def test_aiperf_bench_job_requests_no_gpu():
    job = aiperf.aiperf_bench_job(
        "daalu",
        model="m",
        url="http://llm-classifier.daalu.svc.cluster.local:80",
        concurrency="1,2,4",
        request_count=10,
        input_tokens=128,
        output_tokens=64,
        name="daalu-aiperf-abc123",
    )
    assert job["kind"] == "Job"
    assert job["spec"]["backoffLimit"] == 0
    spec = job["spec"]["template"]["spec"]
    # No uploader requested → a single container invoked with bare args.
    assert len(spec["containers"]) == 1
    container = spec["containers"][0]
    # The client must NOT request a GPU — the GPU is the thing under test.
    limits = container["resources"].get("limits", {})
    requests = container["resources"].get("requests", {})
    assert "nvidia.com/gpu" not in limits and "nvidia.com/gpu" not in requests
    assert any("--url=http://llm-classifier" in a for a in container["args"])


def test_aiperf_bench_job_with_uploader_sidecar():
    job = aiperf.aiperf_bench_job(
        "daalu",
        model="m",
        url="http://svc:80",
        concurrency="1,2",
        request_count=10,
        input_tokens=128,
        output_tokens=64,
        artifacts_s3={
            "endpoint": "http://minio:9000",
            "access_key": "ak",
            "secret_key": "sk",
            "bucket": "daalu-aiperf",
            "prefix": "aiperf/run-1",
        },
        uploader_image="minio/mc:test",
    )
    spec = job["spec"]["template"]["spec"]
    names = [c["name"] for c in spec["containers"]]
    assert names == ["aiperf", "artifact-uploader"]
    # aiperf is wrapped in a python launcher (the image is shell-less) that runs
    # the sweep then drops the DONE sentinel for the uploader. Args ride along as
    # a JSON array in argv so no shell quoting is involved.
    aiperf_c = spec["containers"][0]
    assert aiperf_c["command"][:2] == ["python3", "-c"]
    assert "/signal/DONE" in aiperf_c["command"][2]
    passed_args = json.loads(aiperf_c["command"][3])
    assert passed_args[0] == "profile"
    assert "--model=m" in passed_args
    # The uploader carries the S3 creds + destination as env.
    up = spec["containers"][1]
    assert up["image"] == "minio/mc:test"
    env = {e["name"]: e["value"] for e in up["env"]}
    assert env["S3_BUCKET"] == "daalu-aiperf"
    assert env["S3_PREFIX"] == "aiperf/run-1"
    assert env["S3_ENDPOINT"] == "http://minio:9000"
    # Two shared volumes: the artifact tree + the sentinel channel.
    vol_names = {v["name"] for v in spec["volumes"]}
    assert vol_names == {"artifacts", "signal"}


def test_parse_aiperf_artifacts_curve():
    def _report(ttft: float, thru: float) -> bytes:
        return json.dumps(
            {
                "records": {
                    "time_to_first_token": {"avg": ttft},
                    "output_token_throughput": {"avg": thru},
                }
            }
        ).encode()

    files = {
        "ISL200_OSL200/CON10/profile_export_aiperf.json": _report(40.0, 900.0),
        "ISL200_OSL200/CON1/profile_export_aiperf.json": _report(20.0, 120.0),
        # non-JSON sibling + a log are ignored
        "ISL200_OSL200/CON1/profile_export_aiperf.csv": b"a,b\n1,2\n",
        "ISL200_OSL200/CON1/logs/aiperf.log": b"...",
    }
    levels = aiperf.parse_aiperf_artifacts(files)
    # Two JSONs → two rows, sorted ascending by concurrency.
    assert [lv["concurrency"] for lv in levels] == [1, 10]
    assert levels[0]["isl"] == 200 and levels[0]["osl"] == 200
    assert levels[0]["metrics"]["ttft_ms"] == 20.0
    assert levels[1]["metrics"]["output_token_throughput"] == 900.0


def test_parse_aiperf_artifacts_curve_0_9_0_layout():
    """AIPerf 0.9.0 keys the tree by ``concurrency_{n}`` and carries the seq
    lengths in the report (not the path); the ``aggregate``/``sweep`` roll-ups
    must not be counted as curve points."""

    def _report(ttft: float, thru: float) -> bytes:
        return json.dumps(
            {
                "time_to_first_token": {"avg": ttft},
                "output_token_throughput": {"avg": thru},
                "input_sequence_length": {"avg": 64.0},
                "output_sequence_length": {"avg": 32.0},
            }
        ).encode()

    files = {
        "concurrency_2/profile_export_aiperf.json": _report(40.0, 900.0),
        "concurrency_1/profile_export_aiperf.json": _report(20.0, 120.0),
        # roll-ups carry the _aggregate / _sweep suffix → not curve points
        "aggregate/concurrency_1/profile_export_aiperf_aggregate.json": _report(0, 0),
        "sweep_aggregate/profile_export_aiperf_sweep.json": _report(0, 0),
        # non-JSON siblings ignored
        "concurrency_1/profile_export_aiperf.csv": b"a,b\n1,2\n",
        "concurrency_1/logs/aiperf.log": b"...",
    }
    levels = aiperf.parse_aiperf_artifacts(files)
    # Exactly the two per-level reports, sorted ascending by concurrency.
    assert [lv["concurrency"] for lv in levels] == [1, 2]
    # ISL/OSL come from the report doc, rounded.
    assert levels[0]["isl"] == 64 and levels[0]["osl"] == 32
    assert levels[0]["metrics"]["ttft_ms"] == 20.0
    assert levels[1]["metrics"]["output_token_throughput"] == 900.0


def test_parse_aiperf_output_table():
    table = """
                          NVIDIA AIPerf | LLM Metrics
    | Time To First Token (ms)            |   42.50 |   30.1 |
    | Inter Token Latency (ms)            |    8.20 |    6.0 |
    | Request Latency (ms)                |  900.00 |        |
    | Output Token Throughput (tokens/sec)|  1234.5 |        |
    | Request Throughput (per sec)        |    12.3 |        |
    """
    ok, summary = aiperf.parse_aiperf_output(table)
    assert ok is True
    m = summary["metrics"]
    assert m["ttft_ms"] == 42.5
    assert m["itl_ms"] == 8.2
    assert m["output_token_throughput"] == 1234.5
    assert m["request_throughput"] == 12.3


def test_parse_aiperf_output_json_and_empty():
    ok, summary = aiperf.parse_aiperf_output(
        '{"records": {"time_to_first_token": {"avg": 50.0},'
        ' "output_token_throughput": {"avg": 999.0}}}'
    )
    assert ok is True
    assert summary["metrics"]["ttft_ms"] == 50.0
    assert summary["metrics"]["output_token_throughput"] == 999.0
    # empty / unparseable
    bad, s2 = aiperf.parse_aiperf_output("")
    assert bad is False and "error" in s2
    bad2, s3 = aiperf.parse_aiperf_output("no numbers or labels here")
    assert bad2 is False and "error" in s3


# ── HTTP: super-admin gate ─────────────────────────────────────────────────


async def _make_gpu(db, tenant_id, *, shared: bool):
    row = GpuTenant(
        tenant_id=tenant_id,
        state=GpuTenantState.active,
        gpu_class="ada-16",
        model_classifier="meta/llama-3.1-8b-instruct",
        shared=shared,
    )
    db.add(row)
    await db.commit()
    return row


async def test_aiperf_requires_superuser(client, seeded):
    # A tenant admin (is_admin, not is_superuser) is forbidden everywhere.
    for method, path, body in [
        ("get", "/api/v1/ai-factory/aiperf/runs", None),
        ("post", "/api/v1/ai-factory/aiperf/runs", {"concurrency": "1,2"}),
    ]:
        fn = getattr(client, method)
        r = await fn(path, headers=auth_headers(seeded.token_a), json=body) if body else await fn(
            path, headers=auth_headers(seeded.token_a)
        )
        assert r.status_code == 403, (method, path)


async def test_aiperf_superuser_can_create_and_list(client, seeded):
    # Superuser kicks a run; it is listed site-wide with the canonicalised sweep.
    r = await client.post(
        "/api/v1/ai-factory/aiperf/runs",
        headers=auth_headers(seeded.token_super),
        json={"concurrency": " 1, 2 ,2, 8 ", "request_count": 50, "via_gateway": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "pending"
    run_id = body["id"]

    lst = await client.get(
        "/api/v1/ai-factory/aiperf/runs", headers=auth_headers(seeded.token_super)
    )
    assert lst.status_code == 200
    runs = lst.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["id"] == run_id
    assert runs[0]["concurrency"] == "1,2,8"  # normalised
    assert runs[0]["via_gateway"] is True

    detail = await client.get(
        f"/api/v1/ai-factory/aiperf/runs/{run_id}",
        headers=auth_headers(seeded.token_super),
    )
    assert detail.status_code == 200
    assert "output" in detail.json()


# ── HTTP: owner/provider scope + target restriction ────────────────────────


async def test_aiperf_no_gpu_admin_forbidden(client, seeded):
    # A tenant admin with no GPU (token_a) is not a site operator and owns no
    # card → forbidden on the listing.
    r = await client.get(
        "/api/v1/ai-factory/aiperf/runs", headers=auth_headers(seeded.token_a)
    )
    assert r.status_code == 403


async def test_aiperf_owner_runs_own_endpoint(client, db, seeded):
    await _make_gpu(db, seeded.tenant_b_id, shared=False)
    r = await client.post(
        "/api/v1/ai-factory/aiperf/runs",
        headers=auth_headers(seeded.token_b),
        json={"concurrency": "1,2,4", "request_count": 20},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]
    detail = (
        await client.get(
            f"/api/v1/ai-factory/aiperf/runs/{run_id}",
            headers=auth_headers(seeded.token_b),
        )
    ).json()
    # Pinned to the owner's own namespace Service + their served model.
    assert detail["target_url"] == "http://llm-classifier.daalu.svc.cluster.local:80"
    assert detail["model"] == "meta/llama-3.1-8b-instruct"
    lst = await client.get(
        "/api/v1/ai-factory/aiperf/runs", headers=auth_headers(seeded.token_b)
    )
    assert lst.json()["scope"] == "owner"


async def test_aiperf_owner_cannot_target_arbitrary(client, db, seeded):
    await _make_gpu(db, seeded.tenant_b_id, shared=False)
    r = await client.post(
        "/api/v1/ai-factory/aiperf/runs",
        headers=auth_headers(seeded.token_b),
        json={"concurrency": "1", "target_url": "http://evil.internal"},
    )
    assert r.status_code == 403


async def test_aiperf_provider_targets_pool_upstream(client, db, seeded):
    tenant = await db.get(Tenant, seeded.tenant_b_id)
    tenant.is_gpu_provider = True
    db.add(
        GpuPool(
            provider_tenant_id=seeded.tenant_b_id,
            upstream_url="http://pool-upstream:8888",
            served_models=["meta/llama-x"],
        )
    )
    await db.commit()
    r = await client.post(
        "/api/v1/ai-factory/aiperf/runs",
        headers=auth_headers(seeded.token_b),
        json={"concurrency": "1", "model": "not-served"},
    )
    assert r.status_code == 200, r.text
    detail = (
        await client.get(
            f"/api/v1/ai-factory/aiperf/runs/{r.json()['id']}",
            headers=auth_headers(seeded.token_b),
        )
    ).json()
    assert detail["target_url"] == "http://pool-upstream:8888"
    # A non-served model is coerced to one the pool actually serves.
    assert detail["model"] == "meta/llama-x"


async def test_aiperf_runs_are_tenant_scoped(client, db, seeded):
    # The superuser's run (tenant_a) is invisible to an owner in tenant_b.
    await _make_gpu(db, seeded.tenant_b_id, shared=False)
    su = await client.post(
        "/api/v1/ai-factory/aiperf/runs",
        headers=auth_headers(seeded.token_super),
        json={"concurrency": "1"},
    )
    own = await client.post(
        "/api/v1/ai-factory/aiperf/runs",
        headers=auth_headers(seeded.token_b),
        json={"concurrency": "1"},
    )
    assert su.status_code == 200 and own.status_code == 200
    lb = await client.get(
        "/api/v1/ai-factory/aiperf/runs", headers=auth_headers(seeded.token_b)
    )
    assert [r["id"] for r in lb.json()["runs"]] == [own.json()["id"]]
    ls = await client.get(
        "/api/v1/ai-factory/aiperf/runs", headers=auth_headers(seeded.token_super)
    )
    assert {su.json()["id"], own.json()["id"]} <= {
        r["id"] for r in ls.json()["runs"]
    }
    # Owner fetching the superuser's run → 404 (don't leak its existence).
    d = await client.get(
        f"/api/v1/ai-factory/aiperf/runs/{su.json()['id']}",
        headers=auth_headers(seeded.token_b),
    )
    assert d.status_code == 404


async def test_aiperf_artifacts_list_and_download(client, db, seeded, monkeypatch):
    await _make_gpu(db, seeded.tenant_b_id, shared=False)
    run = AiperfRun(
        tenant_id=seeded.tenant_b_id,
        state=AiperfRunState.passed,
        target_url="http://x",
        model="m",
        concurrency="1",
        request_count=1,
        input_tokens=1,
        output_tokens=1,
        summary={
            "artifacts": [
                {"path": "ISL/CON1/profile_export_aiperf.json", "size": 12}
            ],
            "artifacts_bucket": "daalu-aiperf",
        },
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    lst = await client.get(
        f"/api/v1/ai-factory/aiperf/runs/{run.id}/artifacts",
        headers=auth_headers(seeded.token_b),
    )
    assert lst.status_code == 200
    assert lst.json()["artifacts"][0]["path"] == "ISL/CON1/profile_export_aiperf.json"

    # Download streams from object storage (mocked). The endpoint passes an
    # explicit AIPerf client, so the stub must accept the keyword.
    monkeypatch.setattr(object_storage, "aiperf_client", lambda: object())
    monkeypatch.setattr(
        object_storage,
        "iter_object",
        lambda b, k, chunk_size=1 << 16, client=None: iter([b"hello-bytes"]),
    )
    dl = await client.get(
        f"/api/v1/ai-factory/aiperf/runs/{run.id}/artifacts/ISL/CON1/profile_export_aiperf.json",
        headers=auth_headers(seeded.token_b),
    )
    assert dl.status_code == 200
    assert dl.content == b"hello-bytes"

    # A path not in the run's manifest is refused (no arbitrary-key reads).
    bad = await client.get(
        f"/api/v1/ai-factory/aiperf/runs/{run.id}/artifacts/secret.txt",
        headers=auth_headers(seeded.token_b),
    )
    assert bad.status_code == 404


async def test_aiperf_cluster_tunnel_resolves_from_gpu_tenant(db, seeded):
    """The Job runs on the cluster hosting the run tenant's GpuTenant stack —
    its tunnel (None → hub when the tenant has no stack)."""
    from daalu_automation.gpu_controller.app import _aiperf_cluster_tunnel
    from daalu_automation.models.cluster_tunnel import ClusterTunnel

    tunnel = ClusterTunnel(
        tenant_id=seeded.tenant_b_id,
        slug="wl",
        name="workload",
        operator_pubkey="x",
        operator_privkey_encrypted="x",
        tunnel_ip="10.200.0.9",
    )
    db.add(tunnel)
    await db.commit()
    await db.refresh(tunnel)
    gt = GpuTenant(
        tenant_id=seeded.tenant_b_id,
        state=GpuTenantState.active,
        gpu_class="ada-16",
        model_classifier="m",
        target_cluster_tunnel_id=tunnel.id,
        shared=False,
    )
    db.add(gt)
    await db.commit()

    def _run(tid):
        return AiperfRun(
            tenant_id=tid,
            state=AiperfRunState.pending,
            target_url="http://x",
            model="m",
            concurrency="1",
            request_count=1,
            input_tokens=1,
            output_tokens=1,
        )

    # tenant with a stack on a tunnel → that tunnel
    assert await _aiperf_cluster_tunnel(db, _run(seeded.tenant_b_id)) == tunnel.id
    # tenant with no GpuTenant → None (hub)
    assert await _aiperf_cluster_tunnel(db, _run(seeded.tenant_a_id)) is None


# ── HTTP: reliability (read-only, tenant-scoped) ───────────────────────────


async def test_reliability_no_gpu_is_na(client, seeded):
    r = await client.get(
        "/api/v1/ai-factory/reliability", headers=auth_headers(seeded.token_b)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "n/a"
    assert body["has_gpu"] is False
    # cuda-checkpoint is hard-gated off (legal sign-off) by default.
    assert body["cuda_checkpoint"]["status"] == "gated"
    assert body["cuda_checkpoint"]["enabled"] is False


async def test_reliability_owner_without_prometheus(client, db, seeded):
    await _make_gpu(db, seeded.tenant_a_id, shared=False)
    r = await client.get(
        "/api/v1/ai-factory/reliability", headers=auth_headers(seeded.token_a)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["has_gpu"] is True
    # Prometheus unconfigured in tests -> honest "unknown", not a crash.
    assert body["status"] == "unknown"
    assert body["nvsentinel"]["active"] is False

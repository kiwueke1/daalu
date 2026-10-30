"""Workspace-controller resource builders.

Guards the Service↔proxy contract: the IDE reverse proxy in daalu-api
resolves a workspace pod by Service DNS (``ws-<short>.<ns>.svc:8080`` —
``api.ide_proxy._pod_url``). If the controller stops creating a Service
with that exact name/port, "Open IDE" 502s. These pure-function tests
pin the name, selector, and port so the two sides can't drift.
"""

from __future__ import annotations

import inspect

from starlette.routing import WebSocketRoute

from daalu_automation.api.ide_proxy import _pod_url, ide_proxy_ws
from daalu_automation.workspace_controller.k8s import (
    WORKSPACE_NAMESPACE,
    _bootstrap_script,
    _git_credentials_line,
    build_deployment_spec,
    build_git_secret_spec,
    build_service_spec,
    git_secret_name,
    resource_names,
)

WORKSPACE_ID = "360ce185-f1ab-496e-a297-c7fa1bce36e3"


def test_ide_ws_endpoint_takes_single_websocket():
    # Registered as a Starlette WebSocketRoute with a *function* endpoint,
    # so Starlette invokes it with one ready WebSocket — not a raw
    # (scope, receive, send) triple. The 3-arg form raises before the
    # handshake → the browser sees a 1006 abnormal close. Pin the arity.
    params = list(inspect.signature(ide_proxy_ws).parameters)
    assert params == ["browser"], params
    # And the route wrapper accepts it without raising (function → session).
    route = WebSocketRoute("/ide/{workspace_id:str}/{path:path}", endpoint=ide_proxy_ws)
    assert route.app is not None


def test_service_name_matches_deployment_name():
    # The proxy derives the Service host from resource_names()[0]; the
    # Service must carry that exact name or DNS won't resolve.
    dep_name, _ = resource_names(WORKSPACE_ID)
    svc = build_service_spec(WORKSPACE_ID)
    assert svc["kind"] == "Service"
    assert svc["metadata"]["name"] == dep_name
    assert svc["metadata"]["namespace"] == WORKSPACE_NAMESPACE


def test_service_selector_matches_pod_labels():
    # The Service must select the Deployment's pod template labels, else
    # it has no endpoints and the proxy gets connection-refused.
    dep = build_deployment_spec(
        workspace_id=WORKSPACE_ID,
        tenant_id="t",
        user_id="u",
        profile="small",
        model=None,
        git_repo_url=None,
        git_branch="main",
    )
    pod_labels = dep["spec"]["template"]["metadata"]["labels"]
    selector = build_service_spec(WORKSPACE_ID)["spec"]["selector"]
    assert selector.items() <= pod_labels.items()


def test_service_port_matches_proxy_url():
    # _pod_url hard-codes :8080; the Service must expose the same port.
    svc = build_service_spec(WORKSPACE_ID)
    assert svc["spec"]["ports"][0]["port"] == 8080
    assert ":8080" in _pod_url(WORKSPACE_ID, "/")
    # And the host the proxy targets is the Service FQDN.
    dep_name, _ = resource_names(WORKSPACE_ID)
    assert f"{dep_name}.{WORKSPACE_NAMESPACE}.svc.cluster.local" in _pod_url(
        WORKSPACE_ID, "/"
    )


# ── Private-repo PAT auth ────────────────────────────────────────────────


def test_git_credentials_line_picks_provider_username():
    # GitHub → x-access-token, GitLab → oauth2, else → git. PAT is the
    # password; the host (and port) carry through.
    gh = _git_credentials_line("https://github.com/acme/infra.git", "TOK")
    assert gh == "https://x-access-token:TOK@github.com"
    gl = _git_credentials_line("https://gitlab.com/acme/infra.git", "TOK")
    assert gl == "https://oauth2:TOK@gitlab.com"
    self_hosted = _git_credentials_line("https://git.acme.io:8443/x/y.git", "TOK")
    assert self_hosted == "https://git:TOK@git.acme.io:8443"


def test_git_secret_spec_carries_credentials_in_stringdata():
    sec = build_git_secret_spec(WORKSPACE_ID, "https://github.com/acme/x.git", "TOK")
    assert sec["kind"] == "Secret"
    assert sec["metadata"]["name"] == git_secret_name(WORKSPACE_ID)
    assert sec["metadata"]["namespace"] == WORKSPACE_NAMESPACE
    # stringData (not data) so K8s base64s it; line is the helper format.
    assert sec["stringData"]["credentials"].startswith("https://x-access-token:TOK@")


def test_bootstrap_script_never_embeds_token():
    # The clone URL stays credential-free — auth is via the credential
    # helper, so the PAT can't end up in /workspace/.git/config.
    script = _bootstrap_script("https://github.com/acme/x.git", "main")
    assert "TOK" not in script
    assert "git clone --branch main https://github.com/acme/x.git" in script


def test_deployment_unauthenticated_has_no_git_creds():
    dep = build_deployment_spec(
        workspace_id=WORKSPACE_ID,
        tenant_id="t",
        user_id="u",
        profile="small",
        model=None,
        git_repo_url="https://github.com/acme/x.git",
        git_branch="main",
        git_authenticated=False,
    )
    pod = dep["spec"]["template"]["spec"]
    assert all(v["name"] != "git-creds" for v in pod["volumes"])
    assert "env" not in pod["initContainers"][0]


def test_deployment_authenticated_mounts_git_secret_everywhere():
    dep = build_deployment_spec(
        workspace_id=WORKSPACE_ID,
        tenant_id="t",
        user_id="u",
        profile="small",
        model=None,
        git_repo_url="https://github.com/acme/x.git",
        git_branch="main",
        git_authenticated=True,
    )
    pod = dep["spec"]["template"]["spec"]
    # Secret-backed volume, referencing the per-workspace git Secret.
    vol = next(v for v in pod["volumes"] if v["name"] == "git-creds")
    assert vol["secret"]["secretName"] == git_secret_name(WORKSPACE_ID)
    # Mounted + credential-helper env on BOTH the init and code-server
    # containers so first-boot clone *and* terminal git both authenticate.
    init_c = pod["initContainers"][0]
    code_c = pod["containers"][0]
    for c in (init_c, code_c):
        assert any(m["name"] == "git-creds" for m in c["volumeMounts"])
        keys = {e["name"]: e["value"] for e in c["env"]}
        assert keys["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert keys["GIT_CONFIG_VALUE_0"].startswith("store --file=")
    # The code-server container keeps its original env (gateway/model) too.
    code_keys = {e["name"] for e in code_c["env"]}
    assert {"DAALU_GATEWAY_URL", "DAALU_MODEL"} <= code_keys

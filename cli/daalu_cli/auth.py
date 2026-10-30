"""`daalu login` / `logout` / `status`.

Two ways in, both ending at the same redeem call that returns a PAT:

* **device flow** (``daalu login``) — POST /cli/device/code, open the browser to
  approve, then poll /cli/device/token until it returns the token.
* **express** (``daalu login --code dvc_live_…``) — skip the browser; the code is
  already approved, so redeem it directly.

The redeemed PAT + the host + the default model are written to
``~/.daalu/credentials.json``.
"""

from __future__ import annotations

import socket
import sys
import time
import webbrowser

import httpx

from daalu_cli.config import (
    Credentials,
    clear_credentials,
    credentials_path,
    load_credentials,
    save_credentials,
)

_POLL_TIMEOUT_SECONDS = 600


def _client_name() -> str:
    try:
        host = socket.gethostname()
    except OSError:
        host = "unknown"
    return f"daalu-cli @ {host}"


def _post(host: str, path: str, json: dict) -> dict:
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(f"{host.rstrip('/')}{path}", json=json)
    resp.raise_for_status()
    return resp.json()


def login(host: str, *, code: str | None = None, open_browser: bool = True) -> int:
    """Run a login flow against ``host``. Returns a process exit code."""
    host = host.rstrip("/")
    if code:
        return _redeem_and_store(host, code, label="--code")

    # Device flow.
    try:
        start = _post(host, "/api/v1/cli/device/code", {"client_name": _client_name()})
    except httpx.HTTPError as e:
        print(f"could not reach {host}: {e}", file=sys.stderr)
        return 1

    user_code = start["user_code"]
    verify_complete = start["verification_uri_complete"]
    interval = int(start.get("interval", 5))

    print("\nTo finish signing in, open this URL in your browser:\n")
    print(f"    {verify_complete}\n")
    print(f"and confirm this code:  {user_code}\n")
    if open_browser:
        try:
            webbrowser.open(verify_complete)
        except Exception:  # pragma: no cover — headless / no browser
            pass

    print("Waiting for approval… (Ctrl-C to cancel)")
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    device_code = start["device_code"]
    while time.monotonic() < deadline:
        try:
            poll = _post(
                host, "/api/v1/cli/device/token", {"device_code": device_code}
            )
        except httpx.HTTPError as e:
            print(f"\nnetwork error while polling: {e}", file=sys.stderr)
            return 1
        status = poll.get("status")
        if status == "authorization_pending":
            time.sleep(interval)
            continue
        if status == "approved":
            return _store_from_redeem(host, poll)
        if status == "expired":
            print("\nThis login request expired. Run `daalu login` again.", file=sys.stderr)
            return 1
        # denied / unknown
        print("\nLogin was denied.", file=sys.stderr)
        return 1

    print("\nTimed out waiting for approval.", file=sys.stderr)
    return 1


def _redeem_and_store(host: str, code: str, *, label: str) -> int:
    try:
        poll = _post(host, "/api/v1/cli/device/token", {"device_code": code})
    except httpx.HTTPError as e:
        print(f"could not reach {host}: {e}", file=sys.stderr)
        return 1
    status = poll.get("status")
    if status == "approved":
        return _store_from_redeem(host, poll)
    if status == "expired":
        print(f"That {label} code has expired — generate a fresh one.", file=sys.stderr)
        return 1
    if status == "authorization_pending":
        print("That code hasn't been approved yet.", file=sys.stderr)
        return 1
    print("That code is not valid.", file=sys.stderr)
    return 1


def _store_from_redeem(host: str, poll: dict) -> int:
    token = poll.get("access_token")
    if not token:
        print("server did not return a token", file=sys.stderr)
        return 1
    # The redeem response echoes the canonical host (public_base_url); prefer it
    # so creds point at the externally-reachable URL even if --host was a vanity.
    canonical = (poll.get("host") or host).rstrip("/")
    creds = Credentials(host=canonical, token=token)
    creds = _attach_default_model(creds)
    path = save_credentials(creds)
    print(f"\n✓ Logged in to {canonical}")
    print(f"  Credentials saved to {path}")
    print("  Run `daalu` in a project directory to start coding.")
    return 0


def _attach_default_model(creds: Credentials) -> Credentials:
    """Best-effort: pre-fill the default model so `daalu` starts instantly."""
    from daalu_cli.client import ApiError, DaaluClient

    try:
        cfg = DaaluClient(creds.host, creds.token).coding_config()
    except (ApiError, httpx.HTTPError):
        return creds
    return Credentials(host=creds.host, token=creds.token, model=cfg.get("default_model"))


def logout() -> int:
    removed = clear_credentials()
    if removed:
        print("✓ Logged out — credentials removed.")
    else:
        print("Not logged in (no credentials found).")
    return 0


def status() -> int:
    """Print who we are + which instance/model, validating the token."""
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run `daalu login`.")
        return 1
    from daalu_cli.client import ApiError, DaaluClient

    client = DaaluClient(creds.host, creds.token)
    try:
        me = client.whoami()
    except ApiError as e:
        if e.status == 401:
            print(f"Logged-in host: {creds.host}")
            print("Token is no longer valid — run `daalu login` again.")
            return 1
        print(f"Could not verify session: {e}")
        return 1
    except httpx.HTTPError as e:
        print(f"Could not reach {creds.host}: {e}")
        return 1

    print(f"Host:   {creds.host}")
    print(f"User:   {me.get('email', '(unknown)')}")
    print(f"Model:  {creds.model or '(default)'}")
    print(f"Creds:  {credentials_path()}")
    return 0

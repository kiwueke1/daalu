# Vendored copy of src/daalu_automation/core/aiops/mcp_cli.py (the laptop
# CLI must not depend on the server package). tests/test_aiops_mcp_cli_sync.py
# fails if the two drift: edit the server copy, then re-copy it here.
"""``daalu mcp`` — MCP stdio transport bridged to Daalu's HTTP MCP endpoint.

Reads newline-delimited JSON-RPC messages from stdin, POSTs each to
``{url}/api/v1/mcp`` with ``Authorization: Bearer <token>``, and writes the
response (if any) to stdout as one line. Notifications get no output.
HTTP and network failures become JSON-RPC errors for the request that hit
them, so the MCP client sees a clean error instead of a hung call.

stdout carries protocol messages only: everything else (logs, stray prints
from imported modules) is pointed at stderr before the loop starts.
See docs/design/ai-ops-maturity.md §9 and docs/mcp.md.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import IO, Any

import httpx

log = logging.getLogger("daalu.mcp")

PROTOCOL_VERSION = "2025-06-18"
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INTERNAL_ERROR = -32603
# Implementation-defined server error range (-32000..-32099).
UPSTREAM_ERROR = -32000

_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)


def endpoint_for(url: str) -> str:
    base = url.rstrip("/")
    if base.endswith("/mcp"):
        return base
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    return base + "/mcp"


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """In case a server answers with an SSE stream: collect its data events."""
    out: list[dict[str, Any]] = []
    data: list[str] = []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip() and data:
            try:
                out.append(json.loads("\n".join(data)))
            except ValueError:
                pass
            data = []
    return out


class Bridge:
    def __init__(self, client: httpx.Client, endpoint: str, token: str) -> None:
        self.client = client
        self.endpoint = endpoint
        self.token = token
        self.protocol_version: str | None = None

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    def forward(self, message: Any) -> list[dict[str, Any]]:
        """Send one message upstream; return the messages to write back."""
        is_request = isinstance(message, dict) and "id" in message and "method" in message
        msg_id = message.get("id") if isinstance(message, dict) else None
        try:
            resp = self.client.post(self.endpoint, json=message, headers=self._headers())
        except httpx.HTTPError as e:
            log.error("daalu mcp: cannot reach %s: %s", self.endpoint, e)
            return [_error(msg_id, UPSTREAM_ERROR,
                           f"cannot reach Daalu at {self.endpoint}: {type(e).__name__}: {e}")
                    ] if is_request else []

        if resp.status_code == 202 or not resp.content:
            if resp.status_code >= 400 and is_request:
                return [_error(msg_id, UPSTREAM_ERROR, f"Daalu returned HTTP {resp.status_code}")]
            return []

        ctype = resp.headers.get("content-type", "")
        replies: list[dict[str, Any]] = []
        if "text/event-stream" in ctype:
            replies = _parse_sse(resp.text)
        else:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("jsonrpc") == "2.0":
                replies = [body]
            elif resp.status_code >= 400:
                detail = body.get("detail") if isinstance(body, dict) else resp.text[:500]
                hint = ""
                if resp.status_code == 401:
                    hint = " (check the token: a personal access token dpat_… from " \
                           "Settings → API tokens)"
                log.error("daalu mcp: HTTP %s from %s: %s", resp.status_code,
                          self.endpoint, detail)
                return [_error(msg_id, UPSTREAM_ERROR,
                               f"Daalu returned HTTP {resp.status_code}: {detail}{hint}",
                               {"status": resp.status_code})] if is_request else []
            else:
                return [_error(msg_id, INTERNAL_ERROR,
                               "Daalu returned a non-JSON-RPC response")] if is_request else []

        for r in replies:
            if (isinstance(message, dict) and message.get("method") == "initialize"
                    and isinstance(r.get("result"), dict)):
                self.protocol_version = r["result"].get("protocolVersion") or PROTOCOL_VERSION
        return replies

    def handle_line(self, line: str) -> list[dict[str, Any]]:
        line = line.strip()
        if not line:
            return []
        try:
            message = json.loads(line)
        except ValueError:
            return [_error(None, PARSE_ERROR, "parse error")]
        if isinstance(message, list):
            out: list[dict[str, Any]] = []
            for m in message:  # older clients may batch; answer one by one
                out.extend(self.forward(m))
            return out
        if not isinstance(message, dict):
            return [_error(None, INVALID_REQUEST, "expected a JSON-RPC object")]
        return self.forward(message)


def serve(stdin: IO[str], stdout: IO[str], bridge: Bridge) -> None:
    for line in stdin:
        for reply in bridge.handle_line(line):
            stdout.write(json.dumps(reply, separators=(",", ":"), ensure_ascii=False) + "\n")
            stdout.flush()


def run_stdio(url: str, token: str) -> None:
    protocol_out = sys.stdout
    # Anything else that writes to stdout would corrupt the protocol stream.
    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        import structlog

        structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    except Exception:  # noqa: BLE001
        pass
    if not token:
        raise SystemExit("daalu mcp: a token is required (--token or DAALU_TOKEN)")
    endpoint = endpoint_for(url)
    log.info("daalu mcp: bridging stdio to %s", endpoint)
    with httpx.Client(timeout=_TIMEOUT) as client:
        try:
            serve(sys.stdin, protocol_out, Bridge(client, endpoint, token))
        except KeyboardInterrupt:
            pass

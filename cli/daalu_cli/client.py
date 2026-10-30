"""HTTP client for the Daalu public API.

Thin wrapper over httpx. Three calls the CLI needs:

* ``whoami``          — GET  /api/v1/auth/me           (validate the token)
* ``coding_config``   — GET  /api/v1/coding/config     (models + endpoint)
* ``chat_completion`` — POST /api/v1/coding/v1/chat/completions

All authenticated with ``Authorization: Bearer <pat>``.
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """Raised for non-2xx API responses, carrying the status + a short detail."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class DaaluClient:
    def __init__(self, host: str, token: str, *, timeout: float = 120.0) -> None:
        self.host = host.rstrip("/")
        self._headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        }
        self._timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.host}{path}"

    def whoami(self) -> dict[str, Any]:
        return self._get("/api/v1/auth/me")

    def coding_config(self) -> dict[str, Any]:
        return self._get("/api/v1/coding/config")

    def chat_completion(self, body: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                self._url("/api/v1/coding/v1/chat/completions"),
                headers=self._headers,
                json=body,
            )
        return self._parse(resp)

    # ── internals ──────────────────────────────────────────────────────────

    def _get(self, path: str) -> dict[str, Any]:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(self._url(path), headers=self._headers)
        return self._parse(resp)

    @staticmethod
    def _parse(resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code >= 400:
            detail = _detail(resp)
            raise ApiError(resp.status_code, detail)
        try:
            return resp.json()
        except ValueError as e:  # pragma: no cover — upstream should be JSON
            raise ApiError(resp.status_code, "non-JSON response") from e


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except ValueError:
        pass
    return (resp.text or resp.reason_phrase)[:200]

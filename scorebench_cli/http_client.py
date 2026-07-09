from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Cap how much of a response body we buffer so a hostile/misbehaving server or proxy
# cannot OOM the client with a multi-gigabyte body.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def _read_capped(reader: Any) -> str:
    body = reader.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError(f"response body exceeds {MAX_RESPONSE_BYTES} bytes")
    return body.decode("utf-8")


class HarnessClient:
    def __init__(
        self,
        base_url: str,
        arm_token: str | None = None,
        admin_token: str | None = None,
        admin_session: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.arm_token = arm_token
        self.admin_token = admin_token
        self.admin_session = admin_session

    def _headers(self, admin: bool = False) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if admin:
            if self.admin_session:
                headers["X-Harness-Admin-Session"] = self.admin_session
            elif self.admin_token:
                headers["X-Harness-Admin-Token"] = self.admin_token
        elif self.arm_token:
            headers["Authorization"] = f"Bearer {self.arm_token}"
        return headers

    def get(self, path: str, admin: bool = False, **params) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        return self._request("GET", f"{self.base_url}{path}{query}", None, self._headers(admin=admin))

    def post(self, path: str, payload: dict, admin: bool = False) -> Any:
        data = json.dumps(payload).encode("utf-8")
        return self._request("POST", f"{self.base_url}{path}", data, self._headers(admin=admin))

    @staticmethod
    def _request(method: str, url: str, data: bytes | None, headers: dict[str, str]):
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=3600) as response:
                body = _read_capped(response)
                if not body:
                    return None
                try:
                    return json.loads(body)
                except ValueError as exc:
                    raise RuntimeError(f"expected JSON response but got: {body[:200]}") from exc
        except HTTPError as exc:
            body = _read_capped(exc)
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {"error": body}
            raise RuntimeError(f"HTTP {exc.code}: {payload.get('error', payload)}") from exc

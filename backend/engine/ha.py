from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from typing import Any


class HAError(RuntimeError):
    pass


class HA:
    """The smallest useful Home Assistant REST client.

    The token is read from ``HA_TOKEN`` first, then from a configured command.
    The default command is the same keyring entry the Omarchy hass plugin uses,
    so Voice Assistant does not need its own copy of the secret.
    """

    def __init__(
        self,
        url: str,
        token: str | None = None,
        token_command: list[str] | None = None,
        timeout: float = 10.0,
    ):
        self.url = (url or "http://127.0.0.1:8123").rstrip("/")
        self._token = token or os.environ.get("HA_TOKEN")
        self.token_command = token_command or []
        self.timeout = timeout

    def token(self) -> str:
        if self._token:
            return self._token
        if self.token_command:
            try:
                proc = subprocess.run(
                    self.token_command,
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=True,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller
                raise HAError(f"could not read HA token: {exc}") from exc
            value = (proc.stdout or "").strip()
            if value:
                self._token = value
                return value
        raise HAError(
            "no Home Assistant token; set HA_TOKEN or configure ha.token_command"
        )

    def request(self, method: str, path: str, data: Any = None) -> Any:
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(
            f"{self.url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token()}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise HAError(f"HA {method} {path} -> {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            raise HAError(f"HA {method} {path} failed: {exc}") from exc

    def get_state(self, entity_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/states/{entity_id}")

    def call_service(self, service: str, data: dict[str, Any] | None = None) -> Any:
        if "." not in service:
            raise HAError(f"service must be domain.name, got {service!r}")
        domain, name = service.split(".", 1)
        return self.request("POST", f"/api/services/{domain}/{name}", data or {})

    def set_state(
        self,
        entity_id: str,
        state: str,
        attributes: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"state": state}
        if attributes:
            payload["attributes"] = attributes
        return self.request("POST", f"/api/states/{entity_id}", payload)

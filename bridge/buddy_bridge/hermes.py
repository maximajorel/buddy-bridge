"""Cliente HTTP del API server de Hermes.

Todo lo que sale del bridge hacia el agente pasa por acá: el bearer
(`API_SERVER_KEY`) se agrega en un solo lugar y nunca se imprime. Errores
explícitos: quien llama decide qué devolver, pero nunca se traga la excepción.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterator


class HermesError(Exception):
    """El API server contestó con error o no se pudo alcanzar."""


class HermesClient:
    def __init__(self, base_url: str, api_key: str, default_timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.default_timeout = default_timeout

    # ------------------------------------------------------------------ interno
    def _request(self, method: str, path: str, body=None, headers=None,
                 timeout=None) -> urllib.request.urlopen:
        url = self.base_url + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if self.api_key:
            req.add_header("Authorization", "Bearer " + self.api_key)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            return urllib.request.urlopen(
                req, timeout=self.default_timeout if timeout is None else timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:300]
            except Exception:  # noqa: BLE001 - el cuerpo del error es opcional
                pass
            raise HermesError(f"{method} {path} -> HTTP {exc.code} {detail}") from exc
        except Exception as exc:  # noqa: BLE001 - URLError, timeout, DNS
            raise HermesError(f"{method} {path} -> {exc}") from exc

    def get_json(self, path: str, timeout: float | None = 6):
        with self._request("GET", path, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    # ------------------------------------------------------------------- API
    def active_runs(self) -> int:
        """Runs en curso del perfil default, según el health detallado. -1 = no se pudo."""
        try:
            d = self.get_json("/health/detailed")
            return int(d["readiness"]["checks"]["background_queues"].get("active_api_runs", 0))
        except Exception:  # noqa: BLE001 - degradar a "-1 desconocido" es el contrato
            return -1

    def create_run(self, prompt: str, session_id: str, button: str | None = None) -> dict:
        body = {"input": prompt, "session_id": session_id}
        if button:
            body["metadata"] = {"buddy_button": button}
        with self._request("POST", "/v1/runs", body,
                           headers={"X-Hermes-Session-Id": session_id}) as resp:
            return json.loads(resp.read().decode())

    def stream_run(self, run_id: str) -> Iterator[dict]:
        """Itera el SSE del run. Reintenta con `Last-Event-ID` mientras haya cuerpo."""
        last_seq = -1
        while True:
            headers = {"Last-Event-ID": str(last_seq)} if last_seq >= 0 else {}
            with self._request("GET", f"/v1/runs/{run_id}/events", headers=headers,
                               timeout=None) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ev.get("seq"), int):
                        last_seq = ev["seq"]
                    yield ev
            return

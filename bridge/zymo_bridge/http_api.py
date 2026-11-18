"""El contrato HTTP que consume la placa.

    GET  /health      -> liveness, sin auth (no gasta el token)
    GET  /state       -> la cara, listo para dibujar
    GET  /events      -> long-poll de eventos (cursor `since`)
    POST /buddy/ask   -> crea un run; pide el token de ask, no el de lectura

La auth vive acá, en el transporte, y no dentro de cada handler: el día que se
agregue un endpoint no hay forma de olvidarse del chequeo si el chequeo es la
primera línea. Tras 5 intentos con token inválido desde la misma IP se corta por
`BUDDY_AUTH_COOLDOWN` segundos, porque un token de 24 bytes se fuerza por
diccionario y no por fuerza bruta pero sí por insistencia.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import Config
from .hermes import HermesError
from .service import BridgeService

SERVICE_NAME = "hermes-buddy-bridge"
MAX_BODY = 8 * 1024
AUTH_FAIL_LIMIT = 5
AUTH_COOLDOWN_SECONDS = 60.0


class AuthGuard:
    """Tokens por permiso y bloqueo temporal por IP tras varios fallos."""

    def __init__(self, read_token: str, ask_token: str, limit: int = AUTH_FAIL_LIMIT,
                 cooldown: float = AUTH_COOLDOWN_SECONDS, clock=time.time):
        self.read_token = read_token or ""
        self.ask_token = ask_token or ""
        self.limit = limit
        self.cooldown = cooldown
        self.clock = clock
        self._fails: dict[str, list] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- token
    def _sent_token(self, headers) -> str:
        auth = headers.get("Authorization", "") or ""
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip()
        return (headers.get("X-Buddy-Token", "") or "").strip()

    @staticmethod
    def _match(sent: str, expected: str) -> bool:
        # Comparación en tiempo constante suficiente: longitudes fijas por token.
        if not expected or len(sent) != len(expected):
            return False
        diff = 0
        for a, b in zip(sent, expected):
            diff |= ord(a) ^ ord(b)
        return diff == 0

    def allows(self, headers, need_ask: bool = False) -> bool:
        """Sin token configurado no hay auth (uso local); si no, solo ese permiso."""
        if need_ask:
            # `ask` exige su propio token: que falte uno de lectura no lo habilita.
            return bool(self.ask_token) and self._match(self._sent_token(headers), self.ask_token)
        if not self.read_token:
            return True
        return self._match(self._sent_token(headers), self.read_token)

    # --------------------------------------------------------------- bloqueo
    def blocked(self, ip: str) -> bool:
        with self._lock:
            fails = self._fails.get(ip)
            if not fails:
                return False
            fails[:] = [t for t in fails if self.clock() - t < self.cooldown]
            if not fails:
                self._fails.pop(ip, None)
                return False
            return len(fails) >= self.limit

    def note_failure(self, ip: str) -> None:
        with self._lock:
            self._fails.setdefault(ip, []).append(self.clock())

    def note_success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


def make_handler(service: BridgeService, config: Config):
    guard = service.auth_guard = getattr(service, "auth_guard", None) or AuthGuard(
        config.buddy_token, config.ask_token)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "zymo-bridge/" + __version__

        # ---------------------------------------------------------- helpers
        def log_message(self, fmt, *args):
            print(f"[bridge] {self.address_string()} {fmt % args}", flush=True)

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _ip(self) -> str:
            return self.client_address[0] if self.client_address else "?"

        def _deny(self, code: int, error: str):
            self._send(code, {"error": error})

        def _guard(self, need_ask: bool = False) -> bool:
            """Devuelve True si hay que cortar la respuesta acá."""
            ip = self._ip()
            if guard.blocked(ip):
                self._deny(429, "too many auth failures")
                return True
            if guard.allows(self.headers, need_ask=need_ask):
                guard.note_success(ip)
                return False
            guard.note_failure(ip)
            self._deny(401, "unauthorized")
            return True

        # ------------------------------------------------------------- GET
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/health":
                return self._send(200, {"status": "ok", "service": SERVICE_NAME,
                                        "version": __version__})
            if path == "/state":
                if self._guard():
                    return
                return self._send(200, service.face.snapshot())
            if path == "/events":
                if self._guard():
                    return
                q = parse_qs(parsed.query)
                try:
                    since = int(q.get("since", ["0"])[0])
                except ValueError:
                    return self._deny(400, "since must be int")
                try:
                    wait = float(q.get("wait", [str(config.events_wait_max)])[0])
                except ValueError:
                    return self._deny(400, "wait must be number")
                wait = max(0.0, min(wait, config.events_wait_max))
                return self._send(200, service.face.events.since_request(since, wait))
            return self._deny(404, "not found")

        # ------------------------------------------------------------ POST
        def do_POST(self):
            if urlparse(self.path).path != "/buddy/ask":
                return self._deny(404, "not found")
            if not config.ask_enabled:
                return self._deny(403, "ask disabled (BUDDY_ASK_TOKEN not set)")
            if self._guard(need_ask=True):
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._deny(400, "invalid content-length")
            if length < 0 or length > MAX_BODY:
                return self._deny(413, "body too large")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._deny(400, "invalid json")
            if not isinstance(body, dict):
                return self._deny(400, "body must be an object")
            prompt = (body.get("prompt") or body.get("question") or "").strip()
            button = body.get("button") or None
            if button not in (None, "left", "center", "right"):
                return self._deny(400, "button must be left|center|right")
            if not prompt:
                return self._deny(400, "prompt required")
            if len(prompt) > 2000:
                return self._deny(413, "prompt too long")
            try:
                return self._send(202, service.ask(prompt, button))
            except HermesError as exc:
                return self._send(502, {"error": "hermes", "detail": str(exc)[:300]})
            except Exception as exc:  # noqa: BLE001 - el contrato es siempre JSON
                return self._send(502, {"error": str(exc)})

    return Handler


def serve(service: BridgeService, config: Config, address=None) -> ThreadingHTTPServer:
    """Levanta el server. `address` permite forzar host/puerto (los tests usan
    ('127.0.0.1', 0) para tomar un puerto efímero y no chocar con el servicio vivo)."""
    host, port = address if address else (config.bind, config.port)
    server = ThreadingHTTPServer((host, port), make_handler(service, config))
    server.daemon_threads = True
    return server

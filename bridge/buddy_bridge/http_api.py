"""El contrato HTTP que consume la placa.

    GET  /health      -> liveness, sin auth (no gasta el token)
    GET  /state       -> la cara, listo para dibujar
    GET  /events      -> long-poll de eventos (cursor `since`)
    GET  /calendar    -> estado del calendario + redirect del OAuth
    GET  /calendar/oauth/callback -> redirect público de Google
    POST /buddy/ask   -> crea un run; pide el token de ask, no el de lectura
    POST /calendar/oauth/start  -> arranca el OAuth de Google (token `ask`)

La auth vive acá, en el transporte, y no dentro de cada handler: el día que se
agregue un endpoint no hay forma de olvidarse del chequeo si el chequeo es la
primera línea. Tras 5 intentos con token inválido desde la misma IP se corta por
`BUDDY_AUTH_COOLDOWN` segundos, porque un token de 24 bytes se fuerza por
diccionario y no por fuerza bruta pero sí por insistencia.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from . import __version__
from . import calendar_config
from .config import Config
from .hermes import HermesError
from .service import BridgeService

SERVICE_NAME = "hermes-buddy-bridge"
MAX_BODY = 8 * 1024
AUTH_FAIL_LIMIT = 5
AUTH_COOLDOWN_SECONDS = 60.0

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
DEFAULT_PUBLIC_URL = "https://buddy.maximajorel.com.ar"
OAUTH_STATE_TTL = 600.0
OAUTH_HTTP_TIMEOUT = 15.0


class OAuthExchangeError(Exception):
    """El canje del `code` contra Google falló. No lleva secretos en el mensaje."""


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


class OAuthStateStore:
    """Estados de OAuth en memoria: un solo uso, TTL corto y lock propio.

    El `state` es la única defensa del callback público. Se consume con `pop`
    ANTES de canjear el code: si el canje falla, ese enlace ya no sirve. Un
    `dict` sin lock en un `ThreadingHTTPServer` es una carrera esperando.
    """

    def __init__(self, ttl: float = OAUTH_STATE_TTL, clock=time.time):
        self.ttl = ttl
        self.clock = clock
        self._states: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _prune_locked(self, now: float) -> None:
        expired = [k for k, v in self._states.items()
                   if now - float(v.get("created", 0)) > self.ttl]
        for key in expired:
            self._states.pop(key, None)

    def put(self, state: str, entry: dict) -> None:
        with self._lock:
            now = self.clock()
            self._prune_locked(now)
            entry = dict(entry)
            entry["created"] = now
            self._states[state] = entry

    def pop(self, state: str):
        with self._lock:
            self._prune_locked(self.clock())
            return self._states.pop(state, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._states)


def oauth_redirect_uri(config: Config) -> str:
    """Callback público registrado en Google, sin barra final."""
    base = (getattr(config, "public_url", "") or DEFAULT_PUBLIC_URL).rstrip("/")
    return base + "/calendar/oauth/callback"


def _make_pkce() -> tuple[str, str]:
    """PKCE S256: verifier aleatorio y challenge base64url sin padding."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _google_exchange_code(client_id: str, client_secret: str, code: str,
                          verifier: str, redirect_uri: str) -> dict:
    """Canjea el `code` en el token endpoint de Google.

    Aislado a propósito: los tests lo mockean y NINGUNO sale a internet. El
    mensaje de error solo lleva el código HTTP y el `error` de Google, nunca
    credenciales.
    """
    data = urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
    }).encode("ascii")
    request = urllib.request.Request(
        GOOGLE_TOKEN_URL, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=OAUTH_HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8") or "{}").get("error", "")
        except (ValueError, OSError):
            pass
        suffix = f": {detail}" if detail else ""
        raise OAuthExchangeError(f"Google rechazó el canje (HTTP {exc.code}{suffix})") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise OAuthExchangeError(f"no se pudo hablar con Google: {exc}") from None
    return payload if isinstance(payload, dict) else {}


def _oauth_page(title: str, message: str) -> str:
    """HTML mínimo del callback: sin datos, sin JS, en español."""
    return ("<!doctype html>\n<html lang=\"es\">\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
            f"<title>{title}</title>\n"
            "<body style=\"font-family:system-ui,sans-serif;max-width:34rem;"
            "margin:3rem auto;padding:0 1rem\">\n"
            f"<h1>{title}</h1>\n<p>{message}</p>\n</body>\n</html>\n")


def make_handler(service: BridgeService, config: Config):
    # Se rearma con CADA server: cachear el guard en el service dejaba pegados los
    # tokens viejos, y tras un cambio de `.env` el bridge seguía pidiendo los de
    # antes hasta reiniciarse.
    guard = service.auth_guard = AuthGuard(config.buddy_token, config.ask_token)
    states = service.oauth_states = OAuthStateStore()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "buddy-bridge/" + __version__

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
            if path == "/calendar":
                # Solo lee estado y nunca devuelve secretos: el token de lectura
                # alcanza. Escribir (POST) sí exige el `ask`.
                if self._guard():
                    return
                return self._send(200, self._calendar_status())
            if path == "/calendar_page":
                # Público a propósito: esta página la abre el dueño desde un link del
                # panel del buddy, y un <a target="_blank"> no puede mandar headers.
                # Es seguro mientras ADMIN_HTML siga sin un solo dato: es un formulario vacío.
                # Los datos van por GET /calendar y la escritura por POST /calendar, con token.
                return self._send_html(200, calendar_config.ADMIN_HTML)
            if path == "/calendar/oauth/callback":
                # Público a propósito: el redirect de Google no manda headers.
                # Su única defensa es el `state`: un solo uso y TTL de 10 minutos.
                return self._oauth_callback(parsed)
            if path == "/calendar/help":
                if self._guard():
                    return
                return self._send(200, calendar_config.help_payload())
            return self._deny(404, "not found")

        # ------------------------------------------------------- calendario
        def _calendar_status(self) -> dict:
            # Se lee del ARCHIVO y no de `config`: después de guardar, el `Config`
            # en memoria puede ser el viejo. El `.env` es la fuente de verdad.
            env = calendar_config.current_values(
                calendar_config.read_env_map(Path(config.env_file)))
            status = calendar_config.public_status(env)
            status["oauth_redirect"] = oauth_redirect_uri(config)
            event = service.face.calendar_event()
            status["active"] = service.face.calendar_active()
            status["next"] = None
            if event is not None and status["active"]:
                status["next"] = {"summary": event.summary,
                                  "in_min": int(round((event.start - time.time()) / 60.0))}
            return status

        def _send_html(self, code: int, text: str):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # ------------------------------------------------------------ POST
        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/calendar":
                return self._save_calendar()
            if path == "/calendar/oauth/start":
                return self._oauth_start()
            if path != "/buddy/ask":
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

        # ---------------------------------------------------------- OAuth Google
        def _oauth_start(self):
            """POST /calendar/oauth/start: PKCE + state, devuelve la URL de Google."""
            if self._guard(need_ask=True):
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._deny(400, "invalid content-length")
            if length < 0 or length > MAX_BODY:
                return self._deny(413, "body too large")
            body = {}
            if length:
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    return self._deny(400, "invalid json")
                if not isinstance(body, dict):
                    return self._deny(400, "body must be an object")

            env_file = Path(config.env_file)
            env = calendar_config.read_env_map(env_file)
            try:
                client_id = calendar_config.clean_value(
                    body.get("client_id") or env.get("GOOGLE_CLIENT_ID") or "")
                client_secret = calendar_config.clean_value(
                    body.get("client_secret") or env.get("GOOGLE_CLIENT_SECRET") or "")
            except calendar_config.ConfigWriteError as exc:
                return self._send(400, {"error": str(exc)})
            if not client_id:
                return self._send(400, {"error": "falta client_id: pegalo en el panel "
                                                "o guardalo antes en el .env"})
            if not client_secret:
                return self._send(400, {"error": "falta client_secret para ese client_id"})
            # Las credenciales viajan en el `state` y se escriben recién cuando el
            # callback trae el refresh token: el `.env` nunca queda con dos de tres,
            # que es justo el estado a medias que `calendar_config` prohíbe.

            verifier, challenge = _make_pkce()
            state = secrets.token_urlsafe(32)
            states.put(state, {"verifier": verifier, "client_id": client_id,
                               "client_secret": client_secret, "created": time.time()})
            params = {
                "client_id": client_id,
                "redirect_uri": oauth_redirect_uri(config),
                "response_type": "code",
                "scope": CALENDAR_SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
            # El secret y el verifier NUNCA salen en la respuesta.
            return self._send(200, {"authorize_url": GOOGLE_AUTH_URL + "?" + urlencode(params)})

        def _oauth_callback(self, parsed):
            """GET /calendar/oauth/callback: público, defendido por el `state`."""
            query = parse_qs(parsed.query)
            state = (query.get("state") or [""])[0]
            code = (query.get("code") or [""])[0]
            error = (query.get("error") or [""])[0]
            if error or not state or not code:
                return self._send_html(400, _oauth_page(
                    "No se pudo conectar",
                    "Google devolvió un error o faltan datos. Volvé a intentar desde el panel."))
            # Consumir ANTES de canjear: aunque el canje falle, el state ya no sirve.
            entry = states.pop(state)
            if entry is None:
                return self._send_html(400, _oauth_page(
                    "Enlace vencido",
                    "Este enlace ya se usó o pasó demasiado tiempo. "
                    "Volvé a empezar desde el panel."))
            try:
                tokens = _google_exchange_code(
                    entry["client_id"], entry["client_secret"], code,
                    entry["verifier"], oauth_redirect_uri(config))
            except Exception as exc:  # noqa: BLE001 - el callback siempre es HTML
                print(f"[bridge] oauth: canje falló: {exc}", flush=True)
                return self._send_html(400, _oauth_page(
                    "No se pudo conectar",
                    "Google rechazó el canje del código. Volvé a empezar desde el panel."))
            refresh = (tokens.get("refresh_token") or "").strip()
            if not refresh:
                # No se escribe nada: un .env con dos de tres vuelve a fallar cada minuto.
                return self._send_html(400, _oauth_page(
                    "Falta el refresh token",
                    "Google no devolvió un refresh token. Revocá el acceso del buddy en tu "
                    "cuenta de Google y volvé a empezar desde el panel."))

            env_file = Path(config.env_file)
            current = calendar_config.current_values(calendar_config.read_env_map(env_file))
            values = {
                "GOOGLE_CLIENT_ID": entry["client_id"],
                "GOOGLE_CLIENT_SECRET": entry["client_secret"],
                "GOOGLE_REFRESH_TOKEN": refresh,
            }
            try:
                # Un solo store: la MISMA función que POST /calendar.
                calendar_config.write_env(env_file, calendar_config.validate(values, current))
            except calendar_config.ConfigWriteError as exc:
                return self._send_html(400, _oauth_page("No se pudo guardar", str(exc)))
            except OSError:
                return self._send_html(500, _oauth_page(
                    "No se pudo guardar", "El bridge no pudo escribir el .env."))
            try:
                fresh = Config.load(config.env_file)
            except Exception:  # noqa: BLE001 - config ilegible tras escribir
                return self._send_html(500, _oauth_page(
                    "Configuración inválida",
                    "El calendario se guardó pero el .env no se pudo releer. Reiniciá el bridge."))
            service.reload_calendar(fresh)
            return self._send_html(200, _oauth_page(
                "Calendario conectado", "Calendario conectado, podés cerrar esta pestaña."))

        def _save_calendar(self):
            """POST /calendar: guardar, validar y aplicar sin reiniciar."""
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
            # `clear` borra las credenciales: apagar el calendario tiene que ser
            # tan fácil como prenderlo, sin editar el `.env` a mano por SSH.
            if body.get("clear"):
                try:
                    calendar_config.clear_env(Path(config.env_file))
                except OSError as exc:
                    return self._send(500, {"error": f"no se pudo escribir el .env: {exc}"})
                return self._apply_calendar_values({})

            env_file = Path(config.env_file)
            current = calendar_config.current_values(
                calendar_config.read_env_map(env_file))
            try:
                values = calendar_config.validate(body, current)
                calendar_config.write_env(env_file, values)
            except calendar_config.ConfigWriteError as exc:
                return self._send(400, {"error": str(exc)})
            except OSError as exc:
                return self._send(500, {"error": f"no se pudo escribir el .env: {exc}"})
            return self._apply_calendar_values(values)

        def _apply_calendar_values(self, values: dict) -> None:
            """Recarga el `.env` en caliente y devuelve el estado nuevo.

            Se rearma el `Config` desde el archivo en vez de mutar el que existe:
            una sola fuente de verdad y el próximo restart lee exactamente lo
            mismo que quedó en memoria.
            """
            try:
                fresh = Config.load(config.env_file)
            except Exception as exc:  # noqa: BLE001 - config ilegible tras escribir
                return self._send(500, {"error": f"el .env quedó ilegible: {exc}",
                                        "restored": False})
            service.reload_calendar(fresh)
            env = calendar_config.current_values(calendar_config.read_env_map(
                Path(config.env_file)))
            status = calendar_config.public_status(env)
            return self._send(200, status)

    return Handler


def serve(service: BridgeService, config: Config, address=None) -> ThreadingHTTPServer:
    """Levanta el server. `address` permite forzar host/puerto (los tests usan
    ('127.0.0.1', 0) para tomar un puerto efímero y no chocar con el servicio vivo)."""
    host, port = address if address else (config.bind, config.port)
    server = ThreadingHTTPServer((host, port), make_handler(service, config))
    server.daemon_threads = True
    return server

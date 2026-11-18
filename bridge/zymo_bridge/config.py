"""Configuración del bridge: una sola puerta a las variables de entorno.

Precedencia: **entorno del sistema > `.env` > default**. Así un valor que systemd
ya tiene cargado siempre gana (nunca queda tapado por un archivo viejo), y el
`.env` es la fuente que se edita a mano en el servidor.

El `.env` se parsea con stdlib a propósito: el LXC no tiene `python-dotenv` y no
queremos que el bridge dependa de pip (el servicio arranca antes de cualquier
virtualenv). Se aceptan las formas usuales:

    CLAVE=valor
    CLAVE="valor con espacios"     # se sacan las comillas
    CLAVE=valor      # comentario al final: se recorta
    export CLAVE=valor
    # comentario / línea vacía

Un valor ilegible NO arranca el servicio a medias: `Config.load()` junta los
errores y los reporta juntos, porque un bridge con la mitad de la config es peor
que un servicio que no levanta.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class ConfigError(Exception):
    """Una o más variables no se pudieron leer o validar."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _strip_comment(value: str) -> str:
    """Saca un comentario al final de la línea, respetando comillas y `#` internos."""
    quote = None
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1] in " \t"):
            return value[:i].rstrip()
    return value.strip()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env_text(text: str) -> dict:
    """Parsea un `.env` a dict. No toca `os.environ` (eso lo decide `load`)."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        out[key] = _unquote(_strip_comment(value.strip()))
    return out


def read_env_file(path: Path) -> dict:
    """Lee el `.env`. Si no existe devuelve vacío: el entorno del sistema alcanza."""
    path = Path(path)
    if not path.exists():
        return {}
    return parse_env_text(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Config:
    """Configuración ya validada. Inmutable: se arma una vez al arrancar."""

    hermes_url: str = "http://127.0.0.1:8642"
    hermes_key: str = ""
    buddy_token: str = ""
    buddy_session: str = "esp32-buddy"
    bind: str = "0.0.0.0"
    port: int = 8787
    poll_seconds: float = 3.0
    done_ttl: float = 8.0
    sleep_hint_since: float = 0.0
    events_wait_max: float = 25.0
    events_buffer: int = 64
    tool_window: float = 12.0
    turn_grace: float = 2.5
    state_db: Path = Path("/opt/data/state.db")
    env_file: Path = DEFAULT_ENV_FILE

    # Tokens de auth que la placa puede mandar, en orden de preferencia. `ask_token`
    # vacío = /buddy/ask deshabilitado (el de solo lectura no alcanza para eso).
    ask_token: str = ""

    sources: dict = field(default_factory=dict, compare=False)

    # ------------------------------------------------------------------ carga
    @classmethod
    def load(cls, env_file: Path | None = None, environ: dict | None = None) -> "Config":
        env_file = Path(env_file) if env_file else DEFAULT_ENV_FILE
        environ = dict(os.environ if environ is None else environ)
        file_values = read_env_file(env_file)

        def get(name, default=None):
            if name in environ and environ[name] != "":
                return environ[name]
            if name in file_values and file_values[name] != "":
                return file_values[name]
            return default

        errors = []

        def number(name, default, cast):
            raw = get(name)
            if raw is None:
                return default
            try:
                value = cast(raw)
            except (TypeError, ValueError):
                errors.append(f"{name}={raw!r} no es un número válido")
                return default
            if value < 0:
                errors.append(f"{name}={raw!r} no puede ser negativo")
                return default
            return value

        hermes_url = (get("HERMES_URL") or "http://127.0.0.1:8642").strip().rstrip("/")
        if not hermes_url.startswith(("http://", "https://")):
            errors.append(f"HERMES_URL={hermes_url!r} debe empezar con http:// o https://")

        read_token = (get("BUDDY_TOKEN") or "").strip()
        ask_token = (get("BUDDY_ASK_TOKEN") or "").strip()
        if ask_token and ask_token == read_token:
            # Un solo token para las dos cosas no es un fallback: es el mismo
            # permiso repartido. Si querés ask, va un token distinto.
            errors.append("BUDDY_ASK_TOKEN no puede ser igual a BUDDY_TOKEN")

        port = number("BUDDY_PORT", 8787, int)
        if not (1 <= port <= 65535):
            errors.append(f"BUDDY_PORT={port} fuera de rango (1-65535)")

        state_db = Path(get("BUDDY_STATE_DB") or "/opt/data/state.db")

        if errors:
            raise ConfigError(errors)

        cfg = cls(
            hermes_url=hermes_url,
            hermes_key=(get("API_SERVER_KEY") or "").strip(),
            buddy_token=read_token,
            ask_token=ask_token,
            buddy_session=(get("BUDDY_SESSION") or "esp32-buddy").strip(),
            bind=(get("BUDDY_BIND") or "0.0.0.0").strip(),
            port=port,
            poll_seconds=number("BUDDY_POLL_SECONDS", 3.0, float),
            done_ttl=number("BUDDY_DONE_TTL", 8.0, float),
            sleep_hint_since=number("BUDDY_SLEEP_HINT_SINCE", 0.0, float),
            events_wait_max=number("BUDDY_EVENTS_WAIT_MAX", 25.0, float),
            events_buffer=number("BUDDY_EVENTS_BUFFER", 64, int),
            tool_window=number("BUDDY_TOOL_WINDOW", 12.0, float),
            turn_grace=number("BUDDY_TURN_GRACE", 2.5, float),
            state_db=state_db,
            env_file=env_file,
            sources={k: ("entorno" if k in environ and environ[k] != "" else "archivo")
                     for k in ("HERMES_URL", "API_SERVER_KEY", "BUDDY_TOKEN") if get(k)},
        )
        return cfg

    # ------------------------------------------------------------------ ayudas
    @property
    def ask_enabled(self) -> bool:
        return bool(self.ask_token)

    def redacted(self) -> dict:
        """Config para logs: los secretos se muestran como presencia, nunca valor."""
        def mask(v):
            return "<oculto>" if v else "<vacío>"
        return {
            "hermes_url": self.hermes_url,
            "api_server_key": mask(self.hermes_key),
            "buddy_token": mask(self.buddy_token),
            "buddy_ask_token": mask(self.ask_token),
            "buddy_session": self.buddy_session,
            "bind": self.bind,
            "port": self.port,
            "state_db": str(self.state_db),
            "env_file": str(self.env_file),
        }

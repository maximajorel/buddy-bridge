"""Google Calendar como fuente de la cara: sin IA, sin pip, sin tocar la placa.

Por qué existe este módulo y no un cron de Hermes: el buddy tiene que avisar
aunque Hermes esté apagado. Un cron añade dos piezas (el job y el archivo
intermedio) y un punto de falla más; leer el calendario desde el bridge es una
sola pieza con el mismo token OAuth que ya usa el CLI de calendario.

El costo es cero: son dos requests HTTPS por vuelta (refresh del token +
events.list), nada de LLM. Por eso no hay "intervalo inteligente": consultar
cada minuto es gratis y hace que un evento editado a mano se vea enseguida.

Diseño en tres piezas puras, todas testeables sin red:

- `parse_iso` / `Event`     : el dato, ya digerido.
- `pick`                     : qué evento merece una cara y con cuánta
                               anticipación. Puro.
- `GoogleCalendar`           : el transporte (OAuth + HTTP). Lo único que
                               toca la red.

La cara no decide nada de esto: el bridge manda `state=upcoming` con
`elapsed` = milisegundos *restantes*, y la escalera del firmware se encarga de
la intensidad. Así afinar la urgencia visual nunca significa tocar el bridge.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"

# Ventana por defecto: cuándo empieza a importarle a la cara. 60 min da lugar a
# las tres bandas del firmware (>10 min tranquilo, 2-10 min claro, <2 min
# insistiendo) sin que un recordatorio de "mañana" despierte al buddy hoy.
DEFAULT_LEAD_MIN = 60
# Un evento que ya empezó sigue siendo útil un rato ("la reunión es ahora"), pero
# no para siempre: 5 min de gracia y después la cara vuelve a la vida normal.
DEFAULT_GRACE_MIN = 5


class CalendarError(Exception):
    """El calendario no se pudo leer. El bridge lo loguea y sigue con la cara."""


@dataclass(frozen=True)
class Event:
    summary: str
    start: float          # epoch, segundos
    all_day: bool = False

    def remaining_ms(self, now: float) -> int:
        return int(round((self.start - now) * 1000.0))


def parse_iso(value: str) -> tuple[float, bool]:
    """`2026-11-19T15:30:00-03:00` -> (epoch, False). Todo el día -> (epoch, True).

    Un evento de todo el día en Google llega como `2026-11-19` (sin hora): se
    interpreta como medianoche local, que es lo que significa. El `Z` va con
    `+00:00` porque `fromisoformat` de las stdlib viejas (3.8/3.9, que es lo que
    puede haber en un LXC) no lo acepta.
    """
    text = (value or "").strip()
    if not text:
        raise CalendarError("fecha vacía")
    if "T" not in text:
        try:
            return time.mktime(time.strptime(text, "%Y-%m-%d")), True
        except ValueError as exc:
            raise CalendarError(f"fecha ilegible: {value!r}") from exc
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CalendarError(f"fecha ilegible: {value!r}") from exc
    if dt.tzinfo is None:
        return dt.timestamp(), False
    return dt.timestamp(), False


def _start_of(raw: dict) -> tuple[float, bool]:
    """Saca el arranque de un item de la API, sea `dateTime` o `date`."""
    node = raw.get("start") or {}
    if node.get("dateTime"):
        return parse_iso(node["dateTime"])
    if node.get("date"):
        return parse_iso(node["date"])
    raise CalendarError("evento sin start")


def events_from_payload(payload: dict) -> list[Event]:
    """Convierte la respuesta de events.list en eventos. Ignora lo que no sirve.

    Cancelados y sin resumen no se descartan por prolijidad: la cara tiene que
    decir algo corto y sin nombre no hay nada que decir.
    """
    out = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        if (item.get("status") or "confirmed") == "cancelled":
            continue
        summary = (item.get("summary") or "").strip()
        if not summary:
            continue
        try:
            start, all_day = _start_of(item)
        except CalendarError:
            continue
        out.append(Event(summary=summary, start=start, all_day=all_day))
    return out


def pick(events: list[Event], now: float, lead_min: int = DEFAULT_LEAD_MIN,
         grace_min: int = DEFAULT_GRACE_MIN) -> Event | None:
    """El próximo evento que merece una cara, o None.

    Pura a propósito: es la regla de negocio más delicada del módulo y se testea
    sin red ni relojes.

    La ventana es `[ahora - grace, ahora + lead]` y gana el **más urgente**,
    que no es "el futuro más cercano": un evento que ya empezó es más urgente
    que uno en 55 minutos, y taparlo con el lejano sería el caso pelotudo que
    esta función existe para evitar. Por eso la distancia se mide en valor
    absoluto: `|start - now|`.

    Empates los resuelve el futuro (un evento a 200 ms de empezar le gana al de
    hace 200 ms, que ya pasó).
    """
    horizon = now + lead_min * 60.0
    floor = now - grace_min * 60.0
    candidates = [e for e in events if floor <= e.start <= horizon]
    if not candidates:
        return None
    return min(candidates, key=lambda e: (abs(e.start - now), -e.start))


def describe(event: Event, now: float) -> str:
    """La etiqueta corta que se lee en la placa. La cara dibuja; el texto informa.

    En español rioplatense y sin relojes: "en 20 min" se entiende de un vistazo,
    "15:30" obliga a hacer la cuenta.
    """
    delta = event.start - now
    name = event.summary.strip()
    if len(name) > 22:
        name = name[:21].rstrip() + "…"
    if delta <= 0:
        return f"{name} ahora"
    minutes = int(round(delta / 60.0))
    if minutes < 1:
        return f"{name} ya"
    if minutes < 60:
        return f"{name} en {minutes} min"
    hours = minutes // 60
    return f"{name} en {hours} h"


# --------------------------------------------------------------------- transporte


class GoogleCalendar:
    """Lectura mínima de Google Calendar con `refresh_token` (stdlib puro).

    No usa `google-api-python-client` a propósito: el LXC no tiene pip para el
    servicio y esto es un POST + un GET. El `access_token` se guarda con su
    vencimiento, así que una vuelta por minuto gasta un refresh por hora, no uno
    por minuto (Google además rechaza refreshes muy seguidos).
    """

    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 calendar_id: str = "primary", timeout: float = 10.0, log=print):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.calendar_id = calendar_id or "primary"
        self.timeout = timeout
        self.log = log
        self._access = ""
        self._expires = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    # -- OAuth ---------------------------------------------------------------
    def _token(self) -> str:
        if self._access and time.time() < self._expires - 60:
            return self._access
        body = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # El cuerpo del error de Google dice qué pasa ("invalid_grant": token
            # revocado). Se loguea sin secretos y se propaga.
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise CalendarError(f"OAuth {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001 - red caída, DNS, timeout
            raise CalendarError(f"OAuth sin respuesta: {exc}") from exc
        self._access = data.get("access_token") or ""
        if not self._access:
            raise CalendarError("OAuth sin access_token")
        self._expires = time.time() + float(data.get("expires_in") or 3600)
        return self._access

    # -- lectura -------------------------------------------------------------
    def upcoming(self, now: float | None = None, lead_min: int = DEFAULT_LEAD_MIN,
                 grace_min: int = DEFAULT_GRACE_MIN) -> Event | None:
        """El próximo evento dentro de la ventana, o None. Lanza `CalendarError`."""
        now = time.time() if now is None else now
        token = self._token()
        params = urllib.parse.urlencode({
            "timeMin": _rfc3339(now - grace_min * 60.0),
            "timeMax": _rfc3339(now + lead_min * 60.0),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "10",
        })
        url = CALENDAR_URL.format(cal=urllib.parse.quote(self.calendar_id, safe="")) + "?" + params
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise CalendarError(f"events.list {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise CalendarError(f"events.list sin respuesta: {exc}") from exc
        return pick(events_from_payload(payload), now, lead_min, grace_min)


def _rfc3339(epoch: float) -> str:
    """Epoch -> `2026-11-19T15:30:00Z`. UTC siempre: Google acepta cualquier zona."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def make_calendar(config, log=print) -> "GoogleCalendar | None":
    """Arma el cliente desde la `Config`, o None si faltan credenciales.

    Una sola puerta de construcción: el arranque (`__main__`) y el guardado en
    caliente usan exactamente la misma, así no hay dos formas de quedar mal
    configurado.
    """
    if not config.calendar_enabled:
        return None
    return GoogleCalendar(
        config.calendar_client_id,
        config.calendar_client_secret,
        config.calendar_refresh_token,
        calendar_id=config.calendar_id,
        log=log,
    )

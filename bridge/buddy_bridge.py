#!/usr/bin/env python3
"""Bridge entre el ESP32-S3 buddy y el API server de Hermes (perfil default).

El buddy refleja SOLO al perfil default (Sofía en el Desktop), no a los bots.

Endpoints en la LAN (<bridge-host>:8787):
  GET  /state      -> {"state","label","ts","active","run_id","sleep_hint"} (cara, sin SSE)
  GET  /events     -> long-poll: devuelve [{seq,event,ts,...}] cuando hay algo nuevo
  POST /buddy/ask  -> {"prompt":"...","button":"left|center|right"} crea un run y sigue sus eventos
  GET  /health     -> liveness

La API_SERVER_KEY de Hermes NO va al ESP32: la placa usa BUDDY_TOKEN.
Stdlib pura (el LXC no tiene pip).

REGLAS DE DISEÑO (no romper):
  - El bridge NO decide cuándo dormirse. Manda `sleep_hint` (segundos de
    inactividad de Hermes) y el firmware decide, porque el sueño es del cuerpo.
  - El bridge NO manda `bored`. Ver docs/buddy-08-suenio-y-eventos.md.
"""
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERMES_URL = os.environ.get("HERMES_URL", "http://127.0.0.1:8642")
HERMES_KEY = os.environ.get("API_SERVER_KEY", "")
BUDDY_TOKEN = os.environ.get("BUDDY_TOKEN", "")
BUDDY_SESSION = os.environ.get("BUDDY_SESSION", "esp32-buddy")
PORT = int(os.environ.get("BUDDY_PORT", "8787"))
BIND = os.environ.get("BUDDY_BIND", "0.0.0.0")
POLL_SECONDS = float(os.environ.get("BUDDY_POLL_SECONDS", "3"))
DONE_TTL_SECONDS = float(os.environ.get("BUDDY_DONE_TTL", "8"))
# Cuántos segundos de inactividad de Hermes reporta /state como `sleep_hint`.
# El firmware lo usa SOLO como insumo: si el IMU detecta movimiento, resetea.
SLEEP_HINT_SINCE = float(os.environ.get("BUDDY_SLEEP_HINT_SINCE", "0"))
# Long-poll de /events: cuánto se puede quedar colgado el GET (segundos).
EVENTS_WAIT_MAX = float(os.environ.get("BUDDY_EVENTS_WAIT_MAX", "25"))
# Buffer de eventos para reintentos: la placa puede perderse algunos.
EVENTS_BUFFER = int(os.environ.get("BUDDY_EVENTS_BUFFER", "64"))

# Etiquetas <=40 chars por tool: lo único que ve la pantalla (nada sensible).
TOOL_LABELS = {
    "web_search": "buscando en la web", "web_extract": "leyendo una página",
    "terminal": "usando la terminal", "read_file": "leyendo un archivo",
    "write_file": "escribiendo un archivo", "patch": "editando código",
    "search_files": "buscando archivos", "execute_code": "corriendo código",
    "vision_analyze": "mirando una imagen", "delegate_task": "delegando",
    "todo_list": "ordenando pendientes", "_thinking": "pensando",
}

# last_activity_description -> etiqueta corta para la cara (<=40 chars, sin datos sensibles).
DESC_LABELS = {
    "starting new turn": "escuchándote",
    "thinking": "pensando",
    "responding": "respondiendo",
    "waiting for input": "te espera",
    "waiting": "te espera",
    "tool": "usando una herramienta",
    "streaming": "respondiendo",
    "compressing": "ordenando ideas",
}


def _label_for_desc(desc):
    """Traduce la descripción de la DB a una etiqueta de cara, sin filtrar contenido."""
    d = (desc or "").strip().lower()
    if not d:
        return "trabajando"
    # "tool completed: terminal (5.4s)" -> después de una tool, "pensando"
    if "tool completed" in d or "tool started" in d:
        for tool, label in TOOL_LABELS.items():
            if tool in d:
                return label if "tool started" in d else "pensando"
        return "pensando"
    for key, label in DESC_LABELS.items():
        if key in d:
            return label
    if "tool" in d:
        for tool, label in TOOL_LABELS.items():
            if tool in d:
                return label
        return "usando una herramienta"
    return "trabajando"


_lock = threading.Lock()
_state = {"state": "idle", "label": "acá estoy", "ts": time.time(),
          "run_id": None, "active": 0, "since": time.time(),
          "last_busy": time.time()}

# ---------------------------------------------------------------- eventos
# Los eventos son DISTINTOS del estado: el estado es "cómo estoy ahora",
# el evento es "pasó esto". Un poll puede perder un evento que duró <1 s;
# el long-poll de /events no. Buffer circular + seq monotónico para que la
# placa pueda pedir "desde el seq N" y recuperar lo que se perdió.
_events = deque(maxlen=EVENTS_BUFFER)
_ev_seq = 0
_ev_cond = threading.Condition()


def _emit(event, **fields):
    """Publica un evento y despierta a los que están en long-poll."""
    global _ev_seq
    with _ev_cond:
        _ev_seq += 1
        payload = {"seq": _ev_seq, "event": event, "ts": round(time.time(), 3)}
        payload.update({k: v for k, v in fields.items() if v is not None})
        _events.append(payload)
        _ev_cond.notify_all()
    return payload


def _events_since(seq, wait):
    """Devuelve eventos con seq > `seq`. Si no hay, espera hasta `wait` segundos."""
    deadline = time.time() + wait
    with _ev_cond:
        while True:
            pend = [e for e in _events if e["seq"] > seq]
            if pend or time.time() >= deadline:
                return pend
            _ev_cond.wait(timeout=max(0.05, deadline - time.time()))


def _set(state, label, run_id=None, active=None, clear_run=False):
    """Cambia la cara. Emite evento solo si el estado CAMBIÓ de verdad.

    Es el ÚNICO lugar que debe escribir _state desde lógica de estado: si otro
    sitio escribe el dict a mano, el cambio no se emite y la placa en long-poll
    se queda dormida. Ese fue el bug del 2026-11-18.
    """
    with _lock:
        prev = _state["state"]
        prev_label = _state["label"]
        now = time.time()
        if state != prev:
            _state["since"] = now
        _state["state"] = state
        _state["label"] = (label or "")[:40]
        _state["ts"] = now
        if state != "idle":
            _state["last_busy"] = now
        if clear_run:
            _state["run_id"] = None
        elif run_id is not None:
            _state["run_id"] = run_id
        if active is not None:
            _state["active"] = active
    if state != prev:
        _emit("state", state=state, label=(label or "")[:40], prev=prev)
    elif (label or "")[:40] != prev_label:
        _emit("label", state=state, label=(label or "")[:40])


def _snapshot():
    with _lock:
        snap = dict(_state)
    now = time.time()
    # Cuánto lleva en el estado actual: permite a la placa escalar los gestos
    # (pensando 2s != pensando 40s) sin que el bridge tenga que animar nada.
    base = snap.get("since") or snap.get("ts") or now
    snap["elapsed"] = round(max(0.0, now - base), 1)
    # 'phase' es el indicador SEMÁNTICO para la esquina de la pantalla:
    #   busy  -> tres puntitos (todavía no te contesté)
    #   ok    -> tilde (ya te contesté)
    #   ask   -> signo de admiración rojo (necesito algo tuyo)
    #   idle  -> nada / punto tenue
    snap["phase"] = _PHASE.get(snap.get("state"), "idle")
    # Color del LED ya resuelto: [r,g,b] según phase. El firmware no traduce nada.
    snap["led"] = list(_LED.get(snap["phase"], _LED["idle"]))
    # sleep_hint: segundos de inactividad de Hermes. Insumo, NO orden de dormir.
    # El firmware suma su propio timer de IMU; ver docs/buddy-08.
    idle_since = snap.get("last_busy") or snap.get("since") or now
    snap["sleep_hint"] = round(max(0.0, now - idle_since), 1)
    # Seq del evento más reciente: la placa lo usa como cursor para /events.
    with _ev_cond:
        snap["ev_seq"] = _ev_seq
    snap.pop("last_busy", None)
    return snap


def _req(method, path, body=None, headers=None, timeout=30):
    url = HERMES_URL.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", "Bearer " + HERMES_KEY)
    r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    return urllib.request.urlopen(r, timeout=timeout)


def _active_runs():
    """Agregado del perfil default: cuántos runs hay en curso ahora."""
    try:
        with _req("GET", "/health/detailed", timeout=6) as resp:
            d = json.loads(resp.read().decode())
        return int(d["readiness"]["checks"]["background_queues"].get("active_api_runs", 0))
    except Exception:
        return -1


STATE_DB = os.environ.get("BUDDY_STATE_DB", "/opt/data/state.db")


TOOL_ACTIVE_WINDOW = float(os.environ.get("BUDDY_TOOL_WINDOW", "12"))
# Un 'assistant' recién escrito puede ser narración previa a una tool: esperamos
# un momento antes de declarar el turno terminado (standby).
TURN_END_GRACE = float(os.environ.get("BUDDY_TURN_GRACE", "2.5"))

# state -> phase (indicador de esquina, independiente de la emoción de la cara).
# phase -> (color LED RGB, modo). El firmware puede usar esto directo en vez de
# traducir phase por su cuenta: así cambio de color en un solo lugar.
_LED = {
    "busy": (255, 190, 0),    # amarillo  - trabajando
    "ok": (0, 255, 80),       # verde     - te contesté (titila 5 s)
    "ask": (255, 40, 40),     # rojo      - necesito algo tuyo
    "idle": (0, 180, 255),    # celeste   - standby
}
_PHASE = {
    "thinking": "busy",
    "tool": "busy",
    "waiting": "ask",
    "error": "ask",
    "done": "ok",
    "idle": "idle",
}


def _db_state():
    """Lee el state DB: cubre Desktop, Telegram, cron y api_server del perfil default.

    Devuelve (activo, tool_en_curso, descripcion) o None si no pudo leer.

    Claves:
    - session_turn_leases: hay un turno vivo (pero dura ~300 s, no sirve para el fin).
    - último mensaje: es la señal REAL de fin de turno -> si el último es 'assistant',
      Sofía ya contestó y la cara debe ir a standby. Si es 'user' o 'tool', trabaja.
    """
    if not os.path.exists(STATE_DB):
        return None
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=3)
        try:
            now = time.time()
            row = con.execute(
                "SELECT conversation_id FROM session_turn_leases WHERE expires_at > ? LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return False, None, None
            conv = row[0]
            tool_name, desc = None, None
            # Ventana de 8 mensajes: alcanza para cruzar assistant->tool->assistant.
            rows = con.execute(
                "SELECT role, tool_name, timestamp FROM messages WHERE session_id = ? "
                "ORDER BY timestamp DESC, id DESC LIMIT 8",
                (conv,),
            ).fetchall()
            if not rows:
                return True, None, None

            last_role, _lt, last_ts = rows[0]
            if not isinstance(last_ts, (int, float)):
                last_ts = now

            # --- FIN DE TURNO: el último mensaje es del assistant -> ya contestó.
            # La gracia evita cortar cuando el assistant narra antes de una tool.
            if last_role == "assistant" and (now - last_ts) >= TURN_END_GRACE:
                return False, None, None

            # --- ¿hay una tool sin responder? (más nueva que el último assistant)
            last_assistant_ts = None
            for role, _t, ts in rows:
                if role == "assistant" and isinstance(ts, (int, float)):
                    last_assistant_ts = ts
                    break
            for role, tname, ts in rows:
                if role != "tool" or not tname or not isinstance(ts, (int, float)):
                    continue
                if last_ts - ts > TOOL_ACTIVE_WINDOW:
                    continue
                if last_assistant_ts is not None and ts <= last_assistant_ts:
                    continue  # la tool ya fue respondida
                tool_name = tname
                break
            r = con.execute(
                "SELECT last_activity_description FROM sessions WHERE id = ?",
                (conv,),
            ).fetchone()
            desc = (r[0] if r else None) or None
            return True, tool_name, desc
        finally:
            con.close()
    except Exception:
        return None


def _poll_global():
    """Mantiene 'active' al día aunque el run lo haya arrancado otro (Desktop, Telegram, cron).

    REGLA: todo cambio de cara pasa por _set(), que es lo único que emite eventos.
    Si esto escribiera _state a mano, la placa en /events no se enteraría nunca.
    """
    while True:
        db = _db_state()
        n = _active_runs()
        now = time.time()
        with _lock:
            currently = _state["state"]
            own_run = _state["run_id"] is not None
            since = _state["since"]
            # 'done' se respeta su TTL aunque haya actividad nueva.
            done_ttl_ok = currently == "done" and (now - _state["ts"] <= DONE_TTL_SECONDS)
            if db is not None:
                active, tool_name, desc = db
                src = desc or ""
            else:
                active, tool_name, src = (n > 0), None, ""
            _state["active"] = (1 if active else 0) if db is not None else n
        # Sólo tocamos la cara si no hay un run propio mandando.
        if own_run:
            pass
        elif done_ttl_ok:
            pass
        elif active:
            new_state = "tool" if tool_name else "thinking"
            new_label = (TOOL_LABELS.get(tool_name, "usando una herramienta")
                         if tool_name else _label_for_desc(src))
            _set(new_state, new_label)
        elif currently in ("thinking", "tool", "waiting"):
            _set("idle", "acá estoy")
        elif currently == "done" and (now - since > DONE_TTL_SECONDS):
            _set("idle", "acá estoy")
        # NOTA: acá NO hay transición a `bored`. Dormirse es del firmware
        # (timer propio + IMU). El bridge sólo informa `sleep_hint`.
        if active or currently in ("thinking", "tool", "waiting", "done"):
            time.sleep(POLL_SECONDS)
        else:
            time.sleep(max(POLL_SECONDS, 0.5))


def _follow_run(run_id):
    """Lee el SSE del run y traduce eventos a estados de cara."""
    _set("thinking", "pensando", run_id)
    last_seq = -1
    try:
        while True:
            hdrs = {"Last-Event-ID": str(last_seq)} if last_seq >= 0 else {}
            with _req("GET", f"/v1/runs/{run_id}/events", headers=hdrs,
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
                    name = ev.get("event", "")
                    if name in ("message.delta", "assistant.delta"):
                        _set("thinking", "pensando")
                    elif name == "tool.progress":
                        t = ev.get("tool_name") or "_thinking"
                        _set("thinking" if t == "_thinking" else "tool",
                             TOOL_LABELS.get(t, "trabajando"))
                    elif name == "tool.started":
                        _set("tool", TOOL_LABELS.get(ev.get("tool_name", ""),
                                                     "usando una herramienta"))
                    elif name == "approval.request":
                        _set("waiting", "te necesita")
                    elif name == "run.completed":
                        _set("done", "listo")
                        return
                    elif name in ("run.failed", "tool.failed"):
                        _set("error", "algo falló")
                        return
                    elif name == "run.cancelled":
                        _set("idle", "cancelado")
                        return
            break
    except Exception as exc:  # noqa: BLE001
        _set("error", "sin conexión")
        print(f"[bridge] error siguiendo {run_id}: {exc}", flush=True)
    finally:
        with _lock:
            _state["run_id"] = None


def _ask(prompt, button=None):
    """Crea un run con la sesión fija del buddy y arranca el seguimiento."""
    body = {"input": prompt, "session_id": BUDDY_SESSION}
    hdrs = {"X-Hermes-Session-Id": BUDDY_SESSION}
    if button:
        body["metadata"] = {"buddy_button": button}
    with _req("POST", "/v1/runs", body, hdrs) as resp:
        payload = json.loads(resp.read().decode())
    run_id = payload["run_id"]
    _emit("ask.accepted", run_id=run_id, button=button)
    threading.Thread(target=_follow_run, args=(run_id,), daemon=True).start()
    return {"run_id": run_id, "status": payload.get("status")}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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

    def _authorized(self):
        if not BUDDY_TOKEN:
            return True
        return (self.headers.get("Authorization", "") == "Bearer " + BUDDY_TOKEN
                or self.headers.get("X-Buddy-Token") == BUDDY_TOKEN)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            return self._send(200, {"status": "ok", "service": "hermes-buddy-bridge"})
        if path == "/state":
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, _snapshot())
        if path == "/events":
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            q = parse_qs(parsed.query)
            try:
                since = int(q.get("since", ["0"])[0])
            except ValueError:
                return self._send(400, {"error": "since must be int"})
            try:
                wait = float(q.get("wait", [str(EVENTS_WAIT_MAX)])[0])
            except ValueError:
                return self._send(400, {"error": "wait must be number"})
            wait = max(0.0, min(wait, EVENTS_WAIT_MAX))
            # Pedir un seq más nuevo que el último que tengo -> arrancá del actual.
            with _ev_cond:
                if since > _ev_seq:
                    since = _ev_seq
            evs = _events_since(since, wait)
            with _ev_cond:
                oldest = _events[0]["seq"] if _events else _ev_seq
            # Si el cursor quedó atrás del ring (se perdieron eventos por desborde
            # o reinicio del proceso), avisamos: la placa debe resincronizar con /state.
            if since < oldest - 1 and not evs:
                return self._send(200, [{"seq": _ev_seq, "event": "resync",
                                        "ts": round(time.time(), 3),
                                        "ev_seq": _ev_seq}])
            return self._send(200, evs)
        # La placa que solo hace poll: /state ya le alcanza. 404 explícito.
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/buddy/ask":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "invalid json"})
        prompt = (body.get("prompt") or body.get("question") or "").strip()
        button = (body.get("button") or None)
        if button not in (None, "left", "center", "right"):
            return self._send(400, {"error": "button must be left|center|right"})
        if not prompt:
            return self._send(400, {"error": "prompt required"})
        try:
            return self._send(202, _ask(prompt, button))
        except urllib.error.HTTPError as exc:
            return self._send(502, {"error": "hermes", "detail": exc.read().decode()[:300]})
        except Exception as exc:  # noqa: BLE001
            return self._send(502, {"error": str(exc)})


if __name__ == "__main__":
    threading.Thread(target=_poll_global, daemon=True).start()
    print(f"[bridge] escuchando en {BIND}:{PORT} -> {HERMES_URL} (perfil default)", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()

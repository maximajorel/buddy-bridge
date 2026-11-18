"""El servicio: el hilo que mantiene la cara al día y sigue los runs pedidos.

Junta las tres fuentes de verdad, en este orden de prioridad:

1. Un run propio (`/buddy/ask`): mientras exista, la cara la manda el SSE.
2. El `state.db`: cubre Desktop, Telegram y cron.
3. El health del API server: respaldo cuando la DB no se puede leer.

Y una regla de oro: la cara solo se toca por `Face.set_state`, que es lo que
emite los eventos que la placa escucha en long-poll.
"""

from __future__ import annotations

import threading
import time

from .config import Config
from .events import EventLog
from .face import BUSY_STATES, Face
from .hermes import HermesClient, HermesError
from .labels import IDLE_LABEL, label_for_desc, tool_label
from .state_db import StateDb


class BridgeService:
    def __init__(self, config: Config, face: Face | None = None,
                 hermes: HermesClient | None = None, state_db: StateDb | None = None,
                 log=print):
        self.config = config
        self.log = log
        self.face = face or Face(
            EventLog(config.events_buffer),
            done_ttl=config.done_ttl,
            sleep_hint_since=config.sleep_hint_since,
        )
        self.hermes = hermes or HermesClient(config.hermes_url, config.hermes_key)
        self.state_db = state_db or StateDb(
            config.state_db, tool_window=config.tool_window, turn_grace=config.turn_grace)
        self._stop = threading.Event()

    # --------------------------------------------------------------- ciclo global
    def poll_once(self) -> None:
        """Un paso del poll: lee la DB, ajusta `active` y decide la cara."""
        db = self.state_db.read()
        runs = self.hermes.active_runs()
        now = time.time()

        if db.active is None:
            active = runs > 0
            tool_name, desc = None, ""
        else:
            active = bool(db.active)
            tool_name, desc = db.tool_name, db.description or ""
        self.face.set_active((1 if active else 0) if db.active is not None else runs)

        # Un run propio manda: el poll no pisa su cara ni sus eventos.
        if self.face.own_run() or self.face.done_is_fresh():
            return
        if active:
            if tool_name:
                self.face.set_state("tool", tool_label(tool_name))
            else:
                self.face.set_state("thinking", label_for_desc(desc))
        elif self.face.state() in BUSY_STATES:
            self.face.set_state("idle", IDLE_LABEL)
        elif self.face.done_expired():
            self.face.set_state("idle", IDLE_LABEL)
        # Acá NO hay transición a `bored`: dormirse es del firmware (docs/buddy-08).

    def run_forever(self) -> None:
        """Loop del poll. Sale limpio cuando se llama `stop()`."""
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - el loop no muere por un fallo suelto
                self.log(f"[bridge] poll: {exc}")
            # Sin actividad se puede dormir un poco más: nadie espera una cara.
            delay = self.config.poll_seconds
            if not self.face.own_run() and self.face.state() == "idle":
                delay = max(delay, 0.5)
            self._stop.wait(delay)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------- run propio
    def ask(self, prompt: str, button: str | None = None) -> dict:
        """Crea el run y arranca el seguimiento. Lanza `HermesError` si no se pudo."""
        payload = self.hermes.create_run(prompt, self.config.buddy_session, button)
        run_id = payload["run_id"]
        self.face.events.emit("ask.accepted", run_id=run_id, button=button)
        threading.Thread(target=self.follow_run, args=(run_id,), daemon=True).start()
        return {"run_id": run_id, "status": payload.get("status")}

    def follow_run(self, run_id: str) -> None:
        """Traduce el SSE del run a estados de cara. Siempre suelta el run al final."""
        self.face.set_state("thinking", "pensando", run_id=run_id)
        try:
            for ev in self.hermes.stream_run(run_id):
                name = ev.get("event", "")
                if name in ("message.delta", "assistant.delta"):
                    self.face.set_state("thinking", "pensando")
                elif name == "tool.progress":
                    t = ev.get("tool_name") or "_thinking"
                    self.face.set_state("thinking" if t == "_thinking" else "tool",
                                        tool_label(t))
                elif name == "tool.started":
                    self.face.set_state("tool", tool_label(ev.get("tool_name")))
                elif name == "approval.request":
                    self.face.set_state("waiting", "te necesita")
                elif name == "run.completed":
                    self.face.set_state("done", "listo")
                    return
                elif name in ("run.failed", "tool.failed"):
                    self.face.set_state("error", "algo falló")
                    return
                elif name == "run.cancelled":
                    self.face.set_state("idle", "cancelado")
                    return
        except HermesError as exc:
            self.face.set_state("error", "sin conexión")
            self.log(f"[bridge] error siguiendo {run_id}: {exc}")
        finally:
            self.face.clear_run()

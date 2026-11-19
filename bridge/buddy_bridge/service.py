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

from .calendar import CalendarError, GoogleCalendar, make_calendar
from .config import Config
from .events import EventLog
from .face import BUSY_STATES, Face
from .hermes import HermesClient, HermesError
from .labels import IDLE_LABEL, label_for_desc, tool_label
from .state_db import StateDb


class BridgeService:
    def __init__(self, config: Config, face: Face | None = None,
                 hermes: HermesClient | None = None, state_db: StateDb | None = None,
                 calendar: GoogleCalendar | None = None, log=print):
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
        # El calendario es opcional: sin credenciales el bridge es el de siempre.
        self.calendar = calendar
        self._cal_stop = threading.Event()
        self._cal_thread = None

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
        # El calendario va después del run propio (si le estás hablando, la placa
        # te contesta a vos, no al reloj) y antes que la actividad: un evento
        # vigente no es "algo que estás haciendo", es la hora, y el poll no tiene
        # por qué borrarlo. El carril se reclama en `calendar_once`.
        if self.face.calendar_active():
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

    # ---------------------------------------------------------------- calendario
    def calendar_once(self, now: float | None = None) -> None:
        """Una vuelta del calendario: lee, decide y reclama el carril.

        Se separa del poll porque tiene otro ritmo (una vez por minuto) y otra
        forma de fallar (red, token revocado). Un error NO baja la cara: se
        loguea y se deja lo que estaba, que es lo que el dueño espera si el WiFi
        del LXC tose.
        """
        if self.calendar is None:
            return
        now = time.time() if now is None else now
        try:
            event = self.calendar.upcoming(now, self.config.calendar_lead_min,
                                           self.config.calendar_grace_min)
        except CalendarError as exc:
            # Salir SIN llamar a `_apply_calendar`: un fallo de red no es un
            # "no hay eventos". Liberar el carril acá haría que un hipo de WiFi
            # borre el aviso que ya estaba en pantalla. Si el evento igual se
            # vence, `calendar_until` lo suelta solo.
            self.log(f"[bridge] calendario: {exc}")
            return
        self._apply_calendar(event, now)

    def _apply_calendar(self, event, now: float) -> None:
        """Pone o saca el evento del carril. La reserva dura hasta que empieza."""
        if event is None:
            if self.face.calendar_active():
                self.face.release_calendar()
                self.log("[bridge] calendario: sin eventos en ventana")
            return
        was = self.face.calendar_event()
        # La reserva va hasta el arranque del evento (más la gracia): después el
        # evento ya no merece cara y el poll vuelve a mandar. `pick` garantiza que
        # el evento esté dentro de la ventana, así que esto siempre es futuro o
        # el pasado inmediato de la gracia.
        until = event.start + self.config.calendar_grace_min * 60.0
        self.face.set_calendar(event)
        self.face.claim_calendar(until)
        if was is None or was.summary != event.summary or abs(was.start - event.start) > 1:
            self.log(f"[bridge] calendario: próximo '{event.summary}' en "
                     f"{int((event.start - now) / 60)} min")

    def calendar_forever(self) -> None:
        """Loop del calendario. Ritmo propio: `calendar_poll_seconds` (60 s)."""
        while not self._cal_stop.is_set():
            try:
                self.calendar_once()
            except Exception as exc:  # noqa: BLE001 - el loop no muere por un fallo suelto
                self.log(f"[bridge] calendario: {exc}")
            # Si hay un evento vigente conviene mirar más seguido: un evento
            # adelantado en Google tiene que verse en segundos, no en un minuto.
            delay = self.config.calendar_poll_seconds
            if self.face.calendar_active():
                delay = min(delay, 15.0)
            self._cal_stop.wait(delay)

    def reload_calendar(self, config: Config) -> None:
        """Aplica credenciales nuevas sin reiniciar el servicio.

        Reemplaza el `Config` y rearma el cliente. El hilo del calendario se
        levanta si no estaba: guardar desde la web tiene que funcionar la primera
        vez, sin un `systemctl restart` que el dueño no pidió.
        """
        self.config = config
        self.calendar = make_calendar(config, log=self.log)
        if self.calendar is not None and not self._cal_stop.is_set():
            self.face.release_calendar()
            self.start_calendar()

    def start_calendar(self) -> None:
        """Arranca el hilo del calendario si hay credenciales."""
        if self.calendar is None or not getattr(self.calendar, "configured", False):
            return
        if self._cal_thread is not None and self._cal_thread.is_alive():
            return
        self._cal_thread = threading.Thread(target=self.calendar_forever,
                                            name="calendar", daemon=True)
        self._cal_thread.start()

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
        self._cal_stop.set()

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

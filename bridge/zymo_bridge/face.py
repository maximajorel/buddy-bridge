"""La cara: estado actual, `phase` y color de LED.

Este módulo es el ÚNICO que escribe el estado desde lógica de cara, y
`set_state` es el único método que emite eventos. Si otro sitio toca el dict a
mano, el cambio no se emite y la placa en long-poll se queda dormida (ese fue el
bug del 2026-11-18). `touch`/`set_active` existen para actualizar campos que NO
son la cara sin pasar por la transición.

`phase` y `led` salen pre-digeridos a propósito: agregar un estado o cambiar un
color se hace acá y el firmware no toca una línea.
"""

from __future__ import annotations

import threading
import time

from .events import EventLog
from .labels import IDLE_LABEL, clip

# state -> phase (indicador de esquina, independiente de la emoción de la cara).
# `bored` mapea a idle porque el bridge no manda `bored` (dormirse es del firmware).
PHASE = {
    "thinking": "busy",
    "tool": "busy",
    "waiting": "ask",
    "error": "ask",
    "done": "ok",
    "idle": "idle",
    "bored": "idle",
}

# phase -> color RGB ya resuelto. El firmware no traduce nada.
LED = {
    "busy": (255, 190, 0),   # amarillo - trabajando
    "ok": (0, 255, 80),      # verde    - te contesté
    "ask": (255, 40, 40),    # rojo     - necesito algo tuyo
    "idle": (0, 180, 255),   # celeste  - standby
}

BUSY_STATES = ("thinking", "tool", "waiting")


def phase_for(state: str) -> str:
    """`state` desconocido cae a idle: la placa nunca ve un phase inventado."""
    return PHASE.get(state, "idle")


def led_for(phase: str) -> list[int]:
    return list(LED.get(phase, LED["idle"]))


class Face:
    """Estado compartido de la cara. Thread-safe; de él leen el poll, el SSE y HTTP."""

    def __init__(self, events: EventLog, done_ttl: float = 8.0,
                 sleep_hint_since: float = 0.0):
        self.events = events
        self.done_ttl = done_ttl
        self.sleep_hint_since = sleep_hint_since
        now = time.time()
        self._lock = threading.Lock()
        self._state = {
            "state": "idle",
            "label": IDLE_LABEL,
            "run_id": None,
            "active": 0,
            "ts": now,
            "since": now,
            "last_busy": now,
        }

    # ------------------------------------------------------------------ lectura
    def state(self) -> str:
        with self._lock:
            return self._state["state"]

    def own_run(self) -> bool:
        """True si un `/buddy/ask` está mandando la cara (el poll no la pisa)."""
        with self._lock:
            return self._state["run_id"] is not None

    def done_is_fresh(self) -> bool:
        """`done` reciente: manda sobre la actividad nueva (los ~8 s del tilde)."""
        with self._lock:
            return (self._state["state"] == "done"
                    and time.time() - self._state["ts"] <= self.done_ttl)

    def in_transient_busy(self) -> bool:
        """En uno de los estados que el fin de turno debe bajar a idle."""
        with self._lock:
            return self._state["state"] in BUSY_STATES

    def drop_run(self) -> None:
        """Suelta el run propio (lectura sin lock reentrante)."""
        self.clear_run()

    def done_expired(self) -> bool:
        with self._lock:
            return (self._state["state"] == "done"
                    and time.time() - self._state["since"] > self.done_ttl)

    # ----------------------------------------------------------------- escritura
    def set_state(self, state: str, label: str = "", run_id=None,
                  active=None, clear_run: bool = False) -> None:
        """Cambia la cara y emite evento SOLO si cambió de verdad."""
        label = clip(label)
        with self._lock:
            prev = self._state["state"]
            prev_label = self._state["label"]
            now = time.time()
            if state != prev:
                self._state["since"] = now
            self._state["state"] = state
            self._state["label"] = label
            self._state["ts"] = now
            if state != "idle":
                self._state["last_busy"] = now
            if clear_run:
                self._state["run_id"] = None
            elif run_id is not None:
                self._state["run_id"] = run_id
            if active is not None:
                self._state["active"] = active
        if state != prev:
            self.events.emit("state", state=state, label=label, prev=prev)
        elif label != prev_label:
            self.events.emit("label", state=state, label=label)

    def set_active(self, active: int) -> None:
        """Actualiza el contador de actividad sin disparar transición de cara."""
        with self._lock:
            self._state["active"] = active

    def clear_run(self) -> None:
        with self._lock:
            self._state["run_id"] = None

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> dict:
        """Lo que devuelve `GET /state`, listo para que la placa lo dibuje."""
        with self._lock:
            snap = dict(self._state)
        now = time.time()
        base = snap.get("since") or snap.get("ts") or now
        snap["elapsed"] = round(max(0.0, now - base), 1)
        snap["phase"] = phase_for(snap.get("state"))
        snap["led"] = led_for(snap["phase"])
        # sleep_hint: segundos de inactividad de Hermes. Insumo, NO orden de dormir:
        # el firmware suma su propio timer de IMU (docs/buddy-08).
        idle_since = snap.get("last_busy") or snap.get("since") or now
        snap["sleep_hint"] = round(max(0.0, now - idle_since), 1)
        snap["ev_seq"] = self.events.seq
        snap.pop("last_busy", None)
        return snap

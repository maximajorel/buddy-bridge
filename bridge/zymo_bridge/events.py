"""Ring de eventos con long-poll: la memoria de "qué pasó".

Es distinto del estado (`face.py`): el estado es "cómo estoy ahora" y un poll
puede perderse un cambio que duró <1 s; el evento es "pasó esto" y esta cola
permite a la placa pedir `desde el seq N` y recuperar lo que no llegó a ver.

Un `seq` monotónico es el cursor. Si la placa pide un `seq` más nuevo que el
último emitido (placa reiniciada con un cursor viejo) la cola lo trata como
"arrancá desde ahora"; si quedó atrás del ring, contesta un `resync` para que
la placa vuelva a leer `/state` en vez de creer que no pasó nada.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class EventLog:
    """Cola acotada de eventos con despertar por condición (thread-safe)."""

    def __init__(self, capacity: int = 64):
        self._events = deque(maxlen=max(1, capacity))
        self._seq = 0
        self._cond = threading.Condition()

    @property
    def seq(self) -> int:
        """Último seq emitido. La placa lo usa como cursor inicial."""
        with self._cond:
            return self._seq

    def oldest_seq(self) -> int:
        with self._cond:
            return self._events[0]["seq"] if self._events else self._seq

    def emit(self, event: str, **fields) -> dict:
        """Publica un evento y despierta a todos los que están esperando."""
        payload = None
        with self._cond:
            self._seq += 1
            payload = {"seq": self._seq, "event": event, "ts": round(time.time(), 3)}
            payload.update({k: v for k, v in fields.items() if v is not None})
            self._events.append(payload)
            self._cond.notify_all()
        return payload

    def pending(self, since: int) -> list[dict]:
        with self._cond:
            return [e for e in self._events if e["seq"] > since]

    def wait(self, since: int, timeout: float) -> list[dict]:
        """Eventos con `seq > since`. Si no hay, espera hasta `timeout` segundos."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                pend = [e for e in self._events if e["seq"] > since]
                if pend:
                    return pend
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cond.wait(timeout=remaining)

    def since_request(self, since: int, timeout: float) -> list[dict]:
        """Respuesta completa de `GET /events?since=N&wait=T`, con `resync` si hace falta."""
        with self._cond:
            current = self._seq
            oldest = self._events[0]["seq"] if self._events else self._seq
        # Cursor del futuro: el cliente pide un `seq` que este proceso nunca emitió
        # (sobrevivió a un reinicio del bridge, cuyo contador vuelve a 0). Es
        # exactamente el caso del bug del 2026-11-18: la placa quedó pidiendo 102
        # contra un ring que iba por 47, y como `wait` filtra por `seq > since` no
        # había nada nuevo que devolver, así que el long-poll se cerraba vacío tras
        # 25 s y la cara se congelaba en "pensando". Se responde lo que hay más un
        # `resync` para que la placa relea `/state` y adopte el seq real.
        if since > current:
            events = self.pending(oldest - 1)
            return events + [{"seq": current, "event": "resync",
                              "ts": round(time.time(), 3), "ev_seq": current,
                              "reason": "cursor-ahead"}]
        events = self.wait(since, timeout)
        with self._cond:
            oldest = self._events[0]["seq"] if self._events else self._seq
            current = self._seq
        # Hueco en el cursor: lo que quedó entre `since` y `oldest` ya se descartó
        # del ring. Devuelve `resync` aunque haya eventos nuevos, porque los nuevos
        # no alcanzan para reconstruir lo perdido (la placa relee /state).
        if since < oldest - 1:
            return [{"seq": current, "event": "resync", "ts": round(time.time(), 3),
                     "ev_seq": current}]
        return events

    def clear(self) -> None:
        """Solo para tests: vacía la cola y reinicia el cursor."""
        with self._cond:
            self._events.clear()
            self._seq = 0

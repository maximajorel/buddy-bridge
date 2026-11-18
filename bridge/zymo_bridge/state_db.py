"""Lectura de solo lectura del `state.db` de Hermes.

Es la señal más honesta de "qué está haciendo el agente ahora": cubre Desktop,
Telegram, cron y api_server del perfil default, sin acoplarse a ningún evento.

Dos claves del esquema, aprendidas a la mala:

- `session_turn_leases` dice si hay un turno vivo, pero el lease dura ~300 s: sirve
  para saber que SÍ, nunca para saber que YA TERMINÓ. El fin de turno se detecta
  por el último mensaje.
- Si el último mensaje de la conversación es `assistant`, Sofía ya contestó y la
  cara va a standby (con una gracia corta, porque el assistant también narra antes
  de llamar una tool). Si es `user` o `tool`, sigue trabajando.

La conexión es `mode=ro` a propósito: el bridge no debe poder escribir en la DB
del agente ni por accidente.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

RECENT_MESSAGES = 8  # alcanza para cruzar assistant -> tool -> assistant


@dataclass(frozen=True)
class DbState:
    """Lo que ve el bridge. `active=None` significa "no pude leer", no "está libre"."""

    active: bool | None
    tool_name: str | None = None
    description: str | None = None


def _is_time(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class StateDb:
    def __init__(self, path: Path, tool_window: float = 12.0, turn_grace: float = 2.5):
        self.path = Path(path)
        self.tool_window = tool_window
        self.turn_grace = turn_grace

    def read(self) -> DbState:
        """Estado actual. Nunca lanza: un fallo se reporta como `active=None`."""
        if not self.path.exists():
            return DbState(active=None)
        try:
            con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=3)
        except sqlite3.Error:
            return DbState(active=None)
        try:
            return self._read(con)
        except sqlite3.Error:
            return DbState(active=None)
        finally:
            con.close()

    # ------------------------------------------------------------------ interno
    def _read(self, con: sqlite3.Connection) -> DbState:
        now = time.time()
        row = con.execute(
            "SELECT conversation_id FROM session_turn_leases WHERE expires_at > ? LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            return DbState(active=False)
        conv = row[0]
        rows = con.execute(
            "SELECT role, tool_name, timestamp FROM messages WHERE session_id = ? "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            (conv, RECENT_MESSAGES),
        ).fetchall()
        if not rows:
            return DbState(active=True)

        last_role, _last_tool, last_ts = rows[0]
        if not _is_time(last_ts):
            last_ts = now

        # Fin de turno: el último mensaje es del assistant y ya pasó la gracia.
        if last_role == "assistant" and (now - last_ts) >= self.turn_grace:
            return DbState(active=False)

        # ¿Hay una tool sin responder? Es la más nueva que el último assistant.
        last_assistant_ts = None
        for role, _t, ts in rows:
            if role == "assistant" and _is_time(ts):
                last_assistant_ts = ts
                break
        tool_name = None
        for role, tname, ts in rows:
            if role != "tool" or not tname or not _is_time(ts):
                continue
            # La antigüedad se mide contra AHORA, no contra el último mensaje:
            # una tool colgada a mitad de turno no deja de estar en curso porque
            # el turno siga vivo.
            if now - ts > self.tool_window:
                continue
            if last_assistant_ts is not None and ts <= last_assistant_ts:
                continue  # la tool ya fue respondida
            tool_name = tname
            break

        desc_row = con.execute(
            "SELECT last_activity_description FROM sessions WHERE id = ?", (conv,)
        ).fetchone()
        desc = (desc_row[0] if desc_row else None) or None
        return DbState(active=True, tool_name=tool_name, description=desc)

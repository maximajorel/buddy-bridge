"""Tests de `state_db` contra una SQLite temporal con el esquema mínimo de Hermes."""

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from zymo_bridge.state_db import StateDb

SCHEMA = """
CREATE TABLE session_turn_leases (conversation_id TEXT, expires_at REAL);
CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                       tool_name TEXT, timestamp REAL);
CREATE TABLE sessions (id TEXT PRIMARY KEY, last_activity_description TEXT);
"""


class StateDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"
        self.con = sqlite3.connect(self.path)
        self.con.executescript(SCHEMA)
        self.db = StateDb(self.path, tool_window=12, turn_grace=2.5)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def lease(self, expires_in=60, conv="c1"):
        self.con.execute("INSERT INTO session_turn_leases VALUES (?, ?)",
                         (conv, time.time() + expires_in))
        self.con.commit()

    def msg(self, role, ts_offset=0.0, tool=None, conv="c1"):
        self.con.execute(
            "INSERT INTO messages (session_id, role, tool_name, timestamp) VALUES (?,?,?,?)",
            (conv, role, tool, time.time() - ts_offset))
        self.con.commit()

    def test_archivo_inexistente_es_none(self):
        self.assertEqual(StateDb(Path(self.tmp.name) / "nope.db").read().active, None)

    def test_sin_lease_no_esta_activo(self):
        self.assertFalse(self.db.read().active)

    def test_lease_vencido_no_esta_activo(self):
        self.lease(expires_in=-5)
        self.assertFalse(self.db.read().active)

    def test_lease_sin_mensajes_esta_activo(self):
        self.lease()
        state = self.db.read()
        self.assertTrue(state.active)
        self.assertIsNone(state.tool_name)

    def test_ultimo_assistant_viejo_es_fin_de_turno(self):
        self.lease()
        self.msg("user", ts_offset=20)
        self.msg("assistant", ts_offset=10)
        self.assertFalse(self.db.read().active)

    def test_ultimo_assistant_reciente_respeta_la_gracia(self):
        # Puede ser narración antes de una tool: todavía cuenta como activo.
        self.lease()
        self.msg("assistant", ts_offset=0.5)
        self.assertTrue(self.db.read().active)

    def test_tool_sin_responder_se_reporta(self):
        self.lease()
        self.msg("user", ts_offset=6)
        self.msg("assistant", ts_offset=5, tool=None)
        self.msg("tool", ts_offset=4, tool="terminal")
        state = self.db.read()
        self.assertTrue(state.active)
        self.assertEqual(state.tool_name, "terminal")

    def test_tool_ya_respondida_no_cuenta(self):
        self.lease()
        self.msg("tool", ts_offset=4, tool="terminal")
        self.msg("assistant", ts_offset=1)
        state = self.db.read()
        self.assertTrue(state.active)
        self.assertIsNone(state.tool_name)

    def test_tool_fuera_de_ventana_no_cuenta(self):
        self.lease()
        self.msg("user", ts_offset=30)
        self.msg("tool", ts_offset=20, tool="terminal")  # > tool_window
        self.assertIsNone(self.db.read().tool_name)

    def test_descripcion_de_la_sesion(self):
        self.lease()
        self.con.execute("INSERT INTO sessions VALUES ('c1', 'thinking')")
        self.msg("user", ts_offset=1)
        self.con.commit()
        self.assertEqual(self.db.read().description, "thinking")

    def test_db_corrupta_es_none(self):
        self.path.write_bytes(b"no soy sqlite")
        self.assertIsNone(self.db.read().active)


if __name__ == "__main__":
    unittest.main()

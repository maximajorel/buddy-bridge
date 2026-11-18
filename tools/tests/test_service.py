"""Tests de `service`: el poll decide la cara y respeta el run propio."""

import unittest

from zymo_bridge.config import Config
from zymo_bridge.events import EventLog
from zymo_bridge.face import Face
from zymo_bridge.service import BridgeService
from zymo_bridge.state_db import DbState


class FakeDb:
    def __init__(self, state):
        self.state = state

    def read(self):
        return self.state


class FakeHermes:
    def __init__(self, runs=0):
        self.runs = runs

    def active_runs(self):
        return self.runs


def make(db_state, runs=0, ttl=8.0):
    config = Config()
    face = Face(EventLog(16), done_ttl=ttl)
    service = BridgeService(config, face=face, hermes=FakeHermes(runs),
                            state_db=FakeDb(db_state), log=lambda *_: None)
    return service, face


class PollTest(unittest.TestCase):
    def test_activo_con_tool_muestra_la_tool(self):
        service, face = make(DbState(True, "terminal", ""))
        service.poll_once()
        snap = face.snapshot()
        self.assertEqual(snap["state"], "tool")
        self.assertEqual(snap["label"], "usando la terminal")
        self.assertEqual(snap["phase"], "busy")

    def test_activo_sin_tool_usa_la_descripcion(self):
        service, face = make(DbState(True, None, "thinking"))
        service.poll_once()
        self.assertEqual(face.snapshot()["state"], "thinking")
        self.assertEqual(face.snapshot()["label"], "pensando")

    def test_vuelve_a_idle_cuando_termina(self):
        service, face = make(DbState(True, "terminal", ""))
        service.poll_once()
        service.state_db.state = DbState(False)
        service.poll_once()
        self.assertEqual(face.snapshot()["state"], "idle")

    def test_db_ilegible_usa_el_health(self):
        service, face = make(DbState(None), runs=2)
        service.poll_once()
        self.assertEqual(face.snapshot()["active"], 2)
        self.assertEqual(face.snapshot()["state"], "thinking")

    def test_run_propio_no_se_pisa(self):
        service, face = make(DbState(False))
        face.set_state("thinking", "pensando", run_id="r1")
        service.poll_once()
        self.assertEqual(face.snapshot()["state"], "thinking")
        self.assertEqual(face.snapshot()["run_id"], "r1")

    def test_done_reciente_no_se_pisa(self):
        service, face = make(DbState(True, "terminal", ""))
        face.set_state("done", "listo")
        service.poll_once()
        self.assertEqual(face.snapshot()["state"], "done")

    def test_nunca_emite_bored(self):
        service, face = make(DbState(False))
        for _ in range(3):
            service.poll_once()
        self.assertNotIn("bored", [e.get("state") for e in face.events.pending(0)])

    def test_idle_estable_no_genera_eventos(self):
        service, face = make(DbState(False))
        service.poll_once()
        service.poll_once()
        self.assertEqual(face.events.pending(0), [])


class FollowRunTest(unittest.TestCase):
    def test_tool_started_dibuja_la_tool(self):
        service, face = make(DbState(False))
        service.hermes.stream_run = lambda run_id: iter([
            {"event": "tool.started", "tool_name": "web_search"},
            {"event": "run.completed"},
        ])
        service.follow_run("r1")
        self.assertEqual(face.snapshot()["state"], "done")
        self.assertFalse(face.own_run())
        labels = [e.get("label") for e in face.events.pending(0) if "label" in e]
        self.assertIn("buscando en la web", labels)

    def test_fallo_deja_estado_error(self):
        service, face = make(DbState(False))
        service.hermes.stream_run = lambda run_id: iter([{"event": "run.failed"}])
        service.follow_run("r1")
        snap = face.snapshot()
        self.assertEqual(snap["state"], "error")
        self.assertEqual(snap["phase"], "ask")

    def test_aprobacion_pide_atencion(self):
        service, face = make(DbState(False))
        service.hermes.stream_run = lambda run_id: iter([
            {"event": "approval.request"},
            {"event": "run.cancelled"},
        ])
        service.follow_run("r1")
        self.assertEqual(face.events.pending(0)[1]["event"], "state")
        self.assertEqual(face.events.pending(0)[1]["state"], "waiting")


if __name__ == "__main__":
    unittest.main()

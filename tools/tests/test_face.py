"""Tests de `face`: transiciones, emisión de eventos y snapshot."""

import unittest

from zymo_bridge.events import EventLog
from zymo_bridge.face import IDLE_LABEL, Face, led_for, phase_for


def make(ttl=8.0):
    log = EventLog(16)
    return Face(log, done_ttl=ttl), log


class PhaseTest(unittest.TestCase):
    def test_mapa_de_phases(self):
        self.assertEqual(phase_for("thinking"), "busy")
        self.assertEqual(phase_for("tool"), "busy")
        self.assertEqual(phase_for("waiting"), "ask")
        self.assertEqual(phase_for("error"), "ask")
        self.assertEqual(phase_for("done"), "ok")
        self.assertEqual(phase_for("idle"), "idle")

    def test_estado_desconocido_cae_a_idle(self):
        self.assertEqual(phase_for("inventado"), "idle")
        self.assertEqual(led_for("inventado"), [0, 180, 255])


class FaceTest(unittest.TestCase):
    def test_arranca_idle(self):
        face, _ = make()
        snap = face.snapshot()
        self.assertEqual(snap["state"], "idle")
        self.assertEqual(snap["label"], IDLE_LABEL)
        self.assertEqual(snap["phase"], "idle")

    def test_cambio_de_estado_emite_evento(self):
        face, log = make()
        face.set_state("thinking", "pensando")
        events = log.pending(0)
        self.assertEqual([e["event"] for e in events], ["state"])
        self.assertEqual(events[0]["prev"], "idle")

    def test_mismo_estado_no_emite_state(self):
        face, log = make()
        face.set_state("thinking", "pensando")
        face.set_state("thinking", "pensando")
        self.assertEqual(len(log.pending(0)), 1)

    def test_label_distinto_emite_label(self):
        face, log = make()
        face.set_state("tool", "usando la terminal")
        face.set_state("tool", "corriendo código")
        self.assertEqual([e["event"] for e in log.pending(0)], ["state", "label"])

    def test_label_se_corta_a_40(self):
        face, _ = make()
        face.set_state("tool", "x" * 80)
        self.assertEqual(len(face.snapshot()["label"]), 40)

    def test_snapshot_trae_led_y_phase_resueltos(self):
        face, _ = make()
        face.set_state("waiting", "te necesita")
        snap = face.snapshot()
        self.assertEqual(snap["phase"], "ask")
        self.assertEqual(snap["led"], [255, 40, 40])
        self.assertEqual(snap["ev_seq"], 1)
        self.assertNotIn("last_busy", snap)

    def test_run_id_se_limpia_con_clear_run(self):
        face, _ = make()
        face.set_state("thinking", "pensando", run_id="r1")
        self.assertTrue(face.own_run())
        face.clear_run()
        self.assertFalse(face.own_run())

    def test_done_vence_por_ttl(self):
        face, _ = make(ttl=0.0)
        face.set_state("done", "listo")
        self.assertFalse(face.done_is_fresh())
        self.assertTrue(face.done_expired())

    def test_set_active_no_emite(self):
        face, log = make()
        face.set_active(3)
        self.assertEqual(face.snapshot()["active"], 3)
        self.assertEqual(log.pending(0), [])

    def test_sleep_hint_crece_con_la_inactividad(self):
        face, _ = make()
        face.set_state("thinking", "pensando")
        face.set_state("idle", IDLE_LABEL)
        hint = face.snapshot()["sleep_hint"]
        self.assertGreaterEqual(hint, 0.0)


if __name__ == "__main__":
    unittest.main()

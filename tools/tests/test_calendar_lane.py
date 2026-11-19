"""Tests del carril del calendario: el poll no puede pisar un evento vigente.

El bug que esto previene es silencioso y desesperante: `poll_once` manda `idle`
cada 3 segundos, así que si el evento no tiene reserva propia, la cara muestra
"se viene algo" durante un poll y al siguiente vuelve a standby. El dueño ve un
parpadeo y nunca el aviso.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buddy_bridge.calendar import Event
from buddy_bridge.config import Config
from buddy_bridge.events import EventLog
from buddy_bridge.face import Face
from buddy_bridge.service import BridgeService


class CalendarLaneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = Path(self.tmp.name) / ".env"
        env.write_text("BUDDY_TOKEN=r\nBUDDY_ASK_TOKEN=a\n", encoding="utf-8")
        self.config = Config.load(env)
        self.face = Face(EventLog(32))
        self.service = BridgeService(self.config, face=self.face, log=lambda *_: None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_pone_el_evento_y_reclama_el_carril(self):
        now = 1_000_000.0
        with mock.patch("time.time", return_value=now):
            self.service._apply_calendar(Event("Dentista", now + 300), now)
            self.assertTrue(self.face.calendar_active())
            self.assertEqual(self.face.calendar_event().summary, "Dentista")

    def test_snapshot_reescribe_state_phase_led_y_elapsed(self):
        now = 1_000_000.0
        with mock.patch("time.time", return_value=now):
            self.service._apply_calendar(Event("Dentista", now + 300), now)
            snap = self.face.snapshot()
        self.assertEqual(snap["state"], "upcoming")
        self.assertEqual(snap["phase"], "event")
        self.assertEqual(snap["label"], "Dentista")
        # `elapsed` es el tiempo RESTANTE: así lo lee la escalera del firmware.
        self.assertAlmostEqual(snap["elapsed"], 300.0, places=1)
        self.assertEqual(snap["led"], [170, 80, 255])

    def test_el_poll_no_pisa_un_evento_vigente(self):
        now = 1_000_000.0
        with mock.patch("time.time", return_value=now):
            self.service._apply_calendar(Event("Dentista", now + 300), now)
            self.face.set_state("idle")
            # Tres polls seguidos: sin el carril, el primero ya lo borraría.
            # Se mira el snapshot, que es lo que la placa ve.
            for _ in range(3):
                self.service.poll_once()
                self.assertEqual(self.face.snapshot()["state"], "upcoming")

    def test_pasada_la_gracia_el_poll_vuelve_a_mandar(self):
        now = 1_000_000.0
        with mock.patch("time.time", return_value=now):
            self.service._apply_calendar(Event("Dentista", now + 300), now)
        # Arranque + gracia (5 min): pasada la reserva, el carril suelta.
        later = now + 300 + 6 * 60
        with mock.patch("time.time", return_value=later):
            self.assertFalse(self.face.calendar_active())
            self.assertEqual(self.face.snapshot()["state"], "idle")

    def test_un_evento_no_cancela_una_cara_de_run_propio(self):
        # Si le estás hablando, la placa te contesta a vos, no al reloj.
        with mock.patch("time.time", return_value=1_000_000.0):
            self.face.set_state("thinking", "pensando", run_id="run-1")
            self.service._apply_calendar(Event("Dentista", 1_000_300.0), 1_000_000.0)
            self.service.poll_once()
            self.assertNotEqual(self.face.snapshot()["state"], "upcoming")

    def test_release_limpia_todo(self):
        self.service._apply_calendar(Event("Dentista", 1_000_300.0), 1_000_000.0)
        self.face.release_calendar()
        self.assertFalse(self.face.calendar_active())
        self.assertIsNone(self.face.calendar_event())

    def test_un_fallo_del_calendario_no_baja_la_cara(self):
        # Con red rota, el evento que ya estaba sigue mostrándose: es lo que el
        # dueño espera si el WiFi del LXC tose, y es la razón por la que
        # `calendar_once` no llama a `_apply_calendar(None, ...)` cuando falla.
        from buddy_bridge.calendar import CalendarError
        boom = mock.Mock()
        boom.upcoming.side_effect = CalendarError("sin red")
        self.service.calendar = boom
        now = 1_000_000.0
        with mock.patch("time.time", return_value=now):
            self.service._apply_calendar(Event("Dentista", now + 300), now)
            self.service.calendar_once(now)
            self.assertEqual(self.face.snapshot()["state"], "upcoming")
        self.assertEqual(boom.upcoming.call_count, 1)


if __name__ == "__main__":
    unittest.main()

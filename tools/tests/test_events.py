"""Tests de `events`: ring acotado, cursor y resync."""

import threading
import time
import unittest

from zymo_bridge.events import EventLog


class EventLogTest(unittest.TestCase):
    def test_emit_avanza_seq(self):
        log = EventLog(4)
        a = log.emit("state", state="idle")
        b = log.emit("state", state="thinking")
        self.assertEqual((a["seq"], b["seq"]), (1, 2))
        self.assertEqual(log.seq, 2)

    def test_emit_descarta_campos_none(self):
        log = EventLog()
        ev = log.emit("label", label="x", run_id=None)
        self.assertNotIn("run_id", ev)

    def test_pending_filtra_por_cursor(self):
        log = EventLog(8)
        for i in range(3):
            log.emit("n", i=i)
        self.assertEqual([e["i"] for e in log.pending(1)], [1, 2])

    def test_ring_descarta_los_viejos(self):
        log = EventLog(2)
        for i in range(5):
            log.emit("n", i=i)
        self.assertEqual([e["i"] for e in log.pending(0)], [3, 4])

    def test_cursor_del_futuro_devuelve_lo_pendiente_y_pide_resync(self):
        # Bug del 2026-11-18: tras reiniciar el bridge el contador vuelve a 0 y la
        # placa sigue pidiendo `since=102`. Antes se devolvía [] para siempre y la
        # cara quedaba congelada en "pensando".
        log = EventLog()
        log.emit("a")
        log.emit("b")
        events = log.since_request(999, 0)
        self.assertEqual([e["event"] for e in events], ["a", "b", "resync"])
        self.assertEqual(events[-1]["reason"], "cursor-ahead")
        self.assertEqual(events[-1]["seq"], log.seq)

    def test_cursor_del_futuro_no_espera_el_timeout(self):
        log = EventLog()
        log.emit("a")
        started = time.monotonic()
        log.since_request(500, 25)
        self.assertLess(time.monotonic() - started, 1)

    def test_cursor_perdido_devuelve_resync(self):
        log = EventLog(1)
        for _ in range(3):
            log.emit("x")
        # Ring de 1: quedan sólo seq 3; un cursor en 0 perdió todo.
        events = log.since_request(0, 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "resync")

    def test_wait_devuelve_apenas_hay_evento(self):
        log = EventLog()
        threading.Timer(0.1, lambda: log.emit("late")).start()
        started = time.monotonic()
        events = log.wait(0, 5)
        self.assertEqual(events[0]["event"], "late")
        self.assertLess(time.monotonic() - started, 2)

    def test_wait_respeta_el_timeout(self):
        log = EventLog()
        started = time.monotonic()
        self.assertEqual(log.wait(0, 0.15), [])
        self.assertGreaterEqual(time.monotonic() - started, 0.1)


if __name__ == "__main__":
    unittest.main()

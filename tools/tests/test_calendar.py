"""Tests de `calendar`: parseo, selección del evento y el carril de la cara.

Todo lo que se rompe en silencio vive acá: una fecha mal parseada manda la cara
una hora al carajo, y un `pick` con la comparación al revés tapa el evento que
empieza en dos minutos con uno que empieza mañana. Ni red ni reloj: el `now` entra
por parámetro.
"""

import time
import unittest
from unittest import mock

from buddy_bridge.calendar import (
    CalendarError,
    Event,
    GoogleCalendar,
    describe,
    events_from_payload,
    parse_iso,
    pick,
)


class ParseIsoTest(unittest.TestCase):
    def test_datetime_con_zona(self):
        epoch, all_day = parse_iso("2026-11-19T15:30:00-03:00")
        self.assertFalse(all_day)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.gmtime(epoch)), "2026-11-19 18:30")

    def test_z_final(self):
        epoch, all_day = parse_iso("2026-11-19T18:30:00Z")
        self.assertFalse(all_day)
        self.assertEqual(epoch, parse_iso("2026-11-19T15:30:00-03:00")[0])

    def test_todo_el_dia_es_local(self):
        epoch, all_day = parse_iso("2026-11-19")
        self.assertTrue(all_day)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)), "2026-11-19 00:00")

    def test_vacio_falla(self):
        with self.assertRaises(CalendarError):
            parse_iso("")

    def test_basura_falla(self):
        with self.assertRaises(CalendarError):
            parse_iso("mañana a las 5")


class PayloadTest(unittest.TestCase):
    def payload(self, *items):
        return {"items": list(items)}

    def test_lee_datetime_y_date(self):
        evs = events_from_payload(self.payload(
            {"summary": "Dentista", "start": {"dateTime": "2026-11-19T15:30:00-03:00"}},
            {"summary": "Feriado", "start": {"date": "2026-11-20"}},
        ))
        self.assertEqual([e.summary for e in evs], ["Dentista", "Feriado"])
        self.assertFalse(evs[0].all_day)
        self.assertTrue(evs[1].all_day)

    def test_descarta_cancelados(self):
        evs = events_from_payload(self.payload(
            {"summary": "Viejo", "status": "cancelled", "start": {"dateTime": "2026-11-19T10:00:00Z"}},
            {"summary": "Vivo", "status": "confirmed", "start": {"dateTime": "2026-11-19T11:00:00Z"}},
        ))
        self.assertEqual([e.summary for e in evs], ["Vivo"])

    def test_descarta_sin_resumen(self):
        # Sin nombre no hay nada que decir en la placa.
        evs = events_from_payload(self.payload(
            {"start": {"dateTime": "2026-11-19T11:00:00Z"}},
            {"summary": "  ", "start": {"dateTime": "2026-11-19T12:00:00Z"}},
        ))
        self.assertEqual(evs, [])

    def test_un_item_raro_no_rompe_la_lista(self):
        evs = events_from_payload(self.payload(
            "no soy un dict",
            {"summary": "Sin start"},
            {"summary": "Bien", "start": {"dateTime": "2026-11-19T11:00:00Z"}},
        ))
        self.assertEqual([e.summary for e in evs], ["Bien"])

    def test_payload_vacio(self):
        self.assertEqual(events_from_payload({}), [])


class PickTest(unittest.TestCase):
    NOW = 1_000_000.0

    def at(self, minutes):
        return Event(summary=f"e{minutes}", start=self.NOW + minutes * 60)

    def test_sin_eventos(self):
        self.assertIsNone(pick([], self.NOW))

    def test_fuera_de_ventana_lejos(self):
        self.assertIsNone(pick([self.at(61)], self.NOW, lead_min=60))

    def test_fuera_de_ventana_pasado(self):
        self.assertIsNone(pick([self.at(-6)], self.NOW, grace_min=5))

    def test_toma_el_mas_cercano_no_el_primero(self):
        events = [self.at(50), self.at(3), self.at(20)]
        self.assertEqual(pick(events, self.NOW).summary, "e3")

    def test_evento_empezando_manda_sobre_uno_lejano(self):
        # El caso pelotudo: uno en 55 min no puede tapar el que empezó ahora.
        events = [self.at(55), self.at(-1)]
        self.assertEqual(pick(events, self.NOW).summary, "e-1")

    def test_entre_dos_pasados_gana_el_mas_reciente(self):
        events = [self.at(-5), self.at(-1)]
        self.assertEqual(pick(events, self.NOW).summary, "e-1")

    def test_borde_de_la_ventana_entra(self):
        self.assertIsNotNone(pick([self.at(60)], self.NOW, lead_min=60))

    def test_gracia_cero_descarta_el_pasado(self):
        self.assertIsNone(pick([self.at(-1)], self.NOW, grace_min=0))


class DescribeTest(unittest.TestCase):
    def test_en_minutos(self):
        ev = Event(summary="Dentista", start=1000 + 20 * 60)
        self.assertEqual(describe(ev, 1000), "Dentista en 20 min")

    def test_en_horas(self):
        ev = Event(summary="Reunión", start=1000 + 125 * 60)
        self.assertEqual(describe(ev, 1000), "Reunión en 2 h")

    def test_ahora(self):
        self.assertEqual(describe(Event("Sync", 1000), 1000), "Sync ahora")

    def test_nombre_largo_se_recorta(self):
        ev = Event(summary="Daily de sincronización del equipo de plataforma", start=1060)
        text = describe(ev, 1000)
        self.assertLessEqual(len(text), 32)
        self.assertIn("…", text)


class GoogleCalendarTest(unittest.TestCase):
    def make(self, **kw):
        return GoogleCalendar("cid", "csecret", "rtoken", log=lambda *_: None, **kw)

    def test_configured_exige_las_tres(self):
        self.assertTrue(self.make().configured)
        self.assertFalse(GoogleCalendar("", "s", "r").configured)
        self.assertFalse(GoogleCalendar("c", "", "r").configured)
        self.assertFalse(GoogleCalendar("c", "s", "").configured)

    def test_calendario_por_defecto(self):
        self.assertEqual(GoogleCalendar("c", "s", "r", calendar_id="").calendar_id, "primary")

    def test_upcoming_arma_la_request_y_elige(self):
        cal = self.make(calendar_id="familia@group.calendar.google.com")
        cal._access = "tok"
        cal._expires = time.time() + 3600
        seen = {}

        # `timeMin`/`timeMax` los aplica Google, no el cliente: acá el payload
        # simula exactamente lo que devolvería (todo dentro de la ventana).
        payload = {"items": [
            {"summary": "Lejos", "start": {"dateTime": "2026-11-19T17:50:00Z"}},
            {"summary": "Cerca", "start": {"dateTime": "2026-11-19T17:05:00Z"}},
        ]}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return __import__("json").dumps(payload).encode()

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            return FakeResp()

        now = parse_iso("2026-11-19T17:00:00Z")[0]
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            ev = cal.upcoming(now)
        self.assertEqual(ev.summary, "Cerca")
        self.assertIn("familia%40group.calendar.google.com", seen["url"])
        self.assertIn("singleEvents=true", seen["url"])
        self.assertEqual(seen["auth"], "Bearer tok")

    def test_error_http_es_calendar_error(self):
        import urllib.error
        cal = self.make()
        cal._access = "tok"
        cal._expires = time.time() + 3600

        def boom(req, timeout=None):
            raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)

        with mock.patch("urllib.request.urlopen", boom):
            with self.assertRaises(CalendarError):
                cal.upcoming(1000)

    def test_token_cacheado_no_se_pide_dos_veces(self):
        cal = self.make()
        calls = {"n": 0}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                calls["n"] += 1
                return b'{"access_token":"a","expires_in":3600}'

        with mock.patch("urllib.request.urlopen", lambda req, timeout=None: FakeResp()):
            self.assertEqual(cal._token(), "a")
            self.assertEqual(cal._token(), "a")
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()

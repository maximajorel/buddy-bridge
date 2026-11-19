"""Tests del endpoint `/calendar` de punta a punta, con servidor real.

Un `curl` a mano verifica una vez; esto verifica siempre. Levanta el bridge en
un puerto libre con un `.env` temporal y le habla HTTP de verdad, que es la única
forma de cazar errores de `Content-Length`, auth o serialización.
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from buddy_bridge.config import Config
from buddy_bridge.face import Face
from buddy_bridge.events import EventLog
from buddy_bridge.http_api import serve
from buddy_bridge.service import BridgeService

ASK = "ask-token"
READ = "read-token"


class CalendarEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = Path(cls.tmp.name) / ".env"
        cls.env.write_text(f"BUDDY_TOKEN={READ}\nBUDDY_ASK_TOKEN={ASK}\n", encoding="utf-8")
        cls.config = Config.load(cls.env)
        cls.face = Face(EventLog(16))
        cls.service = BridgeService(cls.config, face=cls.face, log=lambda *_: None)
        cls.server = serve(cls.service, cls.config, address=("127.0.0.1", 0))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        # El rate-limit por IP es del server, no del test: los tests comparten
        # 127.0.0.1 y un 401 a propósito envenenaría los siguientes.
        self.service.auth_guard._fails.clear()

    def request(self, method, path, body=None, token=ASK):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    # ------------------------------------------------------------------ auth
    def test_get_sin_token_da_401(self):
        self.assertEqual(self.request("GET", "/calendar", token=None)[0], 401)

    def test_get_con_token_de_lectura_basta(self):
        # El token de lectura alcanza para MIRAR: no crea runs ni gasta nada.
        self.assertEqual(self.request("GET", "/calendar", token=READ)[0], 200)

    def test_post_exige_ask_token(self):
        # Escribir credenciales es tan sensible como crear un run.
        status, _ = self.request("POST", "/calendar", {}, token=READ)
        self.assertEqual(status, 401)

    # ---------------------------------------------------------------- lectura
    def test_get_devuelve_estado_sin_secretos(self):
        status, body = self.request("GET", "/calendar", token=READ)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("configured", data)
        self.assertIn("lead_min", data)
        self.assertNotIn("GOOGLE_CLIENT_SECRET", body)

    def test_pagina_html(self):
        status, body = self.request("GET", "/calendar_page", token=READ)
        self.assertEqual(status, 200)
        self.assertIn("<!doctype html", body.lower())
        self.assertIn("GOOGLE_CLIENT_ID", body)

    # --------------------------------------------------------------- escritura
    def test_post_incompleto_no_escribe(self):
        # El `.env` del server es compartido por toda la clase: se limpia antes
        # para que un "dos de tres" no se complete con lo que otro test dejó.
        self.request("POST", "/calendar", {"clear": True})
        status, body = self.request("POST", "/calendar", {"GOOGLE_CLIENT_ID": "solo-uno"})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))
        self.assertNotIn("GOOGLE_CLIENT_ID", self.env.read_text(encoding="utf-8"))

    def test_post_completo_escribe_y_recarga(self):
        status, body = self.request("POST", "/calendar", {
            "GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com",
            "GOOGLE_CLIENT_SECRET": "GOCSPX-1",
            "GOOGLE_REFRESH_TOKEN": "1//rt",
            "GOOGLE_CALENDAR_LEAD_MIN": "30",
        })
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["configured"])
        self.assertEqual(data["lead_min"], 30)
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("GOOGLE_CLIENT_ID=cid.apps.googleusercontent.com", text)
        # Y el servicio quedó apuntando al calendario sin reiniciar.
        self.assertIsNotNone(self.service.calendar)

    def test_borrar_limpia_credenciales(self):
        self.request("POST", "/calendar", {
            "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "s", "GOOGLE_REFRESH_TOKEN": "r"})
        status, body = self.request("POST", "/calendar", {"clear": True})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["configured"])
        self.assertIsNone(self.service.calendar)

    def test_json_invalido_da_400(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/calendar",
                                     data=b"{no json", method="POST")
        req.add_header("Authorization", f"Bearer {ASK}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()

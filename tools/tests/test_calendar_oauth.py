"""Tests del OAuth de Google Calendar dentro del bridge.

El flujo tiene tres promesas que no se pueden romper:

1. `/start` es escritura: sin token `ask` no arranca, y con el de lectura tampoco.
2. El `state` es de un solo uso y vence; el callback es público y esa es su única defensa.
3. El único guardado es `calendar_config.write_env`, con las tres claves juntas.

El canje contra Google se mockea siempre: ningún test sale a internet.
"""

import json
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from buddy_bridge import calendar_config
from buddy_bridge.config import Config
from buddy_bridge.events import EventLog
from buddy_bridge.face import Face
from buddy_bridge.http_api import (OAuthStateStore, oauth_redirect_uri, serve)
from buddy_bridge.service import BridgeService

READ = "read-token"
ASK = "ask-token"
CID = "123-abc.apps.googleusercontent.com"
SECRET = "GOCSPX-test-secret"
PUBLIC = "https://buddy.example.test/"
REDIRECT = "https://buddy.example.test/calendar/oauth/callback"
BASE_ENV = (f"BUDDY_TOKEN={READ}\nBUDDY_ASK_TOKEN={ASK}\n"
            f"BUDDY_PUBLIC_URL={PUBLIC}\n")


class OAuthCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = Path(cls.tmp.name) / ".env"
        cls.env.write_text(BASE_ENV, encoding="utf-8")
        cls.config = Config.load(cls.env)
        cls.face = Face(EventLog(16))
        cls.service = BridgeService(cls.config, face=cls.face, log=lambda *_: None)
        # El callback recarga el calendario en caliente. Ese cliente intentaría
        # hablar con Google en un hilo: acá no queremos red, ni siquiera de fondo.
        cls.patcher = unittest.mock.patch("buddy_bridge.service.make_calendar",
                                          return_value=None)
        cls.patcher.start()
        cls.server = serve(cls.service, cls.config, address=("127.0.0.1", 0))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.patcher.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.env.write_text(BASE_ENV, encoding="utf-8")
        self.service.auth_guard._fails.clear()
        with self.service.oauth_states._lock:
            self.service.oauth_states._states.clear()

    def request(self, method, path, body=None, token=None):
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

    def start(self, body=None, token=ASK):
        return self.request("POST", "/calendar/oauth/start", body=body, token=token)

    def start_and_state(self, body=None):
        status, text = self.start(body if body is not None
                                  else {"client_id": CID, "client_secret": SECRET})
        self.assertEqual(status, 200, text)
        url = json.loads(text)["authorize_url"]
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]

    def callback(self, state, code="code-123"):
        path = "/calendar/oauth/callback?" + urllib.parse.urlencode(
            {"state": state, "code": code})
        return self.request("GET", path)


class OAuthStartTest(OAuthCase):
    def test_start_sin_token_es_401(self):
        self.assertEqual(self.start(token=None)[0], 401)

    def test_start_con_token_de_lectura_es_401(self):
        # Leer el calendario no habilita conectar una cuenta.
        self.assertEqual(self.start(token=READ)[0], 401)

    def test_start_sin_client_id_es_400(self):
        status, text = self.start(body=None)
        self.assertEqual(status, 400)
        self.assertIn("client_id", json.loads(text)["error"])

    def test_start_devuelve_authorize_url_con_pkce(self):
        status, text = self.start({"client_id": CID, "client_secret": SECRET})
        self.assertEqual(status, 200)
        data = json.loads(text)
        url = data["authorize_url"]
        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["client_id"], [CID])
        self.assertEqual(query["redirect_uri"], [REDIRECT])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(len(query["code_challenge"][0]), 43)  # S256 sin padding
        self.assertTrue(query["state"][0])
        self.assertEqual(query["scope"],
                         ["https://www.googleapis.com/auth/calendar.readonly"])
        # El secret no viaja en la respuesta.
        self.assertNotIn(SECRET, text)

    def test_start_lee_credenciales_del_env(self):
        self.env.write_text(BASE_ENV + f"GOOGLE_CLIENT_ID={CID}\n"
                                       f"GOOGLE_CLIENT_SECRET={SECRET}\n",
                            encoding="utf-8")
        status, text = self.start(body=None)
        self.assertEqual(status, 200)
        url = json.loads(text)["authorize_url"]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["client_id"], [CID])

    def test_start_no_deja_credenciales_a_medias(self):
        # Las credenciales del body viven en el `state` hasta que el callback
        # trae el refresh token. El `.env` no puede quedar con dos de tres.
        self.start({"client_id": CID, "client_secret": SECRET})
        text = self.env.read_text(encoding="utf-8")
        self.assertNotIn("GOOGLE_CLIENT_ID", text)
        self.assertNotIn("GOOGLE_CLIENT_SECRET", text)


class OAuthCallbackTest(OAuthCase):
    def test_callback_sin_state_es_400(self):
        status, text = self.request("GET", "/calendar/oauth/callback?code=x")
        self.assertEqual(status, 400)
        self.assertIn("<html", text.lower())

    def test_callback_con_state_inventado_es_400(self):
        status, text = self.callback("basura")
        self.assertEqual(status, 400)
        self.assertIn("Enlace vencido", text)

    def test_state_de_un_solo_uso(self):
        state = self.start_and_state()
        with unittest.mock.patch("buddy_bridge.http_api._google_exchange_code",
                                 return_value={"refresh_token": "1//rt"}):
            first, _ = self.callback(state)
            second, body = self.callback(state)
        self.assertEqual(first, 200)
        self.assertEqual(second, 400)
        self.assertIn("Enlace vencido", body)

    def test_callback_feliz_guarda_las_tres_claves(self):
        state = self.start_and_state()
        with unittest.mock.patch("buddy_bridge.http_api._google_exchange_code",
                                 return_value={"refresh_token": "1//refresh-test",
                                               "access_token": "at"}) as exchange:
            status, body = self.callback(state)
        self.assertEqual(status, 200)
        self.assertIn("Calendario conectado", body)
        args = exchange.call_args.args
        self.assertEqual(args[0], CID)
        self.assertEqual(args[1], SECRET)
        self.assertEqual(args[2], "code-123")
        self.assertTrue(args[3])              # code_verifier
        self.assertEqual(args[4], REDIRECT)
        text = self.env.read_text(encoding="utf-8")
        self.assertIn(f"GOOGLE_CLIENT_ID={CID}", text)
        self.assertIn(f"GOOGLE_CLIENT_SECRET={SECRET}", text)
        self.assertIn("GOOGLE_REFRESH_TOKEN=1//refresh-test", text)
        self.assertTrue(self.service.config.calendar_enabled)
        self.assertNotIn(SECRET, body)

    def test_callback_sin_refresh_token_no_deja_el_calendario_configurado(self):
        state = self.start_and_state()
        with unittest.mock.patch("buddy_bridge.http_api._google_exchange_code",
                                 return_value={"access_token": "at"}):
            status, body = self.callback(state)
        self.assertEqual(status, 400)
        self.assertIn("refresh token", body.lower())
        text = self.env.read_text(encoding="utf-8")
        self.assertNotIn("GOOGLE_REFRESH_TOKEN", text)
        self.assertNotIn("GOOGLE_CLIENT_ID", text)
        self.assertNotIn("GOOGLE_CLIENT_SECRET", text)
        env = calendar_config.current_values(calendar_config.read_env_map(self.env))
        self.assertFalse(calendar_config.public_status(env)["configured"])

    def test_callback_si_el_canje_falla_no_guarda_nada(self):
        state = self.start_and_state()
        with unittest.mock.patch("buddy_bridge.http_api._google_exchange_code",
                                 side_effect=RuntimeError("boom")):
            status, body = self.callback(state)
        self.assertEqual(status, 400)
        self.assertNotIn("GOOGLE_REFRESH_TOKEN", self.env.read_text(encoding="utf-8"))


class CalendarStatusTest(OAuthCase):
    def test_calendar_expone_el_redirect(self):
        status, body = self.request("GET", "/calendar", token=READ)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["oauth_redirect"], REDIRECT)


class RedirectTest(unittest.TestCase):
    def test_default_de_produccion(self):
        self.assertEqual(oauth_redirect_uri(Config()),
                         "https://buddy.maximajorel.com.ar/calendar/oauth/callback")

    def test_public_url_pisa_el_default_y_normaliza_la_barra(self):
        cfg = Config(public_url="https://otro.example/")
        self.assertEqual(oauth_redirect_uri(cfg),
                         "https://otro.example/calendar/oauth/callback")


class StateStoreTest(unittest.TestCase):
    def test_un_solo_uso_y_ttl(self):
        now = [1000.0]
        store = OAuthStateStore(ttl=600, clock=lambda: now[0])
        store.put("s", {"verifier": "v"})
        self.assertEqual(store.pop("s")["verifier"], "v")
        self.assertIsNone(store.pop("s"))
        store.put("t", {"verifier": "v"})
        now[0] += 601
        self.assertIsNone(store.pop("t"))


if __name__ == "__main__":
    unittest.main()

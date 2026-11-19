"""Tests de `http_api` contra un servidor real en un puerto efímero.

Levanta `ThreadingHTTPServer` con un `BridgeService` falso (sin Hermes ni DB) para
fijar el contrato que consume la placa: códigos, auth por permiso y el JSON de
`/state`. Es la prueba que evita que un refactor cambie la forma de la respuesta
sin que el firmware se entere.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from buddy_bridge.config import Config
from buddy_bridge.events import EventLog
from buddy_bridge.face import Face
from buddy_bridge.http_api import AuthGuard, serve
from buddy_bridge.service import BridgeService


class FakeDb:
    def read(self):
        return None


class FakeHermes:
    def active_runs(self):
        return 0

    def create_run(self, prompt, session_id, button=None):
        if prompt == "explota":
            raise RuntimeError("boom")
        return {"run_id": "r1", "status": "queued"}

    def stream_run(self, run_id):
        return iter([{"event": "run.completed"}])


class ServerCase(unittest.TestCase):
    read_token = "tok-lectura"
    ask_token = "tok-ask"

    def setUp(self):
        self.config = Config(buddy_token=self.read_token, ask_token=self.ask_token,
                             events_wait_max=0.2)
        self.face = Face(EventLog(16), done_ttl=8.0)
        self.service = BridgeService(self.config, face=self.face, hermes=FakeHermes(),
                                     state_db=FakeDb(), log=lambda *_: None)
        # Puerto efímero: el servicio real ocupa 8787 en el LXC.
        self.server = serve(self.service, self.config, address=("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, path, method="GET", token=None, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            req.add_header("X-Buddy-Token", token)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")


class HealthTest(ServerCase):
    def test_health_sin_token(self):
        code, body = self.call("/health")
        self.assertEqual(code, 200)
        self.assertEqual(body["service"], "hermes-buddy-bridge")
        self.assertIn("version", body)


class StateTest(ServerCase):
    def test_state_sin_token_es_401(self):
        code, body = self.call("/state")
        self.assertEqual(code, 401)

    def test_state_con_token_trae_el_contrato(self):
        self.face.set_state("tool", "usando la terminal")
        code, body = self.call("/state", token="tok-lectura")
        self.assertEqual(code, 200)
        for key in ("state", "label", "phase", "led", "elapsed", "active", "run_id",
                    "ts", "since", "sleep_hint", "ev_seq"):
            self.assertIn(key, body)
        self.assertEqual(body["phase"], "busy")
        self.assertEqual(body["led"], [255, 190, 0])

    def test_state_acepta_bearer(self):
        req = urllib.request.Request(self.base + "/state")
        req.add_header("Authorization", "Bearer tok-lectura")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)

    def test_token_equivocado_es_401(self):
        self.assertEqual(self.call("/state", token="nope")[0], 401)

    def test_bloqueo_tras_cinco_fallos(self):
        for _ in range(5):
            self.call("/state", token="nope")
        self.assertEqual(self.call("/state", token="tok-lectura")[0], 429)


class EventsTest(ServerCase):
    def test_events_devuelve_lo_pendiente(self):
        self.face.set_state("thinking", "pensando")
        code, body = self.call("/events?since=0&wait=0", token="tok-lectura")
        self.assertEqual(code, 200)
        self.assertEqual(body[0]["event"], "state")

    def test_events_since_invalido_es_400(self):
        self.assertEqual(self.call("/events?since=x", token="tok-lectura")[0], 400)

    def test_events_wait_invalido_es_400(self):
        self.assertEqual(self.call("/events?wait=x", token="tok-lectura")[0], 400)

    def test_events_sin_token_es_401(self):
        self.assertEqual(self.call("/events")[0], 401)


class AskTest(ServerCase):
    def test_ask_sin_token_de_ask_es_401(self):
        # El token de lectura no habilita escritura.
        code, _ = self.call("/buddy/ask", "POST", token="tok-lectura", body={"prompt": "hola"})
        self.assertEqual(code, 401)

    def test_ask_valido_es_202(self):
        code, body = self.call("/buddy/ask", "POST", token="tok-ask", body={"prompt": "hola"})
        self.assertEqual(code, 202)
        self.assertEqual(body["run_id"], "r1")

    def test_prompt_vacio_es_400(self):
        code, _ = self.call("/buddy/ask", "POST", token="tok-ask", body={"prompt": "  "})
        self.assertEqual(code, 400)

    def test_button_invalido_es_400(self):
        code, _ = self.call("/buddy/ask", "POST", token="tok-ask",
                            body={"prompt": "hola", "button": "arriba"})
        self.assertEqual(code, 400)

    def test_json_invalido_es_400(self):
        req = urllib.request.Request(self.base + "/buddy/ask", data=b"{no",
                                     method="POST")
        req.add_header("X-Buddy-Token", "tok-ask")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("debería fallar")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_error_interno_es_502(self):
        code, body = self.call("/buddy/ask", "POST", token="tok-ask",
                               body={"prompt": "explota"})
        self.assertEqual(code, 502)
        self.assertIn("error", body)


class AskDisabledTest(ServerCase):
    ask_token = ""

    def test_ask_deshabilitado_sin_ask_token(self):
        code, _ = self.call("/buddy/ask", "POST", token="tok-lectura", body={"prompt": "hola"})
        self.assertEqual(code, 403)


class GuardTest(unittest.TestCase):
    def test_sin_token_de_lectura_abre_state(self):
        guard = AuthGuard(read_token="", ask_token="")
        self.assertTrue(guard.allows({}))

    def test_ask_nunca_abierto_sin_token(self):
        guard = AuthGuard(read_token="", ask_token="")
        self.assertFalse(guard.allows({}, need_ask=True))

    def test_bearer_y_header_propio_son_equivalentes(self):
        guard = AuthGuard("abc", "")
        self.assertTrue(guard.allows({"X-Buddy-Token": "abc"}))
        self.assertTrue(guard.allows({"Authorization": "Bearer abc"}))

    def test_cooldown_expira(self):
        now = [0.0]
        guard = AuthGuard("abc", "", cooldown=10, clock=lambda: now[0])
        for _ in range(5):
            guard.note_failure("1.2.3.4")
        self.assertTrue(guard.blocked("1.2.3.4"))
        now[0] = 11
        self.assertFalse(guard.blocked("1.2.3.4"))


if __name__ == "__main__":
    unittest.main()

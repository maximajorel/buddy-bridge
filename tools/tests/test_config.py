"""Tests de `config`: parseo del `.env`, precedencia y validación."""

import tempfile
import unittest
from pathlib import Path

from buddy_bridge.config import Config, ConfigError, parse_env_text, read_env_file


class ParseEnvTest(unittest.TestCase):
    def test_ignora_comentarios_y_lineas_vacias(self):
        parsed = parse_env_text("# comment\n\nBUDDY_PORT=8790\n   \n")
        self.assertEqual(parsed, {"BUDDY_PORT": "8790"})

    def test_saca_comillas(self):
        parsed = parse_env_text('A="hola mundo"\nB=\'otro\'\n')
        self.assertEqual(parsed, {"A": "hola mundo", "B": "otro"})

    def test_comentario_al_final_se_recorta(self):
        parsed = parse_env_text("BUDDY_PORT=8787   # el puerto\n")
        self.assertEqual(parsed, {"BUDDY_PORT": "8787"})

    def test_hash_dentro_de_comillas_se_respeta(self):
        parsed = parse_env_text('TOKEN="a#b"\n')
        self.assertEqual(parsed, {"TOKEN": "a#b"})

    def test_hash_pegado_no_es_comentario(self):
        parsed = parse_env_text("TOKEN=abc#def\n")
        self.assertEqual(parsed, {"TOKEN": "abc#def"})

    def test_acepta_export_y_espacios(self):
        parsed = parse_env_text("export BUDDY_PORT = 1234\n")
        self.assertEqual(parsed, {"BUDDY_PORT": "1234"})

    def test_valor_vacio_queda_vacio(self):
        self.assertEqual(parse_env_text("API_SERVER_KEY=\n"), {"API_SERVER_KEY": ""})


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        self.env.write_text(text, encoding="utf-8")

    def test_defaults_sin_archivo_ni_entorno(self):
        cfg = Config.load(self.env, environ={})
        self.assertEqual(cfg.port, 8787)
        self.assertEqual(cfg.hermes_url, "http://127.0.0.1:8642")
        self.assertEqual(cfg.buddy_session, "esp32-buddy")
        self.assertFalse(cfg.ask_enabled)

    def test_lee_del_archivo(self):
        self.write("BUDDY_PORT=8790\nBUDDY_TOKEN=secreto\nHERMES_URL=http://10.0.0.5:8642/\n")
        cfg = Config.load(self.env, environ={})
        self.assertEqual(cfg.port, 8790)
        self.assertEqual(cfg.buddy_token, "secreto")
        self.assertEqual(cfg.hermes_url, "http://10.0.0.5:8642")  # sin barra final

    def test_entorno_gana_sobre_archivo(self):
        self.write("BUDDY_PORT=8790\n")
        cfg = Config.load(self.env, environ={"BUDDY_PORT": "9000"})
        self.assertEqual(cfg.port, 9000)

    def test_variable_vacia_en_entorno_no_tapa_el_archivo(self):
        # `export X=` en la shell es "sin definir", no "borrar el archivo".
        self.write("BUDDY_TOKEN=del-archivo\n")
        cfg = Config.load(self.env, environ={"BUDDY_TOKEN": ""})
        self.assertEqual(cfg.buddy_token, "del-archivo")

    def test_puerto_invalido_reporta_error(self):
        with self.assertRaises(ConfigError) as ctx:
            Config.load(self.env, environ={"BUDDY_PORT": "no-es-numero"})
        self.assertIn("BUDDY_PORT", str(ctx.exception))

    def test_puerto_fuera_de_rango(self):
        with self.assertRaises(ConfigError):
            Config.load(self.env, environ={"BUDDY_PORT": "70000"})

    def test_url_sin_esquema_es_error(self):
        with self.assertRaises(ConfigError):
            Config.load(self.env, environ={"HERMES_URL": "127.0.0.1:8642"})

    def test_junta_varios_errores(self):
        with self.assertRaises(ConfigError) as ctx:
            Config.load(self.env, environ={"BUDDY_PORT": "x", "HERMES_URL": "nope"})
        self.assertEqual(len(ctx.exception.errors), 2)

    def test_ask_token_igual_al_de_lectura_es_error(self):
        with self.assertRaises(ConfigError):
            Config.load(self.env, environ={"BUDDY_TOKEN": "abc", "BUDDY_ASK_TOKEN": "abc"})

    def test_ask_habilitado_con_token_propio(self):
        cfg = Config.load(self.env, environ={"BUDDY_TOKEN": "abc", "BUDDY_ASK_TOKEN": "def"})
        self.assertTrue(cfg.ask_enabled)

    def test_redacted_oculta_secretos(self):
        cfg = Config.load(self.env, environ={"API_SERVER_KEY": "s3cr3t-0011",
                                             "BUDDY_TOKEN": "tok-lectura-0011"})
        shown = cfg.redacted()
        self.assertNotIn("s3cr3t-0011", str(shown))
        self.assertNotIn("tok-lectura-0011", str(shown))
        self.assertEqual(shown["api_server_key"], "<oculto>")

    def test_read_env_file_inexistente_es_vacio(self):
        self.assertEqual(read_env_file(Path(self.tmp.name) / "nope.env"), {})


if __name__ == "__main__":
    unittest.main()

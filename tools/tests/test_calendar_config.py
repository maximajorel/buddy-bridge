"""Tests del guardado de credenciales: el `.env` no se puede romper.

Escribir el `.env` de un servicio que se reinicia solo es la operación más
peligrosa del bridge: un archivo a medias deja la placa con cara de "sin
configurar" hasta que alguien entre por SSH. Estos tests fijan las tres promesas
del módulo: valida antes de tocar disco, escribe atómico y no pisa lo ajeno.
"""

import os
import tempfile
import unittest.mock
import unittest
from pathlib import Path

from buddy_bridge import calendar_config as cc


class ValidateTest(unittest.TestCase):
    def full(self, **over):
        base = {
            "GOOGLE_CLIENT_ID": "123-abc.apps.googleusercontent.com",
            "GOOGLE_CLIENT_SECRET": "GOCSPX-xyz",
            "GOOGLE_REFRESH_TOKEN": "1//0g-rt",
        }
        base.update(over)
        return base

    def test_trio_completo(self):
        out = cc.validate(self.full())
        self.assertEqual(out["GOOGLE_CALENDAR_ID"], "primary")
        self.assertEqual(out["GOOGLE_CALENDAR_LEAD_MIN"], "60")
        self.assertEqual(out["GOOGLE_CALENDAR_GRACE_MIN"], "5")

    def test_dos_de_tres_falla(self):
        values = self.full()
        values.pop("GOOGLE_REFRESH_TOKEN")
        with self.assertRaises(cc.ConfigWriteError) as ctx:
            cc.validate(values)
        self.assertIn("GOOGLE_REFRESH_TOKEN", str(ctx.exception))

    def test_secreto_vacio_conserva_el_guardado(self):
        current = {"GOOGLE_CLIENT_SECRET": "GOCSPX-viejo"}
        out = cc.validate({"GOOGLE_CLIENT_ID": "id", "GOOGLE_REFRESH_TOKEN": "rt"}, current)
        self.assertEqual(out["GOOGLE_CLIENT_SECRET"], "GOCSPX-viejo")

    def test_lead_fuera_de_rango(self):
        with self.assertRaises(cc.ConfigWriteError):
            cc.validate(self.full(GOOGLE_CALENDAR_LEAD_MIN="900"))
        with self.assertRaises(cc.ConfigWriteError):
            cc.validate(self.full(GOOGLE_CALENDAR_LEAD_MIN="0"))

    def test_lead_no_numerico(self):
        with self.assertRaises(cc.ConfigWriteError):
            cc.validate(self.full(GOOGLE_CALENDAR_LEAD_MIN="una hora"))

    def test_saltos_de_linea_rechazados(self):
        # Un `\n` inyectado en el valor agregaría variables arbitrarias al .env.
        with self.assertRaises(cc.ConfigWriteError):
            cc.validate(self.full(GOOGLE_CLIENT_ID="id\nBUDDY_TOKEN=hackeado"))

    def test_todo_vacio_es_apagar(self):
        out = cc.validate({})
        self.assertNotIn("GOOGLE_CLIENT_ID", out)


class MergeEnvTextTest(unittest.TestCase):
    def test_conserva_lineas_ajenas_comentarios_y_orden(self):
        existing = "# del dueño\nBUDDY_PORT=8787\nGOOGLE_CLIENT_ID=viejo\nAPI_SERVER_KEY=k\n"
        out = cc.merge_env_text(existing, {"GOOGLE_CLIENT_ID": "nuevo"})
        self.assertEqual(out, "# del dueño\nBUDDY_PORT=8787\nGOOGLE_CLIENT_ID=nuevo\nAPI_SERVER_KEY=k\n")

    def test_agrega_las_claves_nuevas(self):
        out = cc.merge_env_text("BUDDY_PORT=8787\n", {"GOOGLE_CLIENT_ID": "id"})
        self.assertIn("BUDDY_PORT=8787", out)
        self.assertIn("GOOGLE_CLIENT_ID=id", out)
        self.assertIn("# Google Calendar", out)

    def test_archivo_vacio(self):
        out = cc.merge_env_text("", {"GOOGLE_CLIENT_ID": "id"})
        self.assertTrue(out.startswith("# Google Calendar"))


class WriteEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_escribe_y_queda_legible(self):
        self.env.write_text("BUDDY_PORT=8787\n", encoding="utf-8")
        cc.write_env(self.env, {"GOOGLE_CLIENT_ID": "id", "GOOGLE_CALENDAR_ID": "primary"})
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("BUDDY_PORT=8787", text)
        self.assertIn("GOOGLE_CLIENT_ID=id", text)

    def test_permisos_600(self):
        cc.write_env(self.env, {"GOOGLE_CLIENT_ID": "id"})
        mode = os.stat(self.env).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_no_deja_temporales(self):
        cc.write_env(self.env, {"GOOGLE_CLIENT_ID": "id"})
        leftovers = [p.name for p in Path(self.tmp.name).iterdir() if p.name != ".env"]
        self.assertEqual(leftovers, [])

    def test_un_fallo_no_toca_el_archivo_viejo(self):
        self.env.write_text("BUDDY_PORT=8787\n", encoding="utf-8")
        before = self.env.read_text(encoding="utf-8")
        with unittest.mock.patch("os.replace", side_effect=OSError("disco lleno")):
            with self.assertRaises(OSError):
                cc.write_env(self.env, {"GOOGLE_CLIENT_ID": "id"})
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)
        leftovers = [p.name for p in Path(self.tmp.name).iterdir() if p.name != ".env"]
        self.assertEqual(leftovers, [])


class PublicStatusTest(unittest.TestCase):
    def test_nunca_devuelve_el_valor_del_secreto(self):
        env = {"GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "S3CR3T", "GOOGLE_REFRESH_TOKEN": "1//x"}
        status = cc.public_status(env)
        self.assertTrue(status["configured"])
        self.assertNotIn("S3CR3T", str(status))
        self.assertNotIn("1//x", str(status))
        self.assertTrue(status["has_client_secret"])

    def test_sin_credenciales(self):
        status = cc.public_status({})
        self.assertFalse(status["configured"])
        self.assertEqual(status["calendar_id"], "primary")


if __name__ == "__main__":
    unittest.main()

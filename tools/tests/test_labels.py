"""Tests de `labels`: la traducción pura de actividad a etiqueta de cara."""

import unittest

from buddy_bridge import labels


class LabelTest(unittest.TestCase):
    def test_etiqueta_de_tool_conocida(self):
        self.assertEqual(labels.tool_label("terminal"), "usando la terminal")

    def test_tool_desconocida_es_generica(self):
        self.assertEqual(labels.tool_label("tool_inventada"), labels.GENERIC_TOOL_LABEL)

    def test_sin_tool_es_generica(self):
        self.assertEqual(labels.tool_label(None), labels.GENERIC_TOOL_LABEL)

    def test_desc_vacia_es_trabajando(self):
        self.assertEqual(labels.label_for_desc(""), "trabajando")
        self.assertEqual(labels.label_for_desc(None), "trabajando")

    def test_tool_started_muestra_la_tool(self):
        self.assertEqual(labels.label_for_desc("tool started: terminal"),
                         "usando la terminal")

    def test_tool_completed_vuelve_a_pensar(self):
        self.assertEqual(labels.label_for_desc("tool completed: terminal (5.4s)"), "pensando")

    def test_pensando_y_respondiendo(self):
        self.assertEqual(labels.label_for_desc("Thinking"), "pensando")
        self.assertEqual(labels.label_for_desc("responding"), "respondiendo")

    def test_desc_con_tool_sin_marca(self):
        self.assertEqual(labels.label_for_desc("usando execute_code"), "corriendo código")

    def test_todas_las_etiquetas_entran_en_40(self):
        for label in list(labels.TOOL_LABELS.values()) + list(labels.DESC_LABELS.values()):
            self.assertLessEqual(len(label), labels.LABEL_MAX, label)

    def test_clip_corta_largo(self):
        self.assertEqual(len(labels.clip("x" * 100)), labels.LABEL_MAX)


if __name__ == "__main__":
    unittest.main()

"""Order text colour / font and general text fields (2026-10-15, blanket orders)."""
import unittest

import ai_mapping
import excel_data
import order_analysis
import order_pipeline
import templates
import text_rules

BLANKET = (
    "表面 1:\n"
    "Font: Comic Sans MS\n"
    "Text Color: Red (#E30200)\n"
    "Title: Mia's\n"
    "Select Size: 50×60 inch - Adult / Sofa Size\n"
    "CONFIRM CUSTOM INFORMATION: Confirm\n"
)
COUPLE = (
    "Customize\n"
    "Enter Name 01: Alice\n"
    "Enter Name 02: Tom\n"
    "Date: Since 2001\n"
    "Initial: W\n"
    "Name Color: #fff\n"
)


def _text(path, bbox, text=None):
    return {"path_parts": list(path), "name": path[-1], "kind": "type", "visible": True,
            "bbox": list(bbox), "text": text if text is not None else path[-1]}


def _box(path, bbox):
    return {"path_parts": list(path), "name": path[-1], "kind": "shape", "visible": False, "bbox": list(bbox)}


def _template(layers):
    return templates.Template(
        sku="SJS-T", name="SJS-T（50×60）", field_value="", path="/tmp/SJS-T（50×60）.psd",
        filename="SJS-T（50×60）.psd", all_layers=layers, width=4000, height=3480, size_key="50x60",
        structure_signature="sha256:t",
    )


def _blanket():
    return _template([
        {"path_parts": ["组 1"], "name": "组 1", "kind": "group", "visible": True, "bbox": [0, 0, 4000, 3480]},
        _text(["组 1", "Title"], [1068, 785, 3092, 1091], " Nora's"),
        _box(["组 1", "Title的文本框范围"], [962, 738, 3239, 1149]),
    ])


def _couple():
    return _template([
        _text(["Name A"], [100, 100, 900, 300], "Joe"),
        _text(["Name B"], [1100, 100, 1900, 300], "Ann"),
        _text(["year"], [600, 500, 1400, 700], "Since 1989"),
        _text(["Initial"], [900, 900, 1100, 1100], "J"),
    ])


def _row(raw):
    return excel_data.ExcelRow(row_no=2, sku="SJS-T", values={}, custom_info=raw)


def _plan(template, raw, profile=None):
    cfg = {"profile": profile} if profile is not None else {}
    return order_pipeline.compile_plan(template, _row(raw), cfg, "/tmp", strict=False)


def _analyze(template, raw, suggestion=None, profile=None):
    parsed = order_pipeline.parse_fields(raw)
    _, uncovered = order_analysis.field_coverage(template, parsed, profile)
    if suggestion is None:
        suggestion = order_analysis.local_suggestion(template, uncovered)
    candidate = order_analysis.merge_rules(template, profile, suggestion, uncovered)
    return candidate, order_analysis.check_candidate(template, _row(raw), {}, "/tmp", candidate)


class StyleFieldTests(unittest.TestCase):
    def test_style_kinds_and_hex(self):
        for label in ("Text Color", "Font Color", "Name Color", "Lettering Colour"):
            self.assertEqual(text_rules.style_kind(label), text_rules.STYLE_COLOR, label)
        for label in ("Font", "Choose Font", "Font Style"):
            self.assertEqual(text_rules.style_kind(label), text_rules.STYLE_FONT, label)
        # Plain "Color" may be the blanket colour: only the profile/AI decides.
        for label in ("Color", "Select Color", "Font Size", "Man's Hair Color", "Title"):
            self.assertEqual(text_rules.style_kind(label), "", label)
        self.assertEqual(text_rules.color_hex("Red (#E30200)"), "#E30200")
        self.assertEqual(text_rules.color_hex("White (#ffffff)"), "#FFFFFF")
        self.assertEqual(text_rules.color_hex("#fff"), "#FFFFFF")
        self.assertEqual(text_rules.color_hex("Light Pink"), "")
        self.assertEqual(text_rules.normalize_hex("ffb6c1"), "#FFB6C1")
        self.assertEqual(text_rules.normalize_hex("pink"), "")

    def test_blanket_order_colour_and_font_go_to_the_written_text(self):
        plan = _plan(_blanket(), BLANKET)
        self.assertTrue(plan["ok"], plan["errors"])
        [item] = plan["pathReplacements"]
        self.assertEqual(item["path"], ["组 1", "Title"])
        self.assertEqual(item["value"], "Mia's")
        self.assertEqual(item["color"], "#E30200")
        self.assertEqual(item["font"], "Comic Sans MS")
        self.assertIn("fitBox", item)

    def test_blanket_order_passes_local_analysis_without_ai(self):
        candidate, problems = _analyze(_blanket(), BLANKET)
        self.assertEqual(problems, [])
        covered, uncovered = order_analysis.field_coverage(
            _blanket(), order_pipeline.parse_fields(BLANKET.replace("Mia's", "Ella's")), candidate)
        self.assertEqual(uncovered, [])
        self.assertEqual({item["label"]: item["how"] for item in covered}["Text Color"], "style")

    def test_colour_name_needs_a_hex_value(self):
        raw = BLANKET.replace("Red (#E30200)", "Light Pink")
        candidate, problems = _analyze(_blanket(), raw)
        self.assertTrue(any("Light Pink" in item and "色值" in item for item in problems), problems)
        _, uncovered = order_analysis.field_coverage(_blanket(), order_pipeline.parse_fields(raw), candidate)
        self.assertEqual([(item["label"], item["kind"]) for item in uncovered], [("Text Color", "color")])
        ai = {"color_values": {"Light Pink": "#ffb6c1"}, "ignored_fields": []}
        suggestion, errors = ai_mapping.validate_mapping_suggestion(ai, _blanket())
        self.assertEqual(errors, [])
        candidate, problems = _analyze(_blanket(), raw, suggestion, candidate)
        self.assertEqual(problems, [])
        plan = _plan(_blanket(), raw.replace("Mia's", "Ella's"), candidate)
        self.assertEqual(plan["pathReplacements"][0]["color"], "#FFB6C1")

    def test_ignored_colour_field_keeps_template_colour(self):
        raw = BLANKET.replace("Red (#E30200)", "Light Pink")
        plan = _plan(_blanket(), raw, {"ignored_fields": ["Text Color"], "text_reviewed": True,
                                        "text_mappings": []})
        self.assertTrue(plan["ok"], plan["errors"])
        self.assertNotIn("color", plan["pathReplacements"][0])


class GeneralTextTests(unittest.TestCase):
    AI = {
        "text_mappings": [
            {"section": "Customize", "source_key": "Enter Name 01", "target_path": ["Name A"]},
            {"section": "Customize", "source_key": "Enter Name 02", "target_path": ["Name B"]},
            {"section": "Customize", "source_key": "Date", "target_path": ["year"]},
            {"section": "Customize", "source_key": "Initial", "target_path": ["Initial"]},
            {"section": "Customize", "source_key": "Name Color", "target_path": ["Initial"]},
        ],
        "ignored_fields": [],
    }

    def test_ai_maps_other_text_fields_and_colour_hits_names_only(self):
        template = _couple()
        suggestion, errors = ai_mapping.validate_mapping_suggestion(self.AI, template)
        # A colour field is never text to write.
        self.assertTrue(any("Name Color" in item for item in errors), errors)
        self.assertEqual({item["source_key"]: item["kind"] for item in suggestion["text_mappings"]}["Initial"],
                         text_rules.TEXT)
        candidate, problems = _analyze(template, COUPLE, suggestion)
        self.assertEqual(problems, [])
        plan = _plan(template, COUPLE.replace("W\n", "G\n"), candidate)
        self.assertTrue(plan["ok"], plan["errors"])
        by_path = {tuple(item["path"]): item for item in plan["pathReplacements"]}
        self.assertEqual(by_path[("Initial",)]["value"], "G")
        self.assertEqual(by_path[("year",)]["value"], "Since 2001")
        self.assertEqual(by_path[("Name A",)]["color"], "#FFFFFF")
        self.assertEqual(by_path[("Name B",)]["color"], "#FFFFFF")
        self.assertNotIn("color", by_path[("year",)])
        self.assertNotIn("color", by_path[("Initial",)])

    def test_unmapped_other_text_field_is_not_guessed(self):
        _, problems = _analyze(_couple(), COUPLE)
        self.assertTrue(any("Initial" in item for item in problems), problems)

    def test_ai_style_mapping_for_plain_color_with_targets(self):
        template = _couple()
        raw = COUPLE.replace("Name Color: #fff", "Color: Pink")
        ai = {**self.AI, "text_mappings": self.AI["text_mappings"][:4],
              "style_mappings": [{"section": "Customize", "source_key": "Color", "style": "color",
                                  "targets": [{"target_path": ["Initial"]}, {"target_path": ["Missing"]}]}],
              "color_values": {"Pink": "#FFC0CB"}}
        suggestion, errors = ai_mapping.validate_mapping_suggestion(ai, template)
        self.assertTrue(any("Missing" in item for item in errors), errors)
        candidate, problems = _analyze(template, raw, suggestion)
        self.assertEqual(problems, [])
        by_path = {tuple(item["path"]): item for item in _plan(template, raw, candidate)["pathReplacements"]}
        self.assertEqual(by_path[("Initial",)]["color"], "#FFC0CB")
        self.assertNotIn("color", by_path[("Name A",)])
        # Without that rule a plain "Color" is not treated as text colour.
        self.assertEqual(text_rules.style_fields(order_pipeline.parse_fields(raw)), [])


if __name__ == "__main__":
    unittest.main()

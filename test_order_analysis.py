import tempfile
import unittest

import ai_mapping
import excel_data
import order_analysis
import order_pipeline
from test_order_pipeline import _person_template

ORDER_1 = (
    "CUSTOMIZE MAN\nChoose the Chair: Walnut\nMan's  Hair Color: Gray\nMan's Hair-Gray: Gray_10\n"
    "Man's Beard: Black_04\n"
    "CUSTOMIZE WOMAN\nWoman's Hair Color: Brown\nWoman's Hair-Brown: Brown_07\n"
    "Customize Text Information\nChoose Style: Custom Metal Sign\nCustom Man's Name: Johnny\n"
    "Custom Woman's Name: Betty\nCustom Year: 1966\n"
    "Select Size\nCONFIRM CUSTOM INFORMATION: Confirm\n"
)
ORDER_2 = ORDER_1.replace("1966", "1996").replace("Johnny", "Pete").replace("Gray_10", "Gray_02") \
    .replace("Black_04", "Black_01").replace("Brown_07", "Brown_09")


def _text(name, bbox):
    return {"kind": "type", "path_parts": ["组 1", name], "name": name, "visible": True, "bbox": bbox, "text": name}


def _template():
    template = _person_template(extra_layers=())
    template.all_layers = [
        *template.all_layers,
        {"kind": "group", "path_parts": ["组 1"], "visible": True},
        _text("since  1984", [487, 293, 1504, 414]),
        _text("Jim", [175, 780, 339, 863]),
        _text("Darlene", [1469, 779, 1912, 842]),
    ]
    template.size_key = "30x20"
    template.width, template.height = 2000, 1338
    template.structure_signature = "sha256:t"
    return template


def _row(raw):
    return excel_data.ExcelRow(2, "SKU", custom_info=raw)


def _labels(items):
    return sorted(item["label"] for item in items)


AI_ANSWER = {
    "text_mappings": [
        {"section": "Customize Text Information", "source_key": "Custom Man's Name", "target_path": ["组 1", "Jim"], "confidence": 0.95},
        {"section": "Customize Text Information", "source_key": "Custom Woman's Name", "target_path": ["组 1", "Darlene"], "confidence": 0.95},
        {"section": "Customize Text Information", "source_key": "Custom Year", "target_path": ["组 1", "since  1984"], "confidence": 0.95},
    ],
    "person_style_mappings": [],
    "ignored_fields": ["Choose Style", "CONFIRM CUSTOM INFORMATION"],
    "unresolved_fields": [],
}


class PerOrderAnalysisTests(unittest.TestCase):
    def test_new_template_needs_text_and_option_fields_only(self):
        template = _template()
        covered, uncovered = order_analysis.field_coverage(template, order_pipeline.parse_fields(ORDER_1), None)
        # Person materials resolve by layer name: no AI needed for them.
        self.assertIn("Man's Hair-Gray", _labels(covered))
        self.assertEqual(_labels(uncovered), sorted([
            "CONFIRM CUSTOM INFORMATION", "Choose Style", "Custom Man's Name", "Custom Woman's Name", "Custom Year",
        ]))
        request = order_analysis.ai_request(template, order_pipeline.parse_fields(ORDER_1), covered, uncovered)
        self.assertEqual(len(request["customization_text"]), 11)  # the whole order goes to AI
        self.assertEqual(len(request["fields_to_map"]), 5)

    def test_ai_answer_passes_check_then_next_order_reuses_rules_without_ai(self):
        template = _template()
        suggestion, errors = ai_mapping.validate_mapping_suggestion(AI_ANSWER, template)
        self.assertEqual(errors, [])
        candidate = order_analysis.merge_rules(template, None, suggestion)
        self.assertEqual(order_analysis.check_candidate(template, _row(ORDER_1), {}, tempfile.gettempdir(), candidate), [])
        # Order 2: same fields, new values -> fully covered, just new values.
        _, uncovered = order_analysis.field_coverage(template, order_pipeline.parse_fields(ORDER_2), candidate)
        self.assertEqual(uncovered, [])
        plan = order_pipeline.compile_plan(template, _row(ORDER_2), {"profile": candidate}, tempfile.gettempdir(), strict=False)
        self.assertTrue(plan["ok"], plan["errors"])
        self.assertIn({"path": ["组 1", "since  1984"], "value": "since  1996"}, plan["pathReplacements"])
        self.assertIn({"path": ["组 1", "Jim"], "value": "Pete"}, plan["pathReplacements"])

    def test_wrong_ai_answer_does_not_pass(self):
        template = _template()
        wrong = {**AI_ANSWER, "text_mappings": [
            {"source_key": "Custom Year", "target_path": ["组 1", "Jim"]},  # year into a name layer
            *AI_ANSWER["text_mappings"][:2],
        ]}
        suggestion, errors = ai_mapping.validate_mapping_suggestion(wrong, template)
        self.assertTrue(any("年份字段" in item for item in errors))  # the wrong mapping is rejected
        parsed = order_pipeline.parse_fields(ORDER_1)
        _, uncovered = order_analysis.field_coverage(template, parsed, None)
        candidate = order_analysis.merge_rules(template, None, suggestion, uncovered)
        # The certain local rule fills the rejected year, so the saved rules are complete.
        year = [item for item in candidate["text_mappings"] if item["source_key"] == "Custom Year"]
        self.assertEqual(year[0]["target_path"], ["组 1", "since  1984"])
        self.assertEqual(year[0]["source"], "local")
        self.assertEqual(order_analysis.check_candidate(template, _row(ORDER_1), {}, tempfile.gettempdir(), candidate), [])

    def test_unmappable_text_field_goes_to_review(self):
        template = _template()
        template.all_layers = [layer for layer in template.all_layers if layer.get("kind") != "type"] + [
            _text("Jim", [175, 780, 339, 863]), _text("Darlene", [1469, 779, 1912, 842]),
        ]  # no year layer anywhere
        parsed = order_pipeline.parse_fields(ORDER_1)
        _, uncovered = order_analysis.field_coverage(template, parsed, None)
        candidate = order_analysis.merge_rules(template, None, order_analysis.local_suggestion(template, uncovered), uncovered)
        problems = order_analysis.check_candidate(template, _row(ORDER_1), {}, tempfile.gettempdir(), candidate)
        self.assertTrue(any("Custom Year" in item for item in problems), problems)

    def test_new_field_on_reviewed_template_is_the_only_open_item(self):
        template = _template()
        suggestion, _ = ai_mapping.validate_mapping_suggestion(AI_ANSWER, template)
        candidate = order_analysis.merge_rules(template, None, suggestion)
        order = ORDER_1 + "Select Size\nOther Special Requests: Please put us in jeans\n"
        _, uncovered = order_analysis.field_coverage(template, order_pipeline.parse_fields(order), candidate)
        self.assertEqual(_labels(uncovered), ["Other Special Requests"])
        signature = order_analysis.uncovered_signature(uncovered)
        self.assertEqual(signature, order_analysis.uncovered_signature(list(reversed(uncovered))))
        self.assertEqual(order_analysis.special_request_warnings(order_pipeline.parse_fields(order)),
                         ["客户特殊要求「Please put us in jeans」需人工确认"])

    def test_local_fallback_without_ai(self):
        template = _template()
        parsed = order_pipeline.parse_fields(ORDER_1 + "Upload Photo\nUpload Photo - Male: IMG_1.jpg\n")
        _, uncovered = order_analysis.field_coverage(template, parsed, None)
        suggestion = order_analysis.local_suggestion(template, uncovered)
        self.assertEqual(len(suggestion["text_mappings"]), 3)
        self.assertIn("Choose Style", suggestion["ignored_fields"])
        self.assertNotIn("Upload Photo - Male", suggestion["ignored_fields"])  # photos need a human

    def test_legacy_profiles_are_left_alone(self):
        self.assertFalse(order_analysis.is_rules_profile({"status": "approved"}))
        self.assertTrue(order_analysis.is_rules_profile({"rules_version": 2}))


if __name__ == "__main__":
    unittest.main()

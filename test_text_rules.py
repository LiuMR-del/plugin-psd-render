import unittest

import ai_mapping
import excel_data
import order_pipeline
import templates
import text_rules

COUPLE = (
    "Customize Text Information\n"
    "Custom Man's Name: Pete\n"
    "Custom Woman's Name: Sue\n"
    "Custom Year: 1992\n"
    "Choose Style: Custom Metal Sign\n"
)


def _text(path, bbox, text=None, visible=True):
    return {"path_parts": list(path), "name": path[-1], "kind": "type", "visible": visible,
            "bbox": list(bbox), "text": text if text is not None else path[-1]}


def _template(layers, width=2000):
    return templates.Template(
        sku="HJ-T", name="HJ-T", field_value="", path="/tmp/HJ-T(30-40).psd",
        filename="HJ-T(30-40).psd", all_layers=layers, width=width, height=1500, size_key="30x40",
    )


def _parsed(raw=COUPLE):
    return order_pipeline.parse_fields(raw)


def _row(raw=COUPLE):
    return excel_data.ExcelRow(row_no=2, sku="HJ-T", values={}, custom_info=raw)


class ClassifyTests(unittest.TestCase):
    def test_field_kinds(self):
        self.assertEqual(text_rules.classify_field("Custom Man's Name"), text_rules.NAME_MAN)
        self.assertEqual(text_rules.classify_field("Custom Woman's Name"), text_rules.NAME_WOMAN)
        self.assertEqual(text_rules.classify_field("Custom Name - Female"), text_rules.NAME_WOMAN)
        self.assertEqual(text_rules.classify_field("EST.Year"), text_rules.YEAR)
        self.assertEqual(text_rules.classify_field("Custom Date"), text_rules.YEAR)
        self.assertEqual(text_rules.classify_field("Enter family name"), text_rules.NAME)
        for label in ("Choose Style", "Customize the Characters' Look", "Select Size",
                      "CONFIRM CUSTOM INFORMATION", "Font Color", "Man's Hair Color"):
            self.assertEqual(text_rules.classify_field(label), "", label)

    def test_layer_role_uses_name_and_text(self):
        self.assertEqual(text_rules.layer_role({"name": "1994"}), text_rules.YEAR)
        self.assertEqual(text_rules.layer_role({"name": " 1995"}), text_rules.YEAR)
        self.assertEqual(text_rules.layer_role({"name": "since  1984"}), text_rules.YEAR)
        self.assertEqual(text_rules.layer_role({"name": "Year"}), text_rules.YEAR)
        self.assertEqual(text_rules.layer_role({"name": "Joe"}), text_rules.NAME)


class LocalRuleTests(unittest.TestCase):
    def test_year_without_since_and_left_right_names(self):
        # 916301(30-40): the year layer is named "1994"; names left/right.
        template = _template([
            _text(["1994"], [1059, 247, 1527, 399]),
            _text(["Joe"], [133, 685, 274, 752]),
            _text(["Heather"], [1586, 685, 1951, 736]),
        ])
        plan = text_rules.resolve_order_text(template, _parsed())
        self.assertEqual(plan["errors"], [])
        self.assertCountEqual(plan["main"], [
            {"path": ["1994"], "value": "1992"},
            {"path": ["Joe"], "value": "Pete"},
            {"path": ["Heather"], "value": "Sue"},
        ])

    def test_man_on_right_swaps_names(self):
        template = _template([
            {"path_parts": ["Woman素材"], "name": "Woman素材", "kind": "group", "visible": True, "bbox": [100, 0, 900, 900]},
            {"path_parts": ["Man素材"], "name": "Man素材", "kind": "group", "visible": True, "bbox": [1000, 0, 1900, 900]},
            _text(["Anna"], [100, 950, 400, 1000]),
            _text(["Tom"], [1500, 950, 1800, 1000]),
        ])
        plan = text_rules.resolve_order_text(template, _parsed("Custom Man's Name: Pete\nCustom Woman's Name: Sue"))
        self.assertIn({"path": ["Tom"], "value": "Pete"}, plan["main"])
        self.assertIn({"path": ["Anna"], "value": "Sue"}, plan["main"])

    def test_many_name_layers_are_not_guessed(self):
        # 908301(30-40): five overlapping visible names -> never guess.
        template = _template([
            _text(["Shane"], [984, 1142, 1241, 1195]), _text(["Hope"], [1500, 1142, 1718, 1195]),
            _text(["组 5", " James"], [981, 1091, 1224, 1161]),
            _text(["组 5", "BRIAN"], [1031, 1101, 1273, 1153]),
            _text(["组 5", "JOANN"], [1453, 1100, 1720, 1170]),
        ])
        plan = text_rules.resolve_order_text(template, _parsed())
        self.assertEqual(plan["main"], [])
        self.assertEqual(len(plan["errors"]), 3)
        self.assertIn("需在审核中指定", plan["errors"][0])

    def test_stray_visible_layer_does_not_receive_a_name(self):
        # 303(20-30): name group hidden, only a stray "uh" layer visible.
        template = _template([
            _text(["组 1", "Randel"], [75, 642, 437, 706], visible=False),
            _text(["组 1", "Dana"], [1611, 641, 1865, 704], visible=False),
            _text(["uh"], [593, 723, 672, 774]),
        ])
        plan = text_rules.resolve_order_text(template, _parsed())
        self.assertEqual(plan["main"], [])
        self.assertTrue(plan["errors"])

    def test_template_without_text_skips_the_order(self):
        # 2026-10-13: names/years baked into images would keep the sample names.
        plan = text_rules.resolve_order_text(_template([]), _parsed())
        self.assertEqual(plan["errors"], [text_rules.NO_TEXT_LAYERS])
        self.assertTrue(text_rules.lacks_text_layers(_template([]), _parsed()))
        self.assertFalse(text_rules.lacks_text_layers(_template([]), _parsed("Choose Style: Custom Metal Sign\n")))

    def test_smart_object_text_layers(self):
        smart = {
            "path_parts": ["文字"], "name": "文字", "kind": "smartobject", "visible": True,
            "smart_object": {"editable": True, "id": "so-1", "layers": [
                _text(["since 1998"], [400, 200, 1500, 360]),
                _text(["Frank"], [80, 630, 480, 700]), _text(["KATHY"], [1550, 630, 1960, 700]),
            ]},
        }
        plan = text_rules.resolve_order_text(_template([smart]), _parsed())
        self.assertEqual(plan["errors"], [])
        self.assertCountEqual(plan["smart"][("文字",)], [
            {"path": ["since 1998"], "value": "since 1992"},
            {"path": ["Frank"], "value": "Pete"},
            {"path": ["KATHY"], "value": "Sue"},
        ])


class ReviewedProfileTests(unittest.TestCase):
    def setUp(self):
        self.template = _template([
            _text(["Shane"], [984, 1142, 1241, 1195]), _text(["Hope"], [1500, 1142, 1718, 1195]),
            _text(["BRIAN"], [1031, 1101, 1273, 1153]),
        ])

    def _mapping(self, label, path):
        return {"source_key": label, "section": "Customize Text Information", "target_path": path}

    def test_reviewed_mapping_and_ignored_year(self):
        profile = {"text_reviewed": True, "ignored_fields": ["Custom Year"], "text_mappings": [
            self._mapping("Custom Man's Name", ["Shane"]), self._mapping("Custom Woman's Name", ["Hope"]),
        ]}
        plan = text_rules.resolve_order_text(self.template, _parsed(), profile)
        self.assertEqual(plan["errors"], [])
        self.assertEqual(plan["main"], [{"path": ["Shane"], "value": "Pete"}, {"path": ["Hope"], "value": "Sue"}])
        self.assertTrue(any("BRIAN" in item for item in plan["warnings"]))

    def test_year_into_name_layer_and_duplicate_target_are_blocked(self):
        profile = {"text_reviewed": True, "text_mappings": [
            self._mapping("Custom Man's Name", ["Shane"]), self._mapping("Custom Woman's Name", ["Shane"]),
            self._mapping("Custom Year", ["Hope"]),
        ]}
        plan = text_rules.resolve_order_text(self.template, _parsed(), profile)
        self.assertTrue(any("同一个图层" in item for item in plan["errors"]))
        self.assertTrue(any("不能写入姓名图层" in item for item in plan["errors"]))
        self.assertEqual(len(text_rules.check_mapping_roles(profile["text_mappings"], self.template)), 2)

    def test_ai_text_suggestion_with_wrong_role_is_dropped(self):
        suggestion = {"text_mappings": [
            {"source_key": "Custom Year", "target_path": ["Hope"], "confidence": 0.9},
            {"source_key": "Custom Man's Name", "target_path": ["Shane"], "confidence": 0.9},
        ]}
        normalized, errors = ai_mapping.validate_mapping_suggestion(suggestion, self.template)
        self.assertEqual([item["source_key"] for item in normalized["text_mappings"]], ["Custom Man's Name"])
        self.assertTrue(any("年份字段" in item for item in errors))


class CompilePlanTextTests(unittest.TestCase):
    def test_legacy_alias_cannot_double_write_a_name(self):
        # A stale SKU alias from another size must not put the man's name
        # into a second layer (or the year layer).
        template = _template([
            _text(["组 1", "since  1984"], [487, 293, 1504, 414]),
            _text(["组 1", "Jim"], [175, 780, 339, 863]),
            _text(["组 1", "Darlene"], [1469, 779, 1912, 842]),
        ])
        cfg = {"layer_aliases": {"since  1984": "Custom Man's Name", "Jim": "Custom Man's Name"}}
        plan = order_pipeline.compile_plan(template, _row(), cfg, "/tmp", strict=False)
        self.assertTrue(plan["ok"], plan["errors"])
        self.assertCountEqual(plan["pathReplacements"], [
            {"path": ["组 1", "since  1984"], "value": "since  1992"},
            {"path": ["组 1", "Jim"], "value": "Pete"},
            {"path": ["组 1", "Darlene"], "value": "Sue"},
        ])

    def test_unresolved_text_blocks_the_order(self):
        template = _template([_text(["uh"], [593, 723, 672, 774])])
        plan = order_pipeline.compile_plan(template, _row(), {}, "/tmp", strict=False)
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["pathReplacements"], [])


def _box(name, bbox):
    return {"path_parts": [name], "name": name, "kind": "shape", "visible": False, "bbox": list(bbox)}


class FitBoxTests(unittest.TestCase):
    # Layout of the redesigned 908303(20-30): hidden "...文本框范围" rectangles.
    LAYERS = [
        {"path_parts": ["year"], "name": "year", "kind": "group", "visible": True},
        _text(["year", "since 1989"], [440, 267, 1549, 419]),
        _text(["Man's Name"], [105, 630, 439, 705], text="lucas"),
        _text(["Woman's Name"], [1550, 632, 1876, 704], text="emma"),
        _box("Man's Name文本框范围", [67, 585, 465, 753]),
        _box("Wonman's Name文本框范围", [1533, 585, 1931, 753]),  # typo in the PSD
    ]

    def test_box_by_name_then_by_position(self):
        template = _template(self.LAYERS)
        self.assertEqual(text_rules.fit_box(template, [], ["Man's Name"]), [67, 585, 465, 753])
        self.assertEqual(text_rules.fit_box(template, [], ["Woman's Name"]), [1533, 585, 1931, 753])
        self.assertIsNone(text_rules.fit_box(template, [], ["year", "since 1989"]))

    def test_two_boxes_around_one_text_is_ambiguous(self):
        template = _template([_text(["Name"], [100, 100, 200, 150]),
                              _box("A范围", [0, 0, 300, 300]), _box("B范围", [50, 50, 250, 250])])
        self.assertIsNone(text_rules.fit_box(template, [], ["Name"]))

    def test_box_inside_smart_object(self):
        inner = [_text(["Name"], [10, 10, 60, 30]), _box("Name-范围", [0, 0, 100, 40])]
        template = _template([{"path_parts": ["SO"], "name": "SO", "kind": "smartobject", "visible": True,
                               "smart_object": {"editable": True, "layers": inner}}])
        self.assertEqual(text_rules.fit_box(template, ["SO"], ["Name"]), [0, 0, 100, 40])

    def test_plan_carries_fit_box_for_names_only(self):
        plan = order_pipeline.compile_plan(_template(self.LAYERS), _row(), {}, "/tmp", strict=False)
        self.assertTrue(plan["ok"], plan["errors"])
        by_path = {tuple(item["path"]): item for item in plan["pathReplacements"]}
        self.assertEqual(by_path[("Man's Name",)]["fitBox"],
                         {"left": 67, "top": 585, "right": 465, "bottom": 753, "maxScale": 1.3,
                          "syncGroup": "name"})
        self.assertEqual(by_path[("Woman's Name",)]["value"], "Sue")
        self.assertEqual(by_path[("Woman's Name",)]["fitBox"]["syncGroup"], "name")
        self.assertNotIn("fitBox", by_path[("year", "since 1989")])

    def test_year_box_is_not_synced_with_names(self):
        layers = [*self.LAYERS, _box("year范围", [400, 250, 1600, 440])]
        plan = order_pipeline.compile_plan(_template(layers), _row(), {}, "/tmp", strict=False)
        self.assertTrue(plan["ok"], plan["errors"])
        by_path = {tuple(item["path"]): item for item in plan["pathReplacements"]}
        self.assertEqual(by_path[("year", "since 1989")]["fitBox"]["left"], 400)
        self.assertNotIn("syncGroup", by_path[("year", "since 1989")]["fitBox"])


class YearTextTests(unittest.TestCase):
    def test_keeps_template_wording_around_the_year(self):
        # 2026-10-14: standard templates name the layer "year" but its text
        # is "Since 1989"; writing the bare value dropped "Since".
        self.assertEqual(text_rules.year_text("Since 1989", "1980"), "Since 1980")
        self.assertEqual(text_rules.year_text("since   2009", "1980"), "since   1980")
        self.assertEqual(text_rules.year_text("Since\r1989", "1980"), "Since\r1980")
        self.assertEqual(text_rules.year_text("EST. 1995", "June 1st 1985"), "EST. June 1st 1985")
        self.assertEqual(text_rules.year_text(" 1995", "1980"), "1980")
        self.assertEqual(text_rules.year_text("1991", "1980"), "1980")

    def test_value_with_own_wording_or_unclear_template_is_used_as_is(self):
        self.assertEqual(text_rules.year_text("Since 1989", "Since 1980"), "Since 1980")
        self.assertEqual(text_rules.year_text("Since 1989", "est 1980"), "est 1980")
        self.assertEqual(text_rules.year_text("1989 - 2019", "1980"), "1980")
        self.assertEqual(text_rules.year_text("Since", "1980"), "1980")
        self.assertEqual(text_rules.year_text("", "1980"), "1980")

    def test_standard_year_layer_keeps_since(self):
        template = _template([
            _text(["year"], [440, 267, 1549, 419], text="Since 1989"),
            _text(["Man's Name"], [105, 630, 439, 705], text="lucas"),
            _text(["Woman's Name"], [1550, 632, 1876, 704], text="emma"),
        ])
        plan = order_pipeline.compile_plan(template, _row(), {}, "/tmp", strict=False)
        self.assertTrue(plan["ok"], plan["errors"])
        self.assertIn({"path": ["year"], "value": "Since 1992"}, plan["pathReplacements"])


if __name__ == "__main__":
    unittest.main()

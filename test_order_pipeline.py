import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import excel_data
import order_pipeline
import text_rules


class OrderPipelineTests(unittest.TestCase):
    def test_parse_fields_preserves_sections_duplicates_and_empty_headings(self):
        parsed = order_pipeline.parse_fields(
            "CUSTOMIZE MAN\nName: Adam\nName: Jr\n\nCUSTOMIZE WOMAN\nName: Beth\n\nARBITRARY HEADING\n"
        )
        self.assertEqual(parsed.sections, ["CUSTOMIZE MAN", "CUSTOMIZE WOMAN", "ARBITRARY HEADING"])
        self.assertEqual([field.label for field in parsed], ["Name", "Name", "Name"])
        self.assertEqual([field.section for field in parsed], ["CUSTOMIZE MAN", "CUSTOMIZE MAN", "CUSTOMIZE WOMAN"])
        self.assertNotEqual(parsed[0].field_key, parsed[2].field_key)

    def test_materialize_scopes_duplicate_labels_and_keeps_all_values(self):
        row = excel_data.ExcelRow(2, "SKU", custom_info="MAN\nName: Adam\nWOMAN\nName: Beth")
        order_pipeline.materialize_fields(row)
        self.assertEqual(row.values[order_pipeline.serialize_field_key("MAN", "Name")], "Adam")
        self.assertEqual(row.values[order_pipeline.serialize_field_key("WOMAN", "Name")], "Beth")
        self.assertNotIn("Name", row.values)

        multi = order_pipeline.parse_fields("MAN\nName: Adam\nName: Jr")
        values = order_pipeline.materialize_field_values(multi)
        self.assertEqual(values[order_pipeline.serialize_field_key("MAN", "Name")], "Adam\nJr")

    def test_profile_to_legacy_configs_supports_historical_mapping_and_hide_no(self):
        bundle = order_pipeline.profile_to_legacy_configs({
            "selector_mappings": [{
                "source_key": "Man's Glasses",
                "smart_object_path": ["People"],
                "parent_path": ["Man", "Glasses"],
                "value_mapping": {"Yes": [["Man", "Glasses", "Yes"]]},
                "hide_values": ["No"],
            }],
            "text_mappings": [{"source_key": "Name", "target_path": ["Name"]}],
        })
        self.assertEqual(bundle["smart_object_configs"][0]["smart_path"], ["People"])
        selector = bundle["smart_object_configs"][0]["selectors"][0]
        self.assertEqual(selector["hide_values"], ["No"])
        self.assertEqual(bundle["layer_aliases"]["Name"], "Name")

    def test_compile_plan_selects_candidate_ancestors_and_hides_siblings(self):
        template = SimpleNamespace(
            sku="SKU", filename="x.psd", structure_signature="sig",
            all_layers=[
                {"kind": "group", "path_parts": ["Person"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "Hair"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "Hair", "Brown"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "Hair", "Black"], "visible": True},
                {"kind": "type", "path_parts": ["Name"], "visible": True},
            ],
        )
        row = excel_data.ExcelRow(2, "SKU", values={"Hair": "Brown", "Name": "Adam"})
        cfg = {"group_config": {"selectors": [{
            "parent": ["Person", "Hair"], "enabled": True, "column": "Hair",
            "candidates": [
                {"path": ["Person", "Hair", "Brown"], "value": "Brown"},
                {"path": ["Person", "Hair", "Black"], "value": "Black"},
            ], "relative_text_aliases": {}, "hide_values": ["No"],
        }]}}
        plan = order_pipeline.compile_plan(template, row, cfg, tempfile.gettempdir())
        self.assertTrue(plan["ok"], plan["errors"])
        self.assertIn({"path": ["Person", "Hair"], "visible": True}, plan["visibilityActions"])
        self.assertIn({"path": ["Person", "Hair", "Brown"], "visible": True}, plan["visibilityActions"])
        self.assertIn({"path": ["Person", "Hair", "Black"], "visible": False}, plan["visibilityActions"])
        self.assertEqual(plan["pathReplacements"], [{"path": ["Name"], "value": "Adam"}])

    def test_compile_plan_resolves_new_material_value_from_real_psd_scope(self):
        template = SimpleNamespace(
            sku="SKU", filename="x.psd", structure_signature="",
            all_layers=[
                {"kind": "group", "path_parts": ["Person", "7-Man's Top"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "7-Man's Top", "T-shirt"], "visible": True},
                {"kind": "smartobject", "path_parts": ["Person", "7-Man's Top", "T-shirt", "Top_Tshirt_03"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "7-Man's Top", "Sweater"], "visible": True},
                {"kind": "smartobject", "path_parts": ["Person", "7-Man's Top", "Sweater", "Top_Sweater_03"], "visible": True},
            ],
        )
        row = excel_data.ExcelRow(2, "SKU", values={"Man's Top": "Sweater_03"})
        cfg = {"group_config": {"selectors": [{
            "parent": ["Person", "7-Man's Top", "T-shirt"], "enabled": True,
            "column": "Man's Top", "candidates": [{
                "path": ["Person", "7-Man's Top", "T-shirt", "Top_Tshirt_03"], "value": "T-shirt_03",
            }],
        }]}}
        plan = order_pipeline.compile_plan(template, row, cfg, tempfile.gettempdir())
        self.assertTrue(plan["ok"], plan["errors"])
        actions = {tuple(item["path"]): item["visible"] for item in plan["visibilityActions"]}
        self.assertTrue(actions[("Person", "7-Man's Top", "Sweater", "Top_Sweater_03")])
        self.assertFalse(actions[("Person", "7-Man's Top", "T-shirt")])

    def test_compile_plan_uses_business_scope_when_parent_is_selected_branch(self):
        template = SimpleNamespace(
            sku="SKU", filename="x.psd", structure_signature="",
            all_layers=[
                {"kind": "group", "path_parts": ["Person"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "7-Man's Top"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "7-Man's Top", "Sweater"], "visible": True},
                {"kind": "smartobject", "path_parts": ["Person", "7-Man's Top", "Sweater", "Top_Sweater_04"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "7-Man's Top", "T-shirt"], "visible": True},
                {"kind": "smartobject", "path_parts": ["Person", "7-Man's Top", "T-shirt", "Top_Tshirt_03"], "visible": True},
            ],
        )
        row = excel_data.ExcelRow(2, "SKU", values={"Man's Top": "Sweater_04"})
        cfg = {"group_config": {"selectors": [{
            "parent": ["Person", "7-Man's Top", "Sweater"],
            "enabled": True,
            "column": "Man's Top",
            "candidates": [{
                "path": ["Person", "7-Man's Top", "Sweater", "Top_Sweater_04"],
                "value": "Sweater_04",
            }],
        }]}}
        plan = order_pipeline.compile_plan(template, row, cfg, tempfile.gettempdir())
        self.assertTrue(plan["ok"], plan["errors"])
        actions = {tuple(item["path"]): item["visible"] for item in plan["visibilityActions"]}
        self.assertTrue(actions[("Person", "7-Man's Top", "Sweater", "Top_Sweater_04")])
        self.assertFalse(actions[("Person", "7-Man's Top", "T-shirt")])
        self.assertFalse(actions[("Person", "7-Man's Top", "T-shirt", "Top_Tshirt_03")])

    def test_compile_plan_hides_entire_business_scope_for_no(self):
        template = SimpleNamespace(
            sku="SKU", filename="x.psd", structure_signature="",
            all_layers=[
                {"kind": "group", "path_parts": ["Person"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "3-Man's Hair Color"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "3-Man's Hair Color", "Brown"], "visible": True},
                {"kind": "smartobject", "path_parts": ["Person", "3-Man's Hair Color", "Brown", "Hair_Brown_03"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "3-Man's Hair Color", "Black"], "visible": True},
                {"kind": "smartobject", "path_parts": ["Person", "3-Man's Hair Color", "Black", "Hair_Black_01"], "visible": True},
            ],
        )
        row = excel_data.ExcelRow(2, "SKU", values={"Man's Hair Color": "No"})
        cfg = {"group_config": {"selectors": [{
            "parent": ["Person", "3-Man's Hair Color", "Brown"],
            "enabled": True,
            "column": "Man's Hair Color",
            "candidates": [{"path": ["Person", "3-Man's Hair Color", "Brown"], "value": "Brown"}],
        }]}}
        plan = order_pipeline.compile_plan(template, row, cfg, tempfile.gettempdir())
        self.assertTrue(plan["ok"], plan["errors"])
        actions = {tuple(item["path"]): item["visible"] for item in plan["visibilityActions"]}
        self.assertFalse(actions[("Person", "3-Man's Hair Color", "Brown")])
        self.assertFalse(actions[("Person", "3-Man's Hair Color", "Black")])
        self.assertFalse(actions[("Person", "3-Man's Hair Color", "Black", "Hair_Black_01")])

    def test_compile_plan_infers_no_parent_hide_when_mapping_is_missing(self):
        template = SimpleNamespace(
            sku="SKU", filename="x.psd", structure_signature="",
            all_layers=[
                {"kind": "group", "path_parts": ["Person"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "5-Man's Glasses"], "visible": True},
                {"kind": "smartobject", "path_parts": ["Person", "5-Man's Glasses", "Glasses_01"], "visible": True},
            ],
        )
        row = excel_data.ExcelRow(2, "SKU", values={"Man's Glasses": "No"})
        plan = order_pipeline.compile_plan(template, row, {}, tempfile.gettempdir())
        self.assertTrue(plan["ok"], plan["errors"])
        actions = {tuple(item["path"]): item["visible"] for item in plan["visibilityActions"]}
        self.assertFalse(actions[("Person", "5-Man's Glasses")])
        self.assertFalse(actions[("Person", "5-Man's Glasses", "Glasses_01")])

    def test_compile_plan_parent_no_blocks_stale_nested_value(self):
        template = SimpleNamespace(
            sku="SKU", filename="x.psd", structure_signature="",
            all_layers=[
                {"kind": "group", "path_parts": ["Person"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "3-Man's Hair Color"], "visible": True},
                {"kind": "group", "path_parts": ["Person", "3-Man's Hair Color", "Brown"], "visible": True},
                {"kind": "smartobject", "path_parts": ["Person", "3-Man's Hair Color", "Brown", "Hair_Brown_03"], "visible": True},
            ],
        )
        row = excel_data.ExcelRow(2, "SKU", values={"Man's Hair Color": "No", "Man's Hair-Brown": "Brown_03"})
        cfg = {"group_config": {"selectors": [
            {
                "parent": ["Person", "3-Man's Hair Color"],
                "enabled": True,
                "column": "Man's Hair Color",
                "candidates": [{"path": ["Person", "3-Man's Hair Color", "Brown"], "value": "Brown"}],
            },
            {
                "parent": ["Person", "3-Man's Hair Color", "Brown"],
                "enabled": True,
                "column": "Man's Hair-Brown",
                "candidates": [{"path": ["Person", "3-Man's Hair Color", "Brown", "Hair_Brown_03"], "value": "Brown_03"}],
            },
        ]}}
        plan = order_pipeline.compile_plan(template, row, cfg, tempfile.gettempdir())
        self.assertTrue(plan["ok"], plan["errors"])
        actions = {tuple(item["path"]): item["visible"] for item in plan["visibilityActions"]}
        self.assertFalse(actions[("Person", "3-Man's Hair Color")])
        self.assertFalse(actions[("Person", "3-Man's Hair Color", "Brown")])
        self.assertFalse(actions[("Person", "3-Man's Hair Color", "Brown", "Hair_Brown_03")])

    def test_compile_plan_validates_configured_paths_even_when_value_is_empty(self):
        template = SimpleNamespace(
            sku="SKU", filename="x.psd", structure_signature="",
            all_layers=[{"kind": "group", "path_parts": ["Person"], "visible": True}],
        )
        row = excel_data.ExcelRow(2, "SKU", values={"Hair": ""})
        cfg = {"group_config": {"selectors": [{
            "parent": ["Person"], "enabled": True, "column": "Hair",
            "candidates": [{"path": ["Person", "Missing"], "value": "Brown"}],
        }]}}
        plan = order_pipeline.compile_plan(template, row, cfg, tempfile.gettempdir())
        self.assertFalse(plan["ok"])
        self.assertTrue(any("候选路径不存在" in error for error in plan["errors"]))

    def test_compile_plan_rejects_unknown_material_value_and_signature(self):
        template = SimpleNamespace(
            sku="SKU", filename="x.psd", structure_signature="current",
            all_layers=[{"kind": "group", "path_parts": ["Person"], "visible": True}],
        )
        row = excel_data.ExcelRow(2, "SKU", values={"Hair": "Purple"})
        cfg = {"profile": {"status": "approved", "structure_signature": "old"}, "group_config": {"selectors": [{
            "parent": ["Person"], "enabled": True, "column": "Hair",
            "candidates": [{"path": ["Person", "Brown"], "value": "Brown"}],
        }]}}
        plan = order_pipeline.compile_plan(template, row, cfg, tempfile.gettempdir())
        self.assertFalse(plan["ok"])
        self.assertTrue(any("没有匹配候选" in error for error in plan["errors"]))
        self.assertTrue(any("结构签名" in error for error in plan["errors"]))

    def test_compile_plan_keeps_original_text_aliases_with_material_plan(self):
        template = SimpleNamespace(
            sku="SKU", filename="x.psd", structure_signature="",
            all_layers=[
                {"kind": "type", "path_parts": ["since  1994"], "visible": True},
                {"kind": "type", "path_parts": ["Walter"], "visible": True},
            ],
        )
        row = excel_data.ExcelRow(2, "SKU", values={"Custom Year": "1991", "Custom Man's Name": "Adam"})
        cfg = {
            "layer_aliases": {
                "since  1994": "Custom Year",
                "Walter": "Custom Man's Name",
            },
            "group_config": {"selectors": []},
        }
        plan = order_pipeline.compile_plan(template, row, cfg, tempfile.gettempdir())
        self.assertTrue(plan["ok"], plan["errors"])
        self.assertEqual(
            plan["pathReplacements"],
            [
                {"path": ["since  1994"], "value": "SINCE 1991"},
                {"path": ["Walter"], "value": "Adam"},
            ],
        )

    def test_compile_plan_merges_shared_smart_object_and_detects_conflict(self):
        smart = {
            "id": "shared", "editable": True,
            "layers": [{"kind": "type", "path_parts": ["Title"], "visible": True}],
        }
        template = SimpleNamespace(
            sku="SKU", filename="x.psd", structure_signature="",
            all_layers=[
                {"kind": "smartobject", "path_parts": ["A"], "visible": True, "smart_object": smart},
                {"kind": "smartobject", "path_parts": ["B"], "visible": True, "smart_object": smart},
            ],
        )
        row = excel_data.ExcelRow(2, "SKU", values={"Title": "Hello"})
        plan = order_pipeline.compile_plan(template, row, {}, tempfile.gettempdir())
        self.assertTrue(plan["ok"], plan["errors"])
        self.assertEqual(len(plan["smartObjectTextReplacements"]), 1)
        self.assertEqual(plan["smartObjectTextReplacements"][0]["smartObjectId"], "shared")


def _person_template(extra_layers=(), visible=True, hidden_copy=False):
    """Two-sided person tree in a smart object, following the PSD convention."""
    def g(*parts, visible=True):
        return {"kind": "group", "path_parts": list(parts), "visible": visible}

    def so(*parts, visible=False):
        return {"kind": "smartobject", "path_parts": list(parts), "visible": visible}

    hair = ("Man素材", "组 1", "3-Man's Hair Color")
    beard = ("Man素材", "组 1", "4-Man's Beard")
    whair = ("Woman素材", "组 2", "3-Woman's Hair Color")
    layers = [
        g("Man素材"), g("Man素材", "1-Chair"),
        so("Man素材", "1-Chair", "Brown", visible=True), so("Man素材", "1-Chair", "Walnut"),
        g("Man素材", "组 1"), g(*hair),
        g(*hair, "Black"), so(*hair, "Black", "Hair_Black_02", visible=True),
        g(*hair, "Gray", visible=False), so(*hair, "Gray", "Hair_Gray_02"), so(*hair, "Gray", "Hair_Gray_10"),
        g(*beard), g(*beard, "Black"), so(*beard, "Black", "Beard_Black_01", visible=True),
        so(*beard, "Black", "Beard_Black_04"),
        g("Woman素材"), g("Woman素材", "组 2"), g(*whair),
        g(*whair, "Brown", visible=False), so(*whair, "Brown", "Hair_Brown_07"), so(*whair, "Brown", "Hair_Brown_09"),
        g(*whair, "Red"), so(*whair, "Red", "Hair_Red_01", visible=True),
        *extra_layers,
    ]
    smart = {"id": "people", "editable": True, "layers": layers}
    all_layers = [{"kind": "smartobject", "path_parts": ["People"], "visible": visible, "smart_object": smart}]
    if hidden_copy:
        # 908303(30-40) style: a hidden duplicate next to the visible person object.
        copy_smart = {"id": "people-copy", "editable": True, "layers": layers}
        all_layers.append({"kind": "smartobject", "path_parts": ["People 拷贝"], "visible": False,
                           "smart_object": copy_smart})
    return SimpleNamespace(
        sku="SKU", filename="x.psd", structure_signature="", all_layers=all_layers,
    )


def _smart_actions(plan):
    return {
        tuple(item["path"]): item["visible"]
        for item in plan["smartObjectTextReplacements"][0]["visibilityActions"]
    }


class FieldLevelMaterialRuleTests(unittest.TestCase):
    """2026-10-12: hair/beard values must resolve without sample-learned rules."""

    def test_two_level_hair_and_numbered_beard_resolve_without_config(self):
        row = excel_data.ExcelRow(2, "SKU", custom_info=(
            "CUSTOMIZE MAN:\nChoose the Chair: Walnut\nMan's  Hair Color: Gray\n"
            "Man's Hair-Gray: Gray_10\nMan's Beard: Black_04\n"
            "CUSTOMIZE WOMAN:\nWoman's Hair Color: Brown\nWoman's Hair-Brown: Brown_07"
        ))
        plan = order_pipeline.compile_plan(_person_template(), row, {}, tempfile.gettempdir())
        self.assertTrue(plan["ok"], plan["errors"])
        actions = _smart_actions(plan)
        hair = ("Man素材", "组 1", "3-Man's Hair Color")
        self.assertTrue(actions[(*hair, "Gray")])
        self.assertTrue(actions[(*hair, "Gray", "Hair_Gray_10")])
        self.assertFalse(actions[(*hair, "Gray", "Hair_Gray_02")])
        self.assertFalse(actions[(*hair, "Black", "Hair_Black_02")])
        beard = ("Man素材", "组 1", "4-Man's Beard")
        self.assertTrue(actions[(*beard, "Black", "Beard_Black_04")])
        self.assertFalse(actions[(*beard, "Black", "Beard_Black_01")])
        self.assertTrue(actions[("Man素材", "1-Chair", "Walnut")])
        self.assertFalse(actions[("Man素材", "1-Chair", "Brown")])
        whair = ("Woman素材", "组 2", "3-Woman's Hair Color")
        self.assertTrue(actions[(*whair, "Brown")])
        self.assertTrue(actions[(*whair, "Brown", "Hair_Brown_07")])
        self.assertFalse(actions[(*whair, "Red", "Hair_Red_01")])

    def test_field_rule_overrides_stale_sample_selector(self):
        hair = ["Man素材", "组 1", "3-Man's Hair Color"]
        cfg = {"smart_object_configs": [{"smart_path": ["People"], "selectors": [{
            "parent": [*hair, "Black"], "enabled": True, "column": "Man's Hair-Black",
            "candidates": [{"path": [*hair, "Black", "Hair_Black_02"], "value": "Black_02"}],
        }, {
            "parent": hair, "enabled": True, "column": "Man's Hair Color",
            "candidates": [{"path": [*hair, "Black"], "value": "Black"}],
        }]}]}
        row = excel_data.ExcelRow(2, "SKU", custom_info=(
            "CUSTOMIZE MAN:\nMan's Hair Color: Gray\nMan's Hair-Gray: Gray_02"
        ))
        plan = order_pipeline.compile_plan(_person_template(), row, cfg, tempfile.gettempdir())
        self.assertTrue(plan["ok"], plan["errors"])
        actions = _smart_actions(plan)
        self.assertTrue(actions[(*hair, "Gray", "Hair_Gray_02")])
        self.assertFalse(actions[(*hair, "Black", "Hair_Black_02")])

    def test_branch_without_leaf_is_blocked_instead_of_bald(self):
        row = excel_data.ExcelRow(2, "SKU", custom_info="CUSTOMIZE WOMAN:\nWoman's Hair Color: Brown")
        plan = order_pipeline.compile_plan(_person_template(), row, {}, tempfile.gettempdir())
        self.assertFalse(plan["ok"])
        self.assertTrue(any("3-Woman's Hair Color" in error and "实际 0 个" in error for error in plan["errors"]), plan["errors"])

    def test_unknown_value_is_reported_not_left_on_default(self):
        row = excel_data.ExcelRow(2, "SKU", custom_info="CUSTOMIZE MAN:\nMan's Beard: Black_99")
        plan = order_pipeline.compile_plan(_person_template(), row, {}, tempfile.gettempdir())
        self.assertFalse(plan["ok"])
        self.assertTrue(any("Black_99" in error for error in plan["errors"]), plan["errors"])

    def test_no_hides_group_and_text_less_template_is_skipped(self):
        row = excel_data.ExcelRow(2, "SKU", custom_info=(
            "Customize Text Information:\nCustom Man's Name: Bob\nCustom Year: 1977\n"
            "CUSTOMIZE MAN:\nMan's Beard: No"
        ))
        plan = order_pipeline.compile_plan(_person_template(), row, {}, tempfile.gettempdir(), strict=False)
        self.assertFalse(_smart_actions(plan)[("Man素材", "组 1", "4-Man's Beard")])
        # No editable text layer for the name/year: the order must not render.
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["errors"], [text_rules.NO_TEXT_LAYERS])


class MaterialContainerVisibilityTests(unittest.TestCase):
    """2026-10-13: a hidden person smart object must fail the order, not render defaults."""

    CUSTOM = "CUSTOMIZE MAN:\nChoose the Chair: Walnut\nMan's Beard: Black_04"

    def test_hidden_person_object_fails_customized_order(self):
        row = excel_data.ExcelRow(2, "SKU", custom_info=self.CUSTOM)
        plan = order_pipeline.compile_plan(_person_template(visible=False), row, {}, tempfile.gettempdir())
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["errors"], [order_pipeline.MATERIAL_CONTAINER_HIDDEN.format(names="People")])

    def test_hidden_duplicate_next_to_visible_object_passes(self):
        row = excel_data.ExcelRow(2, "SKU", custom_info=self.CUSTOM)
        plan = order_pipeline.compile_plan(_person_template(hidden_copy=True), row, {}, tempfile.gettempdir())
        self.assertTrue(plan["ok"], plan["errors"])

    def test_order_without_character_fields_is_not_checked(self):
        row = excel_data.ExcelRow(2, "SKU", custom_info="Customize the Characters' Look: No\nSelect Size: 12*8 IN")
        plan = order_pipeline.compile_plan(_person_template(visible=False), row, {}, tempfile.gettempdir())
        self.assertTrue(plan["ok"], plan["errors"])


if __name__ == "__main__":
    unittest.main()

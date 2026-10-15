"""相框：文件名不带尺寸的模板对应这个 SKU 的所有尺寸（2026-10-15 用户需求）。"""
import unittest

import template_profiles
import templates


def _tpl(filename, sku="SJS-fbxk1"):
    parsed_sku, size_key, _, variant = templates.parse_template_filename(filename)
    assert parsed_sku == sku, parsed_sku
    return templates.Template(sku=sku, name=sku, field_value=variant, path="/lib/" + filename,
                              filename=filename, size_key=size_key)


class SizeFreeTemplateTests(unittest.TestCase):
    def test_template_without_size_serves_every_size_of_its_sku(self):
        frame = _tpl("SJS-fbxk1.psd")
        index = {"SJS-fbxk1": [frame]}
        for keys in (["40x60"], ["20x30", "8x12"], ["60x40"], "30x45"):
            self.assertEqual(templates.pick_by_size(index, "SJS-fbxk1", keys), [frame])
        self.assertEqual(templates.pick_by_size(index, "SJS-other", ["40x60"]), [])

    def test_sized_template_wins_over_size_free_one(self):
        sized = _tpl("SJS-fbxk1(40×60cm).psd")
        free = _tpl("SJS-fbxk1.psd")
        index = {"SJS-fbxk1": [sized, free]}
        self.assertEqual(templates.pick_by_size(index, "SJS-fbxk1", ["40x60"]), [sized])
        self.assertEqual(templates.pick_by_size(index, "SJS-fbxk1", ["20x30"]), [free])

    def test_variant_of_size_free_template(self):
        default = _tpl("SJS-fbxk1.psd")
        red = _tpl("SJS-fbxk1-[红色].psd")
        index = {"SJS-fbxk1": [default, red]}
        self.assertEqual(templates.pick_by_size(index, "SJS-fbxk1", ["40x60"], "红色"), [red])
        self.assertEqual(templates.pick_by_size(index, "SJS-fbxk1", ["40x60"], "蓝色"), [default])

    def test_profile_of_size_free_template_can_be_saved_and_found(self):
        frame = _tpl("SJS-fbxk1.psd")
        store = {"profiles": {}}
        template_profiles.put_profile(store, {
            "sku": frame.sku, "size_key": "", "template_filename": frame.filename, "status": "approved",
            "template_binding": {"absolute_path": frame.path},
        })
        found = template_profiles.get_profile(store, frame.sku, frame.size_key, frame.filename, frame.path)
        self.assertEqual(found["status"], "approved")
        with self.assertRaises(ValueError):
            template_profiles.put_profile(store, {"sku": "", "size_key": "", "template_filename": "x.psd"})


if __name__ == "__main__":
    unittest.main()

import glob
import os
import shutil
import tempfile
import unittest
from unittest import mock

import font_installer

SYSTEM_FONTS = sorted(glob.glob("/System/Library/Fonts/*.ttf") + glob.glob("C:/Windows/Fonts/*.ttf"))


@unittest.skipUnless(SYSTEM_FONTS, "需要一个系统 .ttf 字体做样本")
class FontInstallerTests(unittest.TestCase):
    def setUp(self):
        self.library = tempfile.mkdtemp(prefix="font-lib-")
        self.target = tempfile.mkdtemp(prefix="font-target-")
        os.makedirs(os.path.join(self.library, "字体"))
        os.makedirs(os.path.join(self.library, ".psd-render-thumbs"))
        self.font = os.path.join(self.library, "字体", "Sample(1).ttf")
        shutil.copy(SYSTEM_FONTS[0], self.font)
        shutil.copy(SYSTEM_FONTS[0], os.path.join(self.library, ".psd-render-thumbs", "x.ttf"))

    def tearDown(self):
        shutil.rmtree(self.library, ignore_errors=True)
        shutil.rmtree(self.target, ignore_errors=True)

    def test_finds_fonts_in_subfolders_but_not_cache_folders(self):
        self.assertEqual(font_installer.find_library_fonts(self.library), [self.font])
        self.assertTrue(font_installer.font_names(self.font))

    def test_installs_missing_font_once_and_never_overwrites(self):
        with mock.patch.object(font_installer, "user_font_dir", return_value=self.target), \
                mock.patch.object(font_installer, "installed_font_names", return_value=set()), \
                mock.patch.object(font_installer, "_register_windows_font"):
            first = font_installer.install_library_fonts(self.library)
            self.assertEqual(first["installed"], ["Sample(1).ttf"])
            self.assertTrue(os.path.exists(os.path.join(self.target, "Sample(1).ttf")))
            second = font_installer.install_library_fonts(self.library)
        # The same-named file already exists: a suffixed copy, no overwrite.
        self.assertEqual(second["installed"], ["Sample(1).ttf"])
        self.assertTrue(os.path.exists(os.path.join(self.target, "Sample(1)-psd-render.ttf")))

    def test_already_installed_font_is_skipped(self):
        names = font_installer.font_names(self.font)
        with mock.patch.object(font_installer, "user_font_dir", return_value=self.target), \
                mock.patch.object(font_installer, "installed_font_names", return_value=names):
            report = font_installer.install_library_fonts(self.library)
        self.assertEqual(report["present"], ["Sample(1).ttf"])
        self.assertEqual(os.listdir(self.target), [])


class _FakeTextLayer:
    def __init__(self, font_set, run_fonts, default_font=0):
        self.resource_dict = {
            "FontSet": [{"Name": name} for name in font_set],
            "StyleSheetSet": [{"StyleSheetData": {"Font": default_font}}],
            "TheNormalStyleSheet": 0,
        }
        self.engine_dict = {"StyleRun": {"RunArray": [
            {"StyleSheet": {"StyleSheetData": {} if index is None else {"Font": index}}} for index in run_fonts
        ]}}


def _type(path, fonts, visible=True):
    return {"name": path[-1], "path": "/".join(path), "path_parts": list(path), "kind": "type",
            "visible": visible, "fonts": list(fonts)}


class MissingFontTests(unittest.TestCase):
    """2026-10-14: check the fonts of the templates about to render, before starting."""

    def test_layer_fonts_use_style_runs_not_the_whole_font_set(self):
        import templates
        # FontSet lists the CJK default too, but only ArnoPro is used by the runs.
        layer = _FakeTextLayer(["AdobeHeitiStd-Regular", "ArnoPro-Bold", "AdobeInvisFont"], [1, 1, 2])
        self.assertEqual(templates._layer_fonts(layer), ["ArnoPro-Bold"])
        # A run without its own font falls back to the default style sheet.
        self.assertEqual(templates._layer_fonts(_FakeTextLayer(["A", "B"], [None], default_font=1)), ["B"])
        self.assertEqual(templates._layer_fonts(object()), [])

    def test_missing_fonts_for_visible_text_including_smart_objects(self):
        import templates
        smart = {"name": "SO", "path": "SO", "path_parts": ["SO"], "kind": "smartobject", "visible": True,
                 "smart_object": {"editable": True, "layers": [_type(["Title"], ["Nandaka-Western"])]}}
        template = templates.Template(
            sku="S", name="S", field_value="", path="/lib/S(30×40cm).psd", filename="S(30×40cm).psd",
            all_layers=[
                _type(["Man's Name"], ["AGaramond-Bold"]),
                _type(["Woman's Name"], ["AGaramond-Bold"]),
                _type(["year"], ["ArnoPro-Bold"]),
                _type(["Hidden"], ["SecretFont"], visible=False),
                smart,
            ],
        )
        result = font_installer.missing_template_fonts([template, template], installed={"arnopro-bold"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["template"], "S(30×40cm).psd")
        self.assertEqual(result[0]["missing"], {
            "AGaramond-Bold": ["Man's Name", "Woman's Name"],
            "Nandaka-Western": ["SO › Title"],
        })
        all_present = {"agaramond-bold", "arnopro-bold", "nandaka-western"}
        self.assertEqual(font_installer.missing_template_fonts([template], installed=all_present), [])


if __name__ == "__main__":
    unittest.main()

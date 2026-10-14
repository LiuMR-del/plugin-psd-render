import os
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

import library_cleanup
import template_profiles
import templates


def _template(directory, filename):
    return SimpleNamespace(sku="HJ-A", size_key="30x20", filename=filename, path=os.path.join(directory, filename))


def _put(store, path):
    template_profiles.put_profile(store, {
        "sku": "HJ-A", "size_key": "30x20", "template_filename": os.path.basename(path),
        "status": "approved", "structure_signature": "sha256:x",
        "template_binding": {"absolute_path": path},
    })


class LibraryCleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lib = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_lists_profiles_of_deleted_templates(self):
        store = {"profiles": {}}
        _put(store, os.path.join(self.lib, "HJ-A(30×20cm).psd"))
        _put(store, os.path.join(self.lib, "HJ-A(30×20cm)-旧.psd"))
        index = {"HJ-A": [_template(self.lib, "HJ-A(30×20cm).psd")]}
        found = library_cleanup.find_stale(self.lib, index, store, {})
        self.assertEqual([label for _, label in found["profiles"]], ["HJ-A(30×20cm)-旧.psd（approved）"])

    def test_moved_library_profile_is_still_in_use_and_deletable(self):
        store = {"profiles": {}}
        _put(store, "/other/machine/HJ-A(30×20cm).psd")
        template = _template(self.lib, "HJ-A(30×20cm).psd")
        self.assertEqual(library_cleanup.find_stale(self.lib, {"HJ-A": [template]}, store, {})["profiles"], [])
        self.assertTrue(template_profiles.delete_profile(store, "HJ-A", "30x20", template.filename, template.path))
        self.assertEqual(store["profiles"], {})

    def test_apply_deletes_only_confirmed_items_and_never_current_library(self):
        store = {"profiles": {}}
        _put(store, os.path.join(self.lib, "gone.psd"))
        other = os.path.join(self.lib, "其他库")
        os.makedirs(other)
        cfg = {"group_configs": {self.lib: {"HJ-A": {}}, other: {"HJ-B": {}}, "/missing/lib": {"HJ-C": {}}},
               "layer_aliases": {"/missing/lib": {"HJ-C": {}}}}
        found = library_cleanup.find_stale(self.lib, {}, store, cfg)
        self.assertEqual(found["missing_libraries"], ["/missing/lib"])
        self.assertEqual(found["other_libraries"], [other])
        removed = library_cleanup.apply(
            self.lib, store, cfg, profile_keys=[key for key, _ in found["profiles"]],
            libraries=["/missing/lib", self.lib],
        )
        self.assertEqual((removed["profiles"], removed["libraries"]), (1, 1))
        self.assertEqual(store["profiles"], {})
        self.assertEqual(set(cfg["group_configs"]), {self.lib, other})
        self.assertEqual(cfg["layer_aliases"], {})

    def test_scan_drops_thumbnails_of_missing_templates(self):
        thumbs = os.path.join(self.lib, templates.THUMB_DIRNAME)
        os.makedirs(thumbs)
        for name in ("old.png", "note.txt"):
            open(os.path.join(thumbs, name), "wb").close()
        templates.scan_templates(self.lib)
        self.assertEqual(os.listdir(thumbs), ["note.txt"])

    def test_unreadable_subfolder_keeps_thumbnails_and_marks_scan_incomplete(self):
        thumbs = os.path.join(self.lib, templates.THUMB_DIRNAME)
        os.makedirs(thumbs)
        open(os.path.join(thumbs, "old.png"), "wb").close()
        real_walk = os.walk

        def failing_walk(top, onerror=None, **kwargs):
            yield from real_walk(top, onerror=onerror, **kwargs)
            onerror(PermissionError(13, "Permission denied", os.path.join(top, "子目录")))

        with mock.patch("templates.os.walk", failing_walk):
            _, stats = templates.scan_templates(self.lib)
        self.assertFalse(stats["complete"])
        self.assertEqual(stats["errors"][0][0], "子目录")
        self.assertEqual(os.listdir(thumbs), ["old.png"])

    def test_same_name_copy_does_not_take_over_an_existing_templates_profile(self):
        root_psd = os.path.join(self.lib, "HJ-A(30×20cm).psd")
        open(root_psd, "wb").close()
        store = {"profiles": {}}
        _put(store, root_psd)
        copy = os.path.join(self.lib, "导入模板", "HJ-A(30×20cm).psd")
        self.assertIsNone(template_profiles.get_profile(store, "HJ-A", "30x20", "HJ-A(30×20cm).psd", copy))
        self.assertFalse(template_profiles.delete_profile(store, "HJ-A", "30x20", "HJ-A(30×20cm).psd", copy))
        self.assertEqual(len(store["profiles"]), 1)


if __name__ == "__main__":
    unittest.main()

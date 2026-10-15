"""模板总库：选外层文件夹，各品类放子文件夹（2026-10-15 用户需求）。"""
import json
import os
import tempfile
import unittest
from unittest import mock

import template_profiles
import templates


def _write(path, data=b"psd"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def _profile(path, status="approved"):
    return {
        "sku": "HJ-A", "size_key": "30x20", "template_filename": os.path.basename(path),
        "status": status, "structure_signature": "sha256:x",
        "template_binding": {"absolute_path": path},
    }


class MasterLibraryScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.calls = []
        self.thumbs = []

        def parse(path):
            self.calls.append(path)
            return {"text_layers": [], "all_layers": [], "width": 100, "height": 50}

        def thumb(path, thumb_path):
            self.thumbs.append(path)
            _write(thumb_path, b"png")
            return True

        patches = [mock.patch("templates._parse_psd", parse), mock.patch("templates._make_thumbnail", thumb)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_category_is_first_folder_under_library(self):
        _write(os.path.join(self.root, "铝板画", "HJ-A(30×20cm).psd"))
        _write(os.path.join(self.root, "毛毯", "旧款", "SJS-B（50×60）.psd"))
        _write(os.path.join(self.root, "HJ-C(30×40cm).psd"))
        index, stats = templates.scan_templates(self.root)
        self.assertEqual(index["HJ-A"][0].category, "铝板画")
        self.assertEqual(index["SJS-B"][0].category, "毛毯")
        self.assertEqual(index["HJ-C"][0].category, "")
        self.assertEqual(stats["categories"], {"": 1, "毛毯": 1, "铝板画": 1})

    def test_reuses_cache_and_thumbnails_of_category_folder_used_as_library(self):
        category = os.path.join(self.root, "铝板画")
        _write(os.path.join(category, "HJ-A(30×20cm).psd"))
        templates.scan_templates(category)
        self.assertEqual(len(self.calls), 1)
        self.calls.clear()
        self.thumbs.clear()

        index, stats = templates.scan_templates(self.root)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.thumbs, [])
        self.assertEqual(stats["cache_hits"], 1)
        thumb = index["HJ-A"][0].thumb
        self.assertTrue(thumb.startswith(os.path.join(self.root, templates.THUMB_DIRNAME)))
        self.assertTrue(os.path.isfile(thumb))
        with open(os.path.join(self.root, templates.CACHE_FILENAME), encoding="utf-8") as handle:
            entries = json.load(handle)["entries"]
        self.assertEqual(list(entries), ["铝板画/HJ-A(30×20cm).psd"])
        self.assertNotIn("_thumb_src", entries["铝板画/HJ-A(30×20cm).psd"])

    def test_changed_file_is_parsed_again_despite_category_cache(self):
        category = os.path.join(self.root, "铝板画")
        psd = os.path.join(category, "HJ-A(30×20cm).psd")
        _write(psd)
        templates.scan_templates(category)
        self.calls.clear()
        _write(psd, b"psd-changed")
        templates.scan_templates(self.root)
        self.assertEqual(self.calls, [psd])


class MergeNestedStoresTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.category = os.path.join(self.root, "铝板画")
        self.psd = os.path.join(self.category, "HJ-A(30×20cm).psd")
        _write(self.psd)
        store = template_profiles.load_store(self.category)
        self.key = template_profiles.put_profile(store, _profile(self.psd))
        template_profiles.save_store(self.category, store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_imports_category_profiles_and_leaves_source_untouched(self):
        source = template_profiles.profile_path(self.category)
        with open(source, encoding="utf-8") as handle:
            before = handle.read()
        self.assertEqual(template_profiles.merge_nested_stores(self.root), 1)
        store = template_profiles.load_store(self.root)
        found = template_profiles.get_profile(store, "HJ-A", "30x20", os.path.basename(self.psd), self.psd)
        self.assertEqual(found["status"], "approved")
        with open(source, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)
        self.assertEqual(template_profiles.merge_nested_stores(self.root), 0)

    def test_library_profile_wins_and_deleted_profile_is_not_imported_again(self):
        store = template_profiles.load_store(self.root)
        template_profiles.put_profile(store, _profile(self.psd, status="needs_review"))
        template_profiles.save_store(self.root, store)
        self.assertEqual(template_profiles.merge_nested_stores(self.root), 0)
        store = template_profiles.load_store(self.root)
        self.assertEqual(store["profiles"][self.key]["status"], "needs_review")

        self.assertTrue(template_profiles.delete_profile(
            store, "HJ-A", "30x20", os.path.basename(self.psd), self.psd))
        template_profiles.save_store(self.root, store)
        self.assertEqual(template_profiles.merge_nested_stores(self.root), 0)
        self.assertEqual(template_profiles.load_store(self.root)["profiles"], {})

    def test_changed_source_is_merged_again(self):
        template_profiles.merge_nested_stores(self.root)
        other = os.path.join(self.category, "HJ-A(30×40cm).psd")
        store = template_profiles.load_store(self.category)
        template_profiles.put_profile(store, {**_profile(other), "size_key": "30x40"})
        template_profiles.save_store(self.category, store)
        source = template_profiles.profile_path(self.category)
        stat = os.stat(source)
        os.utime(source, (stat.st_atime, stat.st_mtime + 5))
        self.assertEqual(template_profiles.merge_nested_stores(self.root), 1)
        self.assertEqual(len(template_profiles.load_store(self.root)["profiles"]), 2)


if __name__ == "__main__":
    unittest.main()

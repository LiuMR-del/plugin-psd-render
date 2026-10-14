import unittest

import template_profiles


class ProfileLookupTests(unittest.TestCase):
    def _store(self, *paths):
        store = {"profiles": {}}
        for path in paths:
            template_profiles.put_profile(store, {
                "sku": "HJ-A", "size_key": "30x20", "template_filename": "HJ-A(30-20).psd",
                "status": "approved", "structure_signature": "sha256:x",
                "template_binding": {"absolute_path": path},
            })
        return store

    def test_moved_library_still_finds_profile(self):
        store = self._store("/Users/dev/铁皮画/HJ-A(30-20).psd")
        found = template_profiles.get_profile(
            store, "HJ-A", "30x20", "HJ-A(30-20).psd", "/Users/operator/模板库/HJ-A(30-20).psd",
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["status"], "approved")

    def test_ambiguous_same_name_profiles_are_not_guessed(self):
        store = self._store("/lib/a/HJ-A(30-20).psd", "/lib/b/HJ-A(30-20).psd")
        self.assertIsNone(template_profiles.get_profile(
            store, "HJ-A", "30x20", "HJ-A(30-20).psd", "/other/HJ-A(30-20).psd",
        ))
        self.assertIsNotNone(template_profiles.get_profile(
            store, "HJ-A", "30x20", "HJ-A(30-20).psd", "/lib/b/HJ-A(30-20).psd",
        ))


if __name__ == "__main__":
    unittest.main()

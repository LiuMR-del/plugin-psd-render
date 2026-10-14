import os
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import local_candidates


class LocalCandidateTests(unittest.TestCase):
    def setUp(self):
        self.template = SimpleNamespace(all_layers=[
            {"kind": "pixel", "path_parts": ["背景"], "visible": True},
            {"kind": "group", "path_parts": ["Man素材", "7-Man's Top"], "visible": True},
            {"kind": "group", "path_parts": ["Man素材", "7-Man's Top", "Sweater"], "visible": True},
            {"kind": "smartobject", "path_parts": ["Man素材", "7-Man's Top", "Sweater", "Top_Sweater_04"], "visible": True},
            {"kind": "group", "path_parts": ["Man素材", "5-Man's Glasses"], "visible": True},
            {"kind": "smartobject", "path_parts": ["Man素材", "5-Man's Glasses", "Glasses_01"], "visible": True},
            {"kind": "type", "path_parts": ["Man素材", "Walter"], "visible": True},
        ])

    def test_package_excludes_background_and_text_layers(self):
        package = local_candidates.build_candidate_package(self.template, [
            {"section_identity": "CUSTOMIZE MAN", "source_key": "Man's Top", "value": "Sweater_04"},
            {"section_identity": "CUSTOMIZE MAN", "source_key": "Custom Man's Name", "value": "Adam"},
        ])
        dumped = str(package)
        self.assertNotIn("背景", dumped)
        self.assertNotIn("Walter", dumped)
        self.assertNotIn("Man's Name", str(package))
        self.assertNotIn("Walter", str(package))

    def test_no_is_resolved_locally_without_ai(self):
        package = local_candidates.build_candidate_package(self.template, [
            {"section_identity": "CUSTOMIZE MAN", "source_key": "Man's Glasses", "value": "No"},
        ])
        mappings, ambiguities = local_candidates.deterministic_mappings(package)
        self.assertFalse(ambiguities)
        self.assertTrue(mappings or ambiguities)
        if mappings:
            self.assertEqual(mappings[0]["value_mapping"], {"No": []})
            self.assertEqual(mappings[0]["hide_parent_values"], ["No"])

    def test_unknown_or_multiple_candidates_become_ambiguity(self):
        package = local_candidates.build_candidate_package(self.template, [
            {"section_identity": "CUSTOMIZE MAN", "source_key": "Man's Top", "value": "Unknown_99"},
        ])
        mappings, ambiguities = local_candidates.deterministic_mappings(package)
        self.assertFalse(mappings)
        self.assertTrue(ambiguities)


if __name__ == "__main__":
    unittest.main()

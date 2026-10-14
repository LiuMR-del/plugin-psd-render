import json
import tempfile
import unittest
from pathlib import Path

import ai_runtime


class AIRuntimeTests(unittest.TestCase):
    def test_legacy_provider_and_fingerprint_do_not_store_key(self):
        provider = ai_runtime.legacy_provider('https://example.test', 'secret-key', ['a'])
        self.assertEqual(provider['id'], 'legacy')
        self.assertNotEqual(ai_runtime.provider_fingerprint(provider['base_url'], 'secret-key'), 'secret-key')

    def test_runtime_round_trip_and_quota_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime.json'
            state = ai_runtime.load_runtime(path)
            provider = ai_runtime.legacy_provider('https://example.test', 'secret-key', ['a'])
            ai_runtime.mark_failure(state, provider, 'secret-key', 'quota_exhausted', 'quota exhausted')
            ai_runtime.save_runtime(state, path)
            loaded = ai_runtime.load_runtime(path)
            entry = loaded['providers']['legacy']
            self.assertEqual(entry['status'], 'quota_exhausted')
            self.assertTrue(ai_runtime.is_blocked(entry))
            self.assertNotIn('secret-key', path.read_text())

    def test_key_change_resets_provider_state(self):
        state = {'providers': {}}
        provider = ai_runtime.legacy_provider('https://example.test', 'one', ['a'])
        ai_runtime.mark_failure(state, provider, 'one', 'quota_exhausted')
        provider2 = ai_runtime.legacy_provider('https://example.test', 'two', ['a'])
        entry = ai_runtime.provider_state(state, provider2, 'two')
        self.assertEqual(entry['status'], 'unknown')
        self.assertNotEqual(entry['fingerprint'], ai_runtime.provider_fingerprint(provider['base_url'], 'one'))

    def test_normalize_profiles_strips_runtime_key(self):
        normalized = ai_runtime.normalize_profiles({
            'provider_order': ['backup'],
            'providers': {'backup': {'base_url': 'https://example.test', 'api_key': 'secret', 'models': ['m']}}
        })
        self.assertNotIn('api_key', normalized['providers']['backup'])


if __name__ == '__main__':
    unittest.main()

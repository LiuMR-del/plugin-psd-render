import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ai_client


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self):
        return None

    def read(self, _limit=None):
        return self.payload


class AIClientTests(unittest.TestCase):
    def test_mapping_payload_is_compact_and_keeps_high_reasoning(self):
        messages = ai_client.mapping_messages(
            sku="SKU", size_key="30x20", filename="x.psd",
            order_fields=[{"source_key": "Name", "values": ["Adam"]}],
            raw_customization_fields=[{
                "section_identity": "CUSTOMIZE MAN",
                "source_key": "Man's Glasses",
                "value": "No",
                "extra": "discarded",
            }],
            layer_tree=[{"path_parts": ["People"], "kind": "group", "visible": True}],
        )
        request = json.loads(messages[1]["content"])
        self.assertIn("customization_fields", request)
        self.assertIn("candidate_package", request)
        self.assertNotIn("order_fields", request)
        self.assertNotIn("raw_customization_fields", request)
        self.assertNotIn("psd_tree", request)
        self.assertNotIn("raw_field", json.dumps(request, ensure_ascii=False))

        response = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        with mock.patch.object(ai_client.urllib.request, "urlopen", return_value=FakeResponse(json.dumps(response).encode())) as opened:
            result = ai_client.request_json(
                base_url="https://example.test/v1", api_key="secret",
                messages=messages, models=["gpt-5.5"], timeout=1,
                total_timeout=2, retries=0,
            )
        sent = json.loads(opened.call_args.args[0].data)
        self.assertEqual(sent["reasoning_effort"], "high")
        self.assertNotIn("temperature", sent)
        self.assertEqual(result.payload, {"ok": True})

    def test_auth_error_does_not_retry_or_fallback(self):
        error = ai_client.urllib.error.HTTPError(
            "https://example.test/v1/chat/completions", 401, "unauthorized", {},
            FakeResponse(b"bad key"),
        )
        with mock.patch.object(ai_client.urllib.request, "urlopen", side_effect=error) as opened:
            with self.assertRaises(ai_client.AIClientError) as raised:
                ai_client.request_json(
                    base_url="https://example.test/v1", api_key="secret",
                    messages=[], models=["a", "b"], timeout=1,
                    total_timeout=2, retries=1,
                )
        self.assertEqual(raised.exception.kind, "auth")
        self.assertEqual(opened.call_count, 1)

    def test_shared_deadline_reaches_fallback_without_resetting_budget(self):
        calls = []
        response = {"choices": [{"message": {"content": '{"ok": true}'}}]}

        def fake_open(request, timeout):
            calls.append((json.loads(request.data)["model"], timeout))
            if len(calls) == 1:
                raise ai_client.urllib.error.URLError("temporary")
            return FakeResponse(json.dumps(response).encode())

        with mock.patch.object(ai_client.urllib.request, "urlopen", side_effect=fake_open):
            result = ai_client.request_json(
                base_url="https://example.test/v1", api_key="secret",
                messages=[], models=["a", "b"], timeout=1,
                total_timeout=2, retries=0,
            )
        self.assertEqual(result.model, "b")
        self.assertEqual([item[0] for item in calls], ["a", "b"])
        self.assertTrue(all(item[1] <= 1 for item in calls))

    def test_quota_exhaustion_does_not_fallback_to_other_models(self):
        error = ai_client.urllib.error.HTTPError(
            "https://example.test/v1/chat/completions", 429, "quota", {},
            FakeResponse(b'{"code":"API_KEY_QUOTA_EXHAUSTED","message":"quota exhausted"}'),
        )
        with mock.patch.object(ai_client.urllib.request, "urlopen", side_effect=error) as opened:
            with self.assertRaises(ai_client.AIClientError) as raised:
                ai_client.request_json(
                    base_url="https://example.test/v1", api_key="secret",
                    messages=[], models=["a", "b"], timeout=1,
                    total_timeout=2, retries=1,
                )
        self.assertEqual(raised.exception.kind, "quota_exhausted")
        self.assertEqual(opened.call_count, 1)

    def test_non_object_response_is_classified(self):
        response = FakeResponse(b"[]")
        with mock.patch.object(ai_client.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(ai_client.AIClientError) as raised:
                ai_client.request_json(
                    base_url="https://example.test/v1", api_key="secret",
                    messages=[], models=["a"], timeout=1,
                    total_timeout=2, retries=0,
                )
        self.assertEqual(raised.exception.kind, "invalid_response")


class ProviderFailoverTests(unittest.TestCase):
    def test_quota_exhausted_provider_fails_over_to_backup(self):
        calls = []
        ok = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        quota = ai_client.urllib.error.HTTPError(
            "https://primary/v1/chat/completions", 429, "quota", {},
            FakeResponse(b'{"code":"API_KEY_QUOTA_EXHAUSTED"}'),
        )

        def fake_open(request, timeout):
            payload = json.loads(request.data)
            calls.append((payload["model"], request.full_url))
            if request.full_url.startswith("https://primary"):
                raise quota
            return FakeResponse(json.dumps(ok).encode())

        providers = [
            {"id": "primary", "name": "Primary", "base_url": "https://primary/v1", "api_key": "a", "models": ["m1"], "enabled": True},
            {"id": "backup", "name": "Backup", "base_url": "https://backup/v1", "api_key": "b", "models": ["m2"], "enabled": True},
        ]
        with mock.patch.object(ai_client.urllib.request, "urlopen", side_effect=fake_open):
            with mock.patch.object(ai_client.ai_runtime, "load_runtime", return_value={"providers": {}}), \
                 mock.patch.object(ai_client.ai_runtime, "save_runtime"):
                result = ai_client.request_json_providers(providers=providers, messages=[], total_timeout=3, retries=0)
        self.assertEqual(result.model, "m2")
        self.assertEqual(result.provider_id, "backup")
        self.assertEqual(len(calls), 2)


class SingleApiTests(unittest.TestCase):
    """2026-10-14: only one API is kept; any failure is reported with its kind."""

    def test_provider_configs_ignore_old_backup_profiles(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = os.path.join(tmp, ".env")
            with open(env, "w", encoding="utf-8") as handle:
                handle.write("PSD_RENDER_AI_BASE_URL=https://main/v1\nPSD_RENDER_AI_API_KEY=k1\n"
                             "PSD_RENDER_AI_MODELS=m1,m2\nPSD_RENDER_AI_BACKUP_KEY=k2\n")
            cfg = {"ai_profiles": {"provider_order": ["legacy", "backup"], "providers": {
                "backup": {"id": "backup", "base_url": "https://backup/v1",
                           "api_key_env": "PSD_RENDER_AI_BACKUP_KEY", "models": ["b1"]}}}}
            with mock.patch.dict(os.environ, {}, clear=False):
                providers = ai_client.provider_configs(reload_env=True, env_path=env, settings_cfg=cfg)
                self.assertEqual([p["base_url"] for p in providers], ["https://main/v1"])
                self.assertEqual(providers[0]["models"], ["m1", "m2"])
                ai_client.save_environment_config("https://main/v1", "k1", ["m2"], env_path=env)
                with open(env, encoding="utf-8") as handle:
                    saved = handle.read()
                self.assertNotIn("BACKUP", saved)
                self.assertIn("PSD_RENDER_AI_MODELS=m2", saved)
                self.assertNotIn("PSD_RENDER_AI_BACKUP_KEY", os.environ)

    def test_rejected_response_format_is_dropped_and_retried(self):
        sent = []
        ok = {"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]}

        def fake_open(request, timeout):
            payload = json.loads(request.data)
            sent.append(payload)
            if "response_format" in payload:
                raise ai_client.urllib.error.HTTPError(
                    request.full_url, 400, "bad", {},
                    FakeResponse(b'{"error":{"message":"response_format json_object is not supported"}}'))
            return FakeResponse(json.dumps(ok).encode())

        with mock.patch.object(ai_client.urllib.request, "urlopen", side_effect=fake_open):
            result = ai_client.request_json(base_url="https://relay/v1", api_key="k", messages=[],
                                            models=["claude-sonnet-5-5-high"], timeout=1,
                                            total_timeout=2, retries=0)
        self.assertEqual(result.payload, {"ok": True})
        self.assertEqual(len(sent), 2)
        self.assertNotIn("response_format", sent[1])
        self.assertEqual(sent[1]["reasoning_effort"], "high")

    def test_single_api_failure_keeps_its_kind(self):
        auth = ai_client.urllib.error.HTTPError("https://relay/v1/chat/completions", 401, "no", {},
                                                FakeResponse(b'{"error":"invalid token"}'))
        providers = [{"id": "legacy", "name": "主 API", "base_url": "https://relay/v1", "api_key": "k",
                      "models": ["m1"], "enabled": True}]
        with mock.patch.object(ai_client.urllib.request, "urlopen", side_effect=auth), \
             mock.patch.object(ai_client.ai_runtime, "load_runtime", return_value={"providers": {}}), \
             mock.patch.object(ai_client.ai_runtime, "save_runtime"):
            with self.assertRaises(ai_client.AIClientError) as raised:
                ai_client.request_json_providers(providers=providers, messages=[], total_timeout=3, retries=0)
        self.assertEqual(raised.exception.kind, "auth")
        self.assertIn("AI 接口不可用", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

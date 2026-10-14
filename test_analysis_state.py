import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import template_profiles


class AnalysisStateTests(unittest.TestCase):
    def test_expired_analyzing_profile_can_be_retried(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).replace(microsecond=0).isoformat()
        profile = template_profiles.make_profile(
            sku="SKU", size_key="30x20", template_filename="x.psd",
            structure_signature="sig", analysis=template_profiles.make_analysis(
                state="analyzing", started_at=old, deadline_at=old,
                task_id="old-task",
            ),
        )
        status, reason = template_profiles.profile_status(profile, "sig")
        self.assertEqual(status, "needs_review")
        self.assertIn("超时", reason)

    def test_persisted_analyzing_profile_without_worker_is_restartable(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0).isoformat()
        profile = template_profiles.make_profile(
            sku="SKU", size_key="30x20", template_filename="x.psd",
            structure_signature="sig", analysis=template_profiles.make_analysis(
                state="analyzing", started_at=old, deadline_at="", task_id="stale-worker",
            ),
        )
        # The profile status remains reviewable; the scheduler owns the
        # distinction between a persisted marker and a live in-memory worker.
        status, reason = template_profiles.profile_status(profile, "sig")
        self.assertEqual(status, "needs_review")
        self.assertIn("正在分析", reason)

    def test_live_analyzing_profile_remains_in_progress(self):
        future = (datetime.now(timezone.utc) + timedelta(minutes=2)).replace(microsecond=0).isoformat()
        profile = template_profiles.make_profile(
            sku="SKU", size_key="30x20", template_filename="x.psd",
            structure_signature="sig", analysis=template_profiles.make_analysis(
                state="analyzing", deadline_at=future, task_id="live-task",
            ),
        )
        status, reason = template_profiles.profile_status(profile, "sig")
        self.assertEqual(status, "needs_review")
        self.assertIn("正在分析", reason)


if __name__ == "__main__":
    unittest.main()

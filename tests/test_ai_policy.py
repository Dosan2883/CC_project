import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import support
from fastapi import HTTPException

from app.ai.policy import ExternalAIPolicy
from app.ai.service import TranslationService
from app.chat.repository import MessageStore
from app.core import metrics
from app.core.database import SQLiteDatabase
from app.rooms.repository import RoomRepository


class ExternalAIPolicyTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = SQLiteDatabase(Path(directory) / "test.db")
        self.rooms = RoomRepository(self.db)
        for visibility in ("public", "private"):
            self.rooms.create({"roomId": visibility, "createdBy": "alice", "createdAt": 0,
                               "name": visibility, "key": visibility, "visibility": visibility})
        self.policy = ExternalAIPolicy(self.db, self.rooms, daily_limit=3)

    def test_consent_revocation_stops_new_calls_and_does_not_delete_cache(self):
        store = MessageStore(self.db, self.rooms)
        first = store.append("public", "alice", "첫 테스트 문장", "ko")["messageSeq"]
        second = store.append("public", "alice", "두 번째 테스트 문장", "ko")["messageSeq"]
        model = Mock()
        model.translate.return_value = "translated"
        service = TranslationService(self.db, self.rooms, model, policy=self.policy)
        with self.assertRaises(HTTPException) as error:
            service.translate_message("public", first, "en", "alice")
        self.assertEqual(error.exception.detail, "AI_CONSENT_REQUIRED")
        model.translate.assert_not_called()

        self.policy.set_consent("Alice", True)
        self.assertFalse(self.policy.consent("bob")["allowed"])
        service.translate_message("public", first, "en", "alice")
        self.policy.set_consent("alice", False)
        self.assertTrue(service.translate_message("public", first, "en", "alice")["cached"])
        with self.assertRaises(HTTPException) as error:
            service.translate_message("public", second, "en", "alice")
        self.assertEqual(error.exception.detail, "AI_CONSENT_REQUIRED")
        model.translate.assert_called_once()

    def test_private_rooms_require_operator_opt_in(self):
        self.policy.set_consent("alice", True)
        with self.assertRaises(HTTPException) as error:
            self.policy.authorize("alice", "private")
        self.assertEqual(error.exception.detail, "AI_PRIVATE_ROOM_DISABLED")
        ExternalAIPolicy(self.db, self.rooms, 3, allow_private=True).authorize("alice", "private")

    def test_daily_budget_is_shared_across_callers(self):
        other = ExternalAIPolicy(self.db, self.rooms, daily_limit=3)

        def attempt(policy):
            try:
                policy.reserve_call()
                return True
            except HTTPException as error:
                self.assertEqual((error.status_code, error.detail), (429, "AI_DAILY_CALL_LIMIT"))
                return False

        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            self.assertEqual(sum(pool.map(attempt, [self.policy, other] * 4)), 3)


class CloudWatchTests(unittest.TestCase):
    def test_flush_sends_raw_latency_samples_for_percentiles(self):
        client = Mock()
        with patch.object(metrics, "ENABLED", True), patch.object(metrics, "client", client):
            metrics.record("MessageDeliveryMs", 10, "Milliseconds")
            metrics.record("MessageDeliveryMs", 200, "Milliseconds")
            metrics.flush()
            metrics.flush()
        client.put_metric_data.assert_called_once_with(Namespace=metrics.NAMESPACE, MetricData=[
            {"MetricName": "MessageDeliveryMs", "Unit": "Milliseconds", "Values": [10, 200], "Dimensions": []},
        ])

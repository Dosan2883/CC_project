import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import support
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from moto import mock_aws

from app.chat.repository import MessageStore
from app.core.database import SQLiteDatabase
from app.media.service import LeaseLost, MediaError, MediaService
from app.media.storage import LocalStorage, S3Storage
from app.rooms.repository import RoomRepository
from test_persistence import create_room


class WorkerChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = SQLiteDatabase(Path(self.temp.name) / "test.sqlite3")
        self.rooms = RoomRepository(self.db)
        create_room(self.rooms)
        self.storage = LocalStorage(Path(self.temp.name) / "files")
        self.service = MediaService(self.storage, lambda *_: None, self.db, self.rooms)

    def upload(self):
        upload = self.service.request_upload("room", "owner", "demo.txt", 5)
        token = parse_qs(urlparse(upload["uploadUrl"]).query)["token"][0]
        self.service.receive_upload(upload["mediaId"], token, b"hello")
        self.service.start_job(upload["mediaId"])
        return upload["mediaId"]

    def test_new_worker_recovers_pending_job_and_duplicate_delivery(self):
        media_id = self.upload()
        reopened = SQLiteDatabase(self.db.path)
        worker = MediaService(self.storage, lambda *_: None, reopened, RoomRepository(reopened))
        self.assertTrue(worker.work(media_id))
        key = worker.asset(media_id)["transcriptKey"]
        self.assertEqual(worker.transcript(media_id)["pages"], ["hello"])
        self.assertTrue(worker.work(media_id))
        self.assertEqual(worker.asset(media_id)["transcriptKey"], key)

    def test_expired_lease_is_reclaimed_and_old_worker_cannot_commit(self):
        media_id = self.upload()
        self.db.update("MEDIA_JOBS", media_id, lambda value: {**value, "status": "PROCESSING", "leaseToken": "stopped-worker", "leaseUntil": time.time() - 1, "attempts": 1})
        self.assertTrue(self.service.work(media_id))
        with self.assertRaises(LeaseLost):
            self.service.finish(media_id, "stopped-worker", self.service.asset(media_id), {"pages": ["stale"], "needsOcr": False}, "bad-key")
        self.assertEqual(self.service.transcript(media_id)["pages"], ["hello"])

    def test_retry_dead_letter_state_and_manual_reprocessing(self):
        media_id = self.upload()
        with self.assertLogs("app.media.service", level="ERROR"), patch("app.media.service.extract", side_effect=OSError("transient")):
            for _ in range(3):
                self.assertFalse(self.service.work(media_id))
                self.db.update("MEDIA_JOBS", media_id, lambda value: {**value, "availableAt": 0})
        job = self.db.get("MEDIA_JOBS", media_id).value
        self.assertEqual((job["status"], job["attempts"], job["deadLetter"]), ("FAILED", 3, True))
        self.service.start_job(media_id)
        self.assertTrue(self.service.work(media_id))
        self.assertEqual(self.service.status(media_id)["status"], "COMPLETED")

    def test_deleted_room_cancels_pending_job(self):
        media_id = self.upload()
        self.rooms.delete("room", "owner")
        self.assertTrue(self.service.work(media_id))
        self.assertEqual(self.service.status(media_id)["status"], "CANCELLED")
        self.assertIsNone(self.service.asset(media_id)["transcriptKey"])

    def test_large_audio_and_video_upload_and_legacy_original_migration(self):
        for ext, signature in (("mp3", b"ID3"), ("mp4", b"0000ftyp")):
            with self.subTest(extension=ext):
                payload = signature.ljust(21 * 1024 * 1024, b"\0")
                upload = self.service.request_upload("room", "owner", f"sample.{ext}", len(payload))
                media_id = upload["mediaId"]
                if self.storage.direct_upload:
                    self.storage.put(self.service.asset(media_id)["s3Key"], payload)
                    self.service.complete_upload(media_id, "owner")
                else:
                    token = parse_qs(urlparse(upload["uploadUrl"]).query)["token"][0]
                    self.service.receive_upload(media_id, token, payload)
                self.assertEqual(self.service.original(media_id), payload)
                legacy_key = "original/" + media_id
                self.storage.put(legacy_key, payload)
                self.db.update("MEDIA", "ASSET#" + media_id, lambda asset: {**asset, "s3Key":legacy_key})
                self.assertEqual(self.service.original(media_id), payload)
                new_key = self.service.asset(media_id)["s3Key"]
                self.assertNotEqual(new_key, legacy_key)
                self.assertEqual(self.storage.get(new_key), payload)

    def test_pending_index_avoids_historical_queries_after_backfill(self):
        self.service.queue.recover()
        media_id = self.upload()
        with patch.object(self.db, "query", wraps=self.db.query) as query:
            self.service.queue.recover()
            self.service.queue.recover()
        self.assertTrue(all(call.args[0] != "MEDIA_JOBS" for call in query.call_args_list))
        self.assertIsNone(self.db.get("PENDING_JOBS#MEDIA_JOBS", media_id))
        self.assertEqual(self.service.status(media_id)["status"], "COMPLETED")

    def test_explicit_extraction_is_durable_even_if_queue_send_fails(self):
        upload = self.service.request_upload("room", "owner", "demo.txt", 5)
        media_id = upload["mediaId"]
        with self.assertLogs("app.media.service", level="ERROR"), patch.object(self.service.queue, "send", side_effect=RuntimeError("queue unavailable")):
            if self.storage.direct_upload:
                self.storage.client.put_object(Bucket=self.storage.bucket, Key=self.service.asset(media_id)["s3Key"], Body=b"hello", IfNoneMatch="*")
                self.service.complete_upload(media_id, "owner")
            else:
                token = parse_qs(urlparse(upload["uploadUrl"]).query)["token"][0]
                self.service.receive_upload(media_id, token, b"hello")
            self.assertIsNone(self.db.get("MEDIA_JOBS", media_id))
            self.service.start_job(media_id)
        self.assertEqual(self.db.get("MEDIA_JOBS", media_id).value["status"], "PENDING")
        self.assertEqual(len(MessageStore(self.db, self.rooms).after("room", 0)), 1)
        reopened = SQLiteDatabase(self.db.path)
        recovered = MediaService(self.storage, lambda *_: None, reopened, RoomRepository(reopened))
        recovered.queue.recover()
        self.assertEqual(recovered.status(media_id)["status"], "COMPLETED")


class AWSMediaChecks(WorkerChecks):
    def setUp(self):
        super().setUp()
        context = mock_aws()
        context.start()
        self.addCleanup(context.stop)
        self.s3 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
        self.s3.create_bucket(Bucket="test-messenger-bucket")
        self.storage = S3Storage("test-messenger-bucket", self.s3)
        self.service = MediaService(self.storage, lambda *_: None, self.db, self.rooms)

    def upload(self):
        upload = self.service.request_upload("room", "owner", "demo.txt", 5)
        asset = self.service.asset(upload["mediaId"])
        self.s3.put_object(Bucket=self.storage.bucket, Key=asset["s3Key"], Body=b"hello", IfNoneMatch="*")
        self.service.complete_upload(upload["mediaId"], "owner")
        self.service.start_job(upload["mediaId"])
        return upload["mediaId"]

    def test_signed_direct_upload_is_immutable_and_completion_is_idempotent(self):
        upload = self.service.request_upload("room", "owner", "demo.txt", 5)
        query = parse_qs(urlparse(upload["uploadUrl"]).query)
        headers = query["X-Amz-SignedHeaders"][0]
        self.assertIn("if-none-match", headers)
        self.assertIn("content-length", headers)
        self.assertEqual(upload["headers"]["If-None-Match"], "*")
        media_id = upload["mediaId"]
        with self.assertRaises(MediaError) as missing:
            self.service.complete_upload(media_id, "owner")
        self.assertEqual(missing.exception.code, "UPLOAD_NOT_FOUND")
        key = self.service.asset(media_id)["s3Key"]
        self.s3.put_object(Bucket=self.storage.bucket, Key=key, Body=b"hello", IfNoneMatch="*")
        with self.assertRaises(ClientError):
            self.s3.put_object(Bucket=self.storage.bucket, Key=key, Body=b"other", IfNoneMatch="*")
        store = MessageStore(self.db, self.rooms)
        for attempt in range(3):
            _, message, created = self.service.complete_upload(media_id, "owner")
            self.assertEqual(created, attempt == 0)
            self.assertEqual(message["messageSeq"], 1)
        self.assertEqual(store.after("room", 0), [message])

    def test_sqs_acknowledges_only_completed_and_duplicate_work(self):
        sqs = boto3.client("sqs", region_name="us-east-1")
        queue_url = sqs.create_queue(QueueName="media-test")["QueueUrl"]
        self.service.queue.client, self.service.queue.url = sqs, queue_url
        media_id = self.upload()
        message = sqs.receive_message(QueueUrl=queue_url)["Messages"][0]
        self.service.queue.process_message(message)
        self.assertEqual(self.service.status(media_id)["status"], "COMPLETED")
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps({"jobId": media_id}))
        self.service.queue.process_message(sqs.receive_message(QueueUrl=queue_url)["Messages"][0])
        self.assertFalse(sqs.receive_message(QueueUrl=queue_url).get("Messages"))

    def test_sqs_extends_visibility_while_extracting(self):
        sqs = boto3.client("sqs", region_name="us-east-1")
        url = sqs.create_queue(QueueName="heartbeat-test")["QueueUrl"]
        self.service.queue.client, self.service.queue.url = sqs, url
        self.service.queue.heartbeat_interval = 0.01
        self.upload()
        message = sqs.receive_message(QueueUrl=url)["Messages"][0]
        tracked = MagicMock(wraps=sqs)
        self.service.queue.client = tracked
        def slow_extract(*_):
            time.sleep(0.08)
            return {"pages": ["hello"], "needsOcr": False}
        with patch("app.media.service.extract", side_effect=slow_extract):
            self.service.queue.process_message(message)
        self.assertTrue(tracked.change_message_visibility.called)
        self.assertTrue(tracked.delete_message.called)
        self.assertTrue(all(call.kwargs["VisibilityTimeout"] == 90 for call in tracked.change_message_visibility.call_args_list))

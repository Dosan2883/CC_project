import concurrent.futures
from urllib.parse import urlparse, parse_qs
import json
import unittest
from unittest.mock import patch
from fastapi import HTTPException
import support
import test_cd_features as cd
from app.core.database import Change
from app.auth.repository import AccountRepository, PersistentThrottle
from app.auth.service import AuthService, TokenSigner
from app.users.service import PreferencesService


class StoredResultChecks(unittest.TestCase):
    def setUp(self):
        cd.FeatureChecks.setUp(self)
        self.jobs.translation.media = self.media

    def complete(self, body=None):
        job = self.jobs.submit(body or self.body, "owner")
        self.jobs.work(job["jobId"])
        return self.jobs.get(job["jobId"], "owner")

    def page(self, pages):
        self.db.commit([Change("MEDIA", "ASSET#f", None, {"mediaId":"f", "roomId":"r", "status":"UPLOADED", "transcriptKey":"old.json"})])
        self.media.storage.put("old.json", json.dumps({"pages":pages, "version":1, "needsOcr":False}).encode())
        return {**self.body, "sources":[{"kind":"document", "mediaId":"f"}]}

    def test_reopen_shared_result_does_not_call_ai_or_charge_quota(self):
        job = self.complete()
        count = self.db.get("AI_QUOTA", "owner#ko").value
        self.rooms.join("r", "guest")
        self.model.configured = False
        again = self.jobs.submit(self.body, "guest")
        self.assertEqual(again["jobId"], job["jobId"])
        self.assertEqual(again["createdAt"], job["createdAt"])
        self.assertEqual(again["status"], "COMPLETED")
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.db.get("AI_QUOTA", "owner#ko").value, count)
        self.assertTrue(job["resultKey"].startswith("rooms/r/summaries/"))
        self.assertEqual(len(self.jobs.list_results("r", "guest")), 1)
        self.assertEqual(self.jobs.list_results("r", "guest")[0]["sourceVersions"][0]["version"], 1)
        self.rooms.leave("r", "guest")
        with self.assertRaises(HTTPException):
            self.jobs.get(job["jobId"], "guest")

    def test_concurrent_identical_jobs_share_id_and_single_call(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            values = list(pool.map(lambda _:self.jobs.submit(self.body, "owner"), range(2)))
            ids = [v["jobId"] for v in values]
            list(pool.map(self.jobs.work, ids))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.model.calls, 1)

    def test_s3_write_retry_uses_durable_response(self):
        job = self.jobs.submit(self.body, "owner")
        put = self.media.storage.put
        failed = False
        def write(key, raw):
            nonlocal failed
            if "/chunks/" not in key and not failed:
                failed = True
                raise OSError("S3 unavailable")
            return put(key, raw)
        with patch.object(self.media.storage, "put", side_effect=write):
            self.jobs.work(job["jobId"])
            self.db.update("AI_JOBS", job["jobId"], lambda v:{**v,"availableAt":0})
            self.jobs.work(job["jobId"])
        self.assertEqual(self.jobs.get(job["jobId"], "owner")["status"], "COMPLETED")
        self.assertEqual(self.model.calls, 1)

    def test_storage_read_failure_never_starts_new_ai_call(self):
        with patch.object(self.media.storage, "optional", side_effect=PermissionError("denied")), self.assertRaises(HTTPException) as error:
            self.jobs.submit(self.body, "owner")
        self.assertEqual(error.exception.detail, "RESULT_STORAGE_UNAVAILABLE")
        self.assertEqual(self.model.calls, 0)

    def test_s3_result_restores_database_completion_without_ai(self):
        job = self.complete()
        record = self.db.get("AI_JOBS", job["jobId"])
        self.db.commit([record.change(None)])
        self.model.configured = False
        restored = self.jobs.submit(self.body, "owner")
        self.assertEqual(restored["status"], "COMPLETED")
        self.assertEqual(self.model.calls, 1)

    def test_long_pdf_summary_reuses_chunks_after_partial_failure(self):
        body = self.page(["first " * 550, "second " * 500])
        calls, failed = [], False
        def summarize(text, target):
            nonlocal failed
            calls.append(text)
            if "second" in text and not failed:
                failed = True
                raise RuntimeError("transient")
            return "결정 [1] [2]"
        self.model.summarize = summarize
        job = self.jobs.submit(body, "owner")
        self.jobs.work(job["jobId"])
        first_count = sum("first" in text for text in calls)
        self.db.update("AI_JOBS", job["jobId"], lambda v:{**v,"availableAt":0})
        self.jobs.work(job["jobId"])
        result = self.jobs.get(job["jobId"], "owner")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(sum("first" in text for text in calls), first_count)
        self.assertEqual(len(result["result"]["evidence"]), 2)
        self.assertTrue(result["resultKey"].startswith("rooms/r/files/f/summaries/"))
        before = len(calls)
        self.jobs.submit(body, "owner")
        self.assertEqual(len(calls), before)

    def test_blank_scan_in_document_cannot_be_reported_as_full_success(self):
        body = self.page(["native", ""])
        with self.assertRaises(HTTPException) as error:
            self.jobs.submit(body, "owner")
        self.assertEqual(error.exception.detail, "OCR_REQUIRED")
        self.assertEqual(self.model.calls, 0)

    def test_long_document_translation_and_reopen(self):
        body = self.page(["first " * 600, "second " * 600])
        body.update(kind="translate", sourceLanguage="auto")
        calls = []
        def translate(text, target, source=None):
            calls.append(text)
            return "번역 " + text
        self.model.translate = translate
        first = self.complete(body)
        self.assertEqual(first["status"], "COMPLETED")
        self.assertGreater(len(calls), 1)
        before = len(calls)
        self.jobs.submit(body, "owner")
        self.assertEqual(len(calls), before)

    def test_signup_language_and_existing_preferences(self):
        auth = AuthService(AccountRepository(self.db), TokenSigner("test-signing-secret-long-enough-12345"), PersistentThrottle(self.db, "signup"))
        auth.signup("japanese", "secret123", "ja")
        preferences = PreferencesService(self.db)
        self.assertEqual(preferences.get("japanese")["defaultTargetLanguage"], "ja")
        preferences.update("japanese", {"defaultTargetLanguage":"fr"})
        auth.login("japanese", "secret123")
        self.assertEqual(preferences.get("japanese")["defaultTargetLanguage"], "fr")

    def test_legacy_summary_import_without_new_ai(self):
        body = {**self.body, "sourceLanguage":"auto"}
        evidence = self.jobs.select("r", body["sources"], "owner")
        old = {**body, "sourceHash":self.jobs.digest(evidence)}
        self.jobs.remember_summary(old, "결정 [1]")
        self.model.configured = False
        result = self.jobs.submit(body, "owner")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.model.calls, 0)

    def test_order_is_canonical_but_language_changes_create_new_results(self):
        from app.chat.repository import MessageStore
        MessageStore(self.db, self.rooms).append("r", "owner", "second", "en")
        body = {**self.body, "sources":[{"kind":"message", "messageSeq":2}, {"kind":"message", "messageSeq":1}]}
        first = self.complete(body)
        again = self.jobs.submit({**body,"sources":list(reversed(body["sources"]))}, "owner")
        self.assertEqual(first["jobId"], again["jobId"])
        before = self.model.calls
        self.complete({**body, "target":"en"})
        self.assertGreater(self.model.calls, before)
        count = self.model.calls
        changed = self.complete({**body, "sourceLanguage":"fr"})
        self.assertNotEqual(changed["jobId"], first["jobId"])
        self.assertGreater(self.model.calls, count)

    def test_database_completion_failure_recovers_from_s3(self):
        job = self.jobs.submit(self.body, "owner")
        finish = self.jobs.finish
        def interrupted(job_id, lease, changes):
            if changes.get("status") == "COMPLETED":
                raise RuntimeError("DB completion interrupted")
            return finish(job_id, lease, changes)
        with patch.object(self.jobs, "finish", side_effect=interrupted):
            self.jobs.work(job["jobId"])
        self.model.configured = False
        recovered = self.jobs.submit(self.body, "owner")
        self.assertEqual(recovered["status"], "COMPLETED")
        self.assertEqual(self.model.calls, 1)

    def test_unknown_post_call_interruption_never_automatically_reinvokes(self):
        calls = []
        def interrupted():
            calls.append(1)
            raise SystemExit("worker stopped before storing response")
        with self.assertRaises(SystemExit):
            self.jobs.artifacts.run({"test":"uncertain"}, "uncertain.json", interrupted)
        with self.assertRaises(HTTPException) as error:
            self.jobs.artifacts.run({"test":"uncertain"}, "uncertain.json", interrupted)
        self.assertEqual(error.exception.detail, "AI_CALL_UNCERTAIN")
        self.assertEqual(len(calls), 1)

    def test_upload_preview_does_not_extract_until_ai_request(self):
        from app.media.routes import original
        upload = self.media.request_upload("r", "owner", "demo.pdf", 9)
        token = parse_qs(urlparse(upload["uploadUrl"]).query)["token"][0]
        self.media.receive_upload(upload["mediaId"], token, b"%PDF-demo")
        self.assertIsNone(self.db.get("MEDIA_JOBS", upload["mediaId"]))
        with patch("app.media.routes.media_svc", self.media), patch("app.media.routes.room_repo", self.rooms):
            self.assertEqual(original(upload["mediaId"], "owner").body, b"%PDF-demo")
        self.assertIsNone(self.db.get("MEDIA_JOBS", upload["mediaId"]))
        body = {**self.body, "sources":[{"kind":"document", "mediaId":upload["mediaId"]}]}
        with self.assertRaises(HTTPException) as error:
            self.jobs.submit(body, "owner")
        self.assertEqual(error.exception.detail, "EXTRACTION_PENDING")
        self.assertEqual(self.db.get("MEDIA_JOBS", upload["mediaId"]).value["status"], "PENDING")

    def test_room_marker_failure_remains_pending_until_recovery(self):
        from app.rooms.folders import RoomFolders
        folders = RoomFolders(self.db, self.rooms, self.media.storage)
        with self.assertLogs("app.rooms.folders", level="ERROR"), patch.object(self.media.storage, "put", side_effect=OSError("S3 down")):
            folders.ensure("r")
        self.assertEqual(self.db.get("ROOM_FOLDERS", "r").value["status"], "PENDING")
        folders.recover()
        self.assertEqual(self.db.get("ROOM_FOLDERS", "r").value["status"], "COMPLETED")
        self.assertEqual(len(self.rooms.all()), 1)

    def test_legacy_original_and_text_migrate_together_without_ai(self):
        self.db.commit([Change("MEDIA", "ASSET#old", None, {"mediaId":"old", "roomId":"r", "ext":"pdf", "s3Key":"original/old", "transcriptKey":"extracted/old.json"})])
        self.media.storage.put("original/old", b"%PDF-original")
        self.media.storage.put("extracted/old.json", json.dumps({"pages":["legacy text"], "version":1}).encode())
        self.assertEqual(self.media.transcript("old")["pages"], ["legacy text"])
        migrated = self.media.asset("old")
        self.assertTrue(migrated["s3Key"].startswith("rooms/r/files/old/"))
        self.assertTrue(migrated["transcriptKey"].startswith("rooms/r/files/old/"))
        self.assertEqual(self.media.storage.get("original/old"), b"%PDF-original")
        self.assertEqual(self.model.calls, 0)

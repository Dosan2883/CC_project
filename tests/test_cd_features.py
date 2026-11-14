import io
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import support
from fastapi import HTTPException
from app.ai.jobs import AIJobs
from app.ai.service import TranslationService
from app.chat.repository import MessageStore
from app.core.database import SQLiteDatabase, Change
from app.rooms.repository import RoomRepository
from app.media.service import MediaService, looks_valid
from app.media.storage import LocalStorage
from app.media.extractors import extract


class Model:
    configured = True
    calls = 0
    fail = False

    def summarize(self, text, language):
        self.calls += 1
        if self.fail:
            raise RuntimeError("transient")
        return "결정 [1]" if language == "Korean" else "Decision [1]"


class FeatureChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = SQLiteDatabase(Path(self.temp.name) / "test.db")
        self.rooms = RoomRepository(self.db)
        self.rooms.create({"roomId":"r", "createdBy":"owner", "createdAt":0, "name":"r", "key":"r", "visibility":"public"})
        MessageStore(self.db, self.rooms).append("r", "owner", "ship on Friday", "en")
        self.media = MediaService(LocalStorage(Path(self.temp.name)), lambda *args: None, self.db, self.rooms)
        self.model = Model()
        self.jobs = AIJobs(self.db, self.rooms, self.media, TranslationService(self.db, self.rooms, self.model))
        self.body = {"roomId":"r", "kind":"summarize", "target":"ko", "sourceLanguage":"en", "consent":True,
                     "sources":[{"kind":"message", "messageSeq":1, "startChar":0}]}

    def test_selection_result_evidence_and_authorization(self):
        job = self.jobs.submit(self.body, "owner")
        self.assertTrue(self.jobs.work(job["jobId"]))
        result = self.jobs.get(job["jobId"], "owner")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["result"]["evidence"][0]["text"], "ship on Friday")
        self.assertTrue(self.jobs.work(job["jobId"]))
        self.assertEqual(self.model.calls, 1)
        with self.assertRaises(HTTPException):
            self.jobs.get(job["jobId"], "outsider")

    def test_summary_cache_reuses_until_selected_messages_change(self):
        first = self.jobs.submit(self.body, "owner")
        self.assertTrue(self.jobs.work(first["jobId"]))
        second = self.jobs.submit(self.body, "owner")
        self.assertTrue(second["existing"])  # 이미 만든 요약이라 새 작업이 없다
        self.assertEqual(self.model.calls, 1)  # 같은 선택이면 두 번째는 Lambda를 부르지 않는다
        MessageStore(self.db, self.rooms).append("r", "owner", "ship on Monday", "en")
        changed = {**self.body, "sources": [{"kind": "message", "messageSeq": 2, "startChar": 0}]}
        third = self.jobs.submit(changed, "owner")
        self.assertTrue(self.jobs.work(third["jobId"]))
        self.assertEqual(self.model.calls, 2)  # 선택한 자료가 달라지면 다시 요약한다
        other = self.jobs.submit({**self.body, "target": "en"}, "owner")
        self.assertTrue(self.jobs.work(other["jobId"]))
        self.assertEqual(self.model.calls, 3)  # 대상 언어가 다르면 별도 요약

    def test_summary_in_wrong_language_is_rejected_and_not_cached(self):
        self.model.summarize = lambda text, language: "결정 [1]"  # 일본어를 요청했는데 한국어로 답한 경우
        job = self.jobs.submit({**self.body, "target": "ja"}, "owner")
        self.jobs.work(job["jobId"])
        self.assertEqual(self.db.get("AI_JOBS", job["jobId"]).value["status"], "PENDING")
        self.assertIsNone(self.jobs.cached_summary({**self.db.get("AI_JOBS", job["jobId"]).value}))

    def test_summary_list_names_sharing_and_rename(self):
        job = self.jobs.submit({**self.body, "title": "  금요일   배포 요약  "}, "owner")
        self.assertTrue(self.jobs.work(job["jobId"]))
        listed = self.jobs.list_summaries("r", "owner")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["title"], "금요일 배포 요약")
        self.assertNotIn("resultKey", listed[0])
        self.assertEqual(self.jobs.get(job["jobId"], "owner")["entryId"], listed[0]["entryId"])
        detail = self.jobs.read_summary("r", listed[0]["entryId"], "owner")
        self.assertEqual(detail["result"]["text"], "결정 [1]")
        again = self.jobs.submit(self.body, "owner")  # 같은 선택이면 새 작업·새 항목 없이 기존 요약을 알려 준다
        self.assertTrue(again["existing"])
        self.assertEqual((again["entryId"], again["title"]), (listed[0]["entryId"], "금요일 배포 요약"))
        self.assertNotIn("jobId", again)
        listed = self.jobs.list_summaries("r", "owner")
        self.assertEqual((len(listed), listed[0]["title"], self.model.calls), (1, "금요일 배포 요약", 1))
        self.assertEqual(self.jobs.rename_summary("r", listed[0]["entryId"], "새 이름", "owner")["title"], "새 이름")
        with self.assertRaises(HTTPException):
            self.jobs.rename_summary("r", listed[0]["entryId"], "  ", "owner")
        with self.assertRaises(HTTPException):
            self.jobs.list_summaries("r", "outsider")
        with self.assertRaises(HTTPException):
            self.jobs.read_summary("r", listed[0]["entryId"], "outsider")
        with self.assertRaises(HTTPException):
            self.jobs.rename_summary("r", listed[0]["entryId"], "해킹", "outsider")

    def test_delete_summary_owner_only_and_allows_regeneration(self):
        job = self.jobs.submit(self.body, "owner")
        self.assertTrue(self.jobs.work(job["jobId"]))
        entry = self.jobs.list_summaries("r", "owner")[0]["entryId"]
        with self.assertRaises(HTTPException):
            self.jobs.delete_summary("r", entry, "outsider")
        self.assertEqual(self.jobs.delete_summary("r", entry, "owner")["deleted"], True)
        self.assertEqual(self.jobs.list_summaries("r", "owner"), [])
        with self.assertRaises(HTTPException):
            self.jobs.delete_summary("r", entry, "owner")
        again = self.jobs.submit(self.body, "owner")  # 지운 뒤에는 같은 선택도 새로 요약한다
        self.assertNotIn("existing", again)
        self.assertTrue(self.jobs.work(again["jobId"]))
        self.assertEqual(self.model.calls, 2)

    def test_room_files_list_newest_first_with_status(self):
        from app.core.database import Change
        for seq, (media_id, name, at) in enumerate([("m1", "a.pdf", 100), ("m2", "b.pdf", 200)], start=10):
            asset = {"mediaId": media_id, "roomId": "r", "filename": name, "type": "document", "ext": "pdf", "size": 5, "status": "COMPLETED", "jobId": media_id, "pageCount": 2}
            card = {"mediaId": media_id, "filename": name, "kind": "document", "ext": "pdf", "size": 5}
            self.db.commit([
                Change("MEDIA", "ASSET#" + media_id, None, asset),
                Change("MEDIA_JOBS", media_id, None, {"status": "COMPLETED", "progress": 100, "errorCode": None, "retryCount": 0}),
                Change("ROOM#r", f"MESSAGE#{seq:020d}", None, {"sender": "owner", "sentAt": at, "media": card, "bodyOriginal": name}),
                Change("ROOM#r", "MEDIA_MESSAGE#" + media_id, None, {"messageSeq": seq}),
            ])
        files = self.media.list_room_files("r")
        self.assertEqual([f["filename"] for f in files], ["b.pdf", "a.pdf"])
        self.assertEqual((files[0]["status"], files[0]["sender"], files[0]["pageCount"]), ("COMPLETED", "owner", 2))

    def test_delete_file_owner_only_hides_everywhere(self):
        from app.core.database import Change
        from app.media.service import MediaError
        asset = {"mediaId": "m9", "roomId": "r", "uploader": "owner", "filename": "x.pdf", "type": "document", "ext": "pdf", "size": 5, "status": "COMPLETED", "jobId": "m9", "pageCount": 1}
        self.db.commit([
            Change("MEDIA", "ASSET#m9", None, asset),
            Change("MEDIA_JOBS", "m9", None, {"status": "COMPLETED", "progress": 100, "errorCode": None, "retryCount": 0}),
            Change("ROOM#r", f"MESSAGE#{20:020d}", None, {"sender": "owner", "sentAt": 1, "media": {"ext": "pdf"}, "bodyOriginal": "x.pdf"}),
            Change("ROOM#r", "MEDIA_MESSAGE#m9", None, {"messageSeq": 20}),
        ])
        self.assertEqual([f["mediaId"] for f in self.media.list_room_files("r")], ["m9"])
        self.assertEqual(self.media.live_asset("m9")["mediaId"], "m9")
        with self.assertRaises(HTTPException):
            self.media.delete_file("m9", "outsider")  # 방 멤버가 아니면 거부
        self.assertEqual(self.media.delete_file("m9", "owner")["status"], "DELETED")
        self.assertEqual(self.media.list_room_files("r"), [])
        with self.assertRaises(MediaError):
            self.media.live_asset("m9")  # 삭제된 파일은 요약·번역에서도 없는 파일로 본다
        self.assertEqual(self.media.delete_file("m9", "owner")["status"], "DELETED")  # 다시 지워도 같은 결과

    def test_failure_backoff_and_dead_letter(self):
        self.model.fail = True
        job = self.jobs.submit(self.body, "owner")
        for attempt in range(3):
            self.assertFalse(self.jobs.work(job["jobId"]))
            self.db.update("AI_JOBS", job["jobId"], lambda value: {**value, "availableAt":0})
        value = self.jobs.get(job["jobId"], "owner")
        self.assertEqual(value["status"], "FAILED")
        self.assertTrue(value["deadLetter"])

    def test_source_version_change_invalidates_result(self):
        self.db.commit([Change("MEDIA", "ASSET#f", None, {"mediaId":"f", "roomId":"r", "transcriptKey":"text.json"})])
        self.media.storage.put("text.json", json.dumps({"pages":["old"], "version":1}).encode())
        self.body["sources"] = [{"kind":"page", "mediaId":"f", "page":1}]
        job = self.jobs.submit(self.body, "owner")
        self.media.storage.put("text.json", json.dumps({"pages":["new"], "version":2}).encode())
        with self.assertRaises(HTTPException) as error:
            self.jobs.get(job["jobId"], "owner")
        self.assertEqual(error.exception.status_code, 409)

    def test_image_validation_and_ocr_metadata(self):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (32,32), "white").save(output, "PNG")
        data = output.getvalue()
        self.assertTrue(looks_valid("png", data))
        self.assertFalse(looks_valid("jpg", data))
        with patch("app.media.ocr.recognize", return_value={"text":"hello", "regions":[{"polygon":[[0,0],[20,0],[20,10],[0,10]], "text":"hello", "confidence":.9}], "language":"en", "model":"fake", "width":32, "height":32}):
            result = extract("png", data, lambda *args: None, "en")
        self.assertEqual(result["pages"], ["hello"])
        self.assertEqual(result["pageDetails"][0]["page"], 1)

    def test_mixed_pdf_ocr_only_blank_page(self):
        from pypdf import PdfWriter
        output = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(72,72)
        writer.add_blank_page(72,72)
        writer.write(output)
        with patch.dict("app.media.extractors.EXTRACTORS", {"pdf": lambda *args: {"pages":["native", ""], "needsOcr":False}}), patch("app.media.ocr.recognize", return_value={"text":"scan", "regions":[], "language":"ko", "model":"fake"}) as recognize:
            result = extract("pdf", output.getvalue(), lambda *args: None, "ko")
        self.assertEqual(result["pages"], ["native", "scan"])
        self.assertEqual(recognize.call_count, 1)

    def test_correction_preserves_original_and_rejects_stale_version(self):
        from app.media.routes import correct_page, PageCorrection
        self.db.commit([Change("MEDIA", "ASSET#f", None, {"mediaId":"f", "roomId":"r", "transcriptKey":"old.json", "extractionVersion":1})])
        self.media.storage.put("old.json", json.dumps({"pages":["original"], "version":1}).encode())
        with patch("app.media.routes.db", self.db), patch("app.media.routes.media_svc", self.media), patch("app.media.routes.room_repo", self.rooms):
            body = PageCorrection(version=1, text="corrected", language="en")
            result = correct_page("f", 1, body, "owner")
            self.assertEqual(result["pages"], ["corrected"])
            self.assertEqual(result["originalPages"], ["original"])
            with self.assertRaises(HTTPException) as error:
                correct_page("f", 1, body, "owner")
            self.assertEqual(error.exception.status_code, 409)

    def test_lambda_summary_contract_and_output_limit(self):
        path = Path(__file__).resolve().parents[1] / "lambda/translation/handler.py"
        spec = importlib.util.spec_from_file_location("translation_lambda", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._bedrock = Mock()
        module._bedrock.converse.return_value = {"stopReason":"max_tokens", "output":{"message":{"content":[{"text":"partial [1]"}]}}}
        result = module._translate("[1] selected evidence", "Korean", "summarize")
        self.assertEqual(result["finishReason"], "max_tokens")
        self.assertTrue(result["truncated"])
        args = module._bedrock.converse.call_args.kwargs
        self.assertEqual(args["inferenceConfig"]["maxTokens"], 4000)
        self.assertIn("Cite evidence", args["system"][0]["text"])

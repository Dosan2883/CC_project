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
        return "Decision [1]"


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

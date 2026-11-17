"""Section 3 regression checks: local models and mock FFmpeg only, no paid APIs."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import support
from fastapi import HTTPException
from app.core.language import detect
from app.media.preprocess import prepare
from app.media.subtitles import webvtt
from app.media.whisper import SpeechError, WhisperTranscriber
from app.media.whisper import transcribe_bytes
from app.media.service import looks_valid, ALLOWED_EXT
from app.ai.service import TranslationService
import test_cd_features as fixtures


class MediaRequirements(unittest.TestCase):
    def test_mov_and_vtt_escaping_and_timestamps(self):
        self.assertEqual(ALLOWED_EXT["mov"], "video")
        self.assertTrue(looks_valid("mov", b"\0\0\0\x18ftypqt  "))
        self.assertFalse(looks_valid("mov", b"invalid"))
        out = webvtt([{"start":3661.125, "end":3663, "text":"<script>\n\nhello --> world"}])
        self.assertIn("01:01:01.125 --> 01:01:03.000", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("\n\nhello", out)

    def test_probe_rejects_long_or_no_audio_before_decoding(self):
        for info, code in [({"format":{"duration":"601"}, "streams":[{"codec_type":"audio","codec_name":"aac"}]}, "AUDIO_TOO_LONG"),
                           ({"format":{"duration":"5"}, "streams":[]}, "INVALID_AUDIO")]:
            with patch("app.media.preprocess.run", return_value=json.dumps(info).encode()) as run:
                with self.assertRaises(SpeechError) as error:
                    prepare("video.mov", ".", 600)
                self.assertEqual(error.exception.code, code)
                self.assertEqual(run.call_count, 1)

    def test_normalization_and_chunk_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            def execute(command):
                if command[0] == "ffprobe":
                    return json.dumps({"format":{"duration":"125"}, "streams":[{"codec_type":"audio","codec_name":"aac"}]}).encode()
                self.assertIn("16000", command)
                self.assertIn("pcm_s16le", command)
                Path(directory, "audio-0000.wav").touch()
                Path(directory, "audio-0001.wav").touch()
                return b""
            with patch("app.media.preprocess.run", side_effect=execute):
                files, duration, chunk = prepare("movie.mov", directory)
            self.assertEqual((len(files), duration, chunk), (2, 125, 120))

    def test_six_language_identification_and_short_review(self):
        samples = {
            "ko":"이번 금요일 회의에서 다음 주 출시 일정과 담당자를 결정하겠습니다.",
            "en":"We will decide the release schedule and the responsible team during the meeting on Friday.",
            "ja":"金曜日の会議では来週の発売日程と担当者を決める予定です。",
            "zh-CN":"我们将在星期五的会议上讨论下周的发布计划并确定负责人。",
            "es":"Durante la reunión del viernes vamos a decidir el calendario de lanzamiento y los responsables.",
            "fr":"Pendant la réunion de vendredi nous allons décider du calendrier de lancement et des responsables."}
        for language, text in samples.items():
            self.assertEqual(detect(text)["language"], language)
        self.assertTrue(detect("Hi")["needsReview"])

    def test_cache_source_revision_and_scope(self):
        one = TranslationService.cache_key("ko", "hello", "en", {"room":"r","version":1})
        self.assertNotEqual(one, TranslationService.cache_key("ko", "hello", "en", {"room":"r","version":2}))
        self.assertNotEqual(one, TranslationService.cache_key("ko", "hello", "en", {"room":"other","version":1}))

    def test_chunk_timestamps_rebased_to_movie_time(self):
        transcriber = WhisperTranscriber(model=object())
        values = [{"segments":[{"start":1,"end":3,"text":"first"}],"language":"en","model":"test"},
                  {"segments":[{"start":0,"end":5,"text":"second"}],"language":"en","model":"test"}]
        with patch("app.media.whisper.create_transcriber", return_value=transcriber), patch("app.media.preprocess.prepare", return_value=([Path("a.wav"),Path("b.wav")],125,120)), patch.object(transcriber, "transcribe_file", side_effect=values):
            result = transcribe_bytes(b"movie", "mov")
        self.assertEqual([(s["start"],s["end"]) for s in result["segments"]], [(1,3),(120,125)])


class SourceRevisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FeatureChecks()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_message_edit_preserves_original_permission_conflict_and_regeneration(self):
        from app.chat import routes
        from app.ai.refresh import recover
        f = self.fixture
        old = f.jobs.submit(f.body, "owner")
        self.assertTrue(f.jobs.work(old["jobId"]))
        with patch.multiple(routes, db=f.db, room_repo=f.rooms), patch.object(routes.manager, "broadcast"):
            edited = asyncio.run(routes.correct_message("r", 1, routes.MessageCorrection(version=1, text="release on Monday", language="en"), "owner"))
            self.assertEqual(edited["bodyOriginal"], "ship on Friday")
            self.assertEqual(edited["bodyCurrent"], "release on Monday")
            with self.assertRaises(HTTPException) as error:
                asyncio.run(routes.correct_message("r", 1, routes.MessageCorrection(version=1, text="stale"), "owner"))
            self.assertEqual(error.exception.status_code, 409)
        recover(f.jobs)
        jobs = f.db.query("AI_JOBS")
        self.assertEqual(len(jobs), 2)
        new = next(r.value for r in jobs if r.sk != old["jobId"])
        self.assertEqual(new["evidence"][0]["text"], "release on Monday")
        self.assertEqual(f.db.query("SOURCE_REFRESH")[0].value["status"], "COMPLETED")

    def test_segment_edit_and_subtitles_authorization_and_revision(self):
        from app.media import routes
        from app.core import runtime
        f = self.fixture
        key = "rooms/r/files/f/extracted/1/original.json"
        f.media.storage.put(key, json.dumps({"version":1,"pages":[], "language":"en",
                                             "segments":[{"start":0,"end":2,"text":"hello"}]}).encode())
        from app.core.database import Change
        f.db.commit([Change("MEDIA", "ASSET#f", None, {"mediaId":"f","roomId":"r","transcriptKey":key,"extractionVersion":1})])
        with patch.multiple(routes, db=f.db, media_svc=f.media, room_repo=f.rooms):
            changed = routes.correct_segment("f", 0, routes.SegmentCorrection(version=1,text="corrected"), "owner")
            self.assertEqual(changed["originalSegments"][0]["text"], "hello")
            response = routes.subtitles("f", "original", False, "owner")
            self.assertIn(b"corrected", response.body)
            self.assertEqual(response.headers["x-source-version"], "2")
            with self.assertRaises(HTTPException):
                routes.subtitles("f", "original", False, "intruder")
            with self.assertRaises(HTTPException):
                routes.subtitles("f", "ko", False, "owner")

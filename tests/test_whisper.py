"""음성·영상 인식과 업로드 흐름 검사. 실제 모델이나 AWS 없이 가짜 모델과 가짜 인식기만 쓴다."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support  # noqa: F401  (테스트용 환경 설정)
from app.core.database import SQLiteDatabase
from app.media.extractors import ExtractError, extract
from app.media.service import ALLOWED_EXT, MediaError, MediaService, looks_valid, max_bytes_for
from app.media.storage import LocalStorage
from app.media import whisper
from app.media.whisper import (FakeTranscriber, SpeechError, WhisperTranscriber, create_transcriber,
                               segments_from_whisper, transcribe_bytes)
from app.rooms.repository import RoomRepository


def seg(start, end, text, no_speech=0.0, logprob=-0.2):
    return SimpleNamespace(start=start, end=end, text=text, no_speech_prob=no_speech, avg_logprob=logprob)


class FakeModel:
    def __init__(self, segments, duration=10.0, language="ko", fail=False):
        self.segments, self.duration, self.language, self.fail = segments, duration, language, fail
        self.calls = []

    def transcribe(self, path, **options):
        self.calls.append((path, options))
        if self.fail:
            raise RuntimeError("cannot decode")
        return iter(self.segments), SimpleNamespace(duration=self.duration, language=self.language)


class SegmentConversionTests(unittest.TestCase):
    def test_converts_strips_and_rounds(self):
        out = segments_from_whisper([seg(0.004, 3.2149, "  안녕하세요  "), seg(3.3, 7.0, "회의입니다")])
        self.assertEqual(out, [{"start": 0.0, "end": 3.21, "text": "안녕하세요"}, {"start": 3.3, "end": 7.0, "text": "회의입니다"}])

    def test_drops_empty_and_hallucinated_segments(self):
        out = segments_from_whisper([seg(0, 1, "   "), seg(1, 2, "시청해 주셔서 감사합니다", no_speech=0.95, logprob=-1.5), seg(2, 3, "정상", no_speech=0.95, logprob=-0.3)])
        self.assertEqual([s["text"] for s in out], ["정상"])  # 무음 확률만 높고 확신이 있으면 남긴다


class TranscriberTests(unittest.TestCase):
    def test_returns_segments_language_and_progress(self):
        model = FakeModel([seg(0, 4, "첫 문장"), seg(4, 10, "둘째 문장")], duration=10.0, language="ja")
        done = []
        out = WhisperTranscriber("base", model=model).transcribe_file("a.mp3", "auto", lambda d, t: done.append((d, t)))
        self.assertEqual([s["text"] for s in out["segments"]], ["첫 문장", "둘째 문장"])
        self.assertEqual((out["language"], out["durationSeconds"], out["model"]), ("ja", 10.0, "whisper-base"))
        self.assertEqual(done, [(4.0, 10.0), (10.0, 10.0)])
        self.assertIsNone(model.calls[0][1]["language"])  # auto는 언어를 지정하지 않는다
        self.assertTrue(model.calls[0][1]["vad_filter"])

    def test_language_is_passed_to_model(self):
        model = FakeModel([seg(0, 1, "x")])
        WhisperTranscriber(model=model).transcribe_file("a.mp3", "zh-CN")
        self.assertEqual(model.calls[0][1]["language"], "zh")

    def test_rejects_unknown_language_and_too_long_audio(self):
        with self.assertRaises(SpeechError) as error:
            WhisperTranscriber(model=FakeModel([])).transcribe_file("a.mp3", "xx")
        self.assertEqual(error.exception.code, "UNSUPPORTED_LANGUAGE")
        with self.assertRaises(SpeechError) as error:
            WhisperTranscriber(model=FakeModel([seg(0, 1, "x")], duration=601), max_seconds=600).transcribe_file("a.mp3", "ko")
        self.assertEqual(error.exception.code, "AUDIO_TOO_LONG")

    def test_decode_failure_hides_details(self):
        with self.assertRaises(SpeechError) as error:
            WhisperTranscriber(model=FakeModel([], fail=True)).transcribe_file("a.mp3", "ko")
        self.assertEqual(error.exception.code, "INVALID_AUDIO")
        self.assertNotIn("cannot decode", str(error.exception))

    def test_not_installed_is_reported(self):
        with patch.dict("sys.modules", {"faster_whisper": None}), self.assertRaises(SpeechError) as error:
            WhisperTranscriber("base").transcribe_file("a.mp3", "ko")
        self.assertEqual(error.exception.code, "STT_NOT_INSTALLED")


class FactoryAndBytesTests(unittest.TestCase):
    def test_backend_selection(self):
        with patch.dict("os.environ", {"STT_BACKEND": "fake"}):
            self.assertIsInstance(create_transcriber(), FakeTranscriber)
        for value in ("", "transcribe", "other"):
            with patch.dict("os.environ", {"STT_BACKEND": value}), self.assertRaises(SpeechError) as error:
                create_transcriber()
            self.assertEqual(error.exception.code, "STT_NOT_CONFIGURED")

    def test_whisper_backend_is_created_once(self):
        with patch.dict("os.environ", {"STT_BACKEND": "whisper", "STT_MODEL": "small"}), patch.object(whisper, "_whisper", None):
            first = create_transcriber()
            self.assertIs(create_transcriber(), first)
            self.assertEqual(first.model_size, "small")

    def test_transcribe_bytes_uses_a_temp_file_and_removes_it(self):
        seen = []
        class Spy(FakeTranscriber):
            def transcribe_file(self, path, language=None, progress=None):
                seen.append((path, Path(path).read_bytes(), Path(path).suffix))
                return super().transcribe_file(path, language, progress)
        with patch.object(whisper, "create_transcriber", return_value=Spy()):
            out = transcribe_bytes(b"audio-bytes", "MP3", "ko")
        self.assertEqual(len(out["segments"]), 3)
        self.assertEqual((seen[0][1], seen[0][2]), (b"audio-bytes", ".mp3"))
        self.assertFalse(Path(seen[0][0]).exists())
        with self.assertRaises(SpeechError):
            transcribe_bytes(b"x", "exe")


class ExtractAndUploadTests(unittest.TestCase):
    def test_extract_audio_with_fake_backend(self):
        with patch.dict("os.environ", {"STT_BACKEND": "fake"}):
            result = extract("mp3", b"x", lambda done, total: None, "ko")
        self.assertEqual((result["pages"], result["needsOcr"], len(result["segments"])), ([], False, 3))
        self.assertEqual((result["language"], result["recognitionModel"]), ("ko", "fake"))

    def test_extract_without_backend_fails_with_code(self):
        with patch.dict("os.environ", {"STT_BACKEND": ""}), self.assertRaises(ExtractError) as error:
            extract("wav", b"x", lambda done, total: None)
        self.assertEqual(error.exception.code, "STT_NOT_CONFIGURED")

    def test_allowed_types_and_limits(self):
        self.assertEqual({ALLOWED_EXT[e] for e in ("mp3", "m4a", "wav", "ogg", "flac")}, {"audio"})
        self.assertEqual({ALLOWED_EXT[e] for e in ("mp4", "webm")}, {"video"})
        self.assertGreater(max_bytes_for("mp4"), max_bytes_for("pdf"))
        self.assertGreater(max_bytes_for("mp3"), max_bytes_for("pdf"))

    def test_magic_bytes(self):
        good = {"mp3": b"ID3\x04" + b"\0" * 8, "wav": b"RIFF\0\0\0\0WAVEfmt ", "m4a": b"\0\0\0\x20ftypM4A ", "mp4": b"\0\0\0\x18ftypmp42",
                "ogg": b"OggS\0\x02", "flac": b"fLaC\0\0", "webm": b"\x1a\x45\xdf\xa3\x01"}
        for ext, data in good.items():
            self.assertTrue(looks_valid(ext, data), ext)
            self.assertFalse(looks_valid(ext, b"not a media file at all"), ext)
        self.assertTrue(looks_valid("mp3", b"\xff\xfb\x90\x00" + b"\0" * 8))  # 태그 없는 MP3 프레임

    def test_upload_to_transcript_flow(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"STT_BACKEND": "fake"}):
            db = SQLiteDatabase(Path(temp) / "t.db")
            rooms = RoomRepository(db)
            rooms.create({"roomId": "r", "createdBy": "owner", "createdAt": 0, "name": "r", "key": "r", "visibility": "public"})
            media = MediaService(LocalStorage(Path(temp)), lambda *args: None, db, rooms)
            data = b"RIFF\0\0\0\0WAVEfmt " + b"\0" * 100
            upload = media.request_upload("r", "owner", "회의.wav", len(data))
            token = upload["uploadUrl"].split("token=")[1]
            asset = media.receive_upload(upload["mediaId"], token, data)
            self.assertEqual((asset["type"], asset["status"]), ("audio", "UPLOADED"))
            self.assertTrue(media.work(upload["mediaId"]))
            self.assertEqual(media.status(upload["mediaId"])["status"], "COMPLETED")
            transcript = media.transcript(upload["mediaId"])
            self.assertEqual(len(transcript["segments"]), 3)
            self.assertEqual(transcript["pages"], [])
            with self.assertRaises(MediaError):  # 파일 종류와 내용이 다르면 거절
                media.request_upload("r", "owner", "x.exe", 5)
            bad = media.request_upload("r", "owner", "가짜.mp3", 20)
            with self.assertRaises(MediaError) as error:
                media.receive_upload(bad["mediaId"], bad["uploadUrl"].split("token=")[1], b"this is not an mp3 file")
            self.assertEqual(error.exception.code, "FILE_SIZE_MISMATCH")


if __name__ == "__main__":
    unittest.main()

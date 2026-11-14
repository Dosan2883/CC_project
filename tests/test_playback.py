"""음성 재생 주소·부분 전송·구간 번역 검사. 실제 AWS나 번역 서비스 없이 가짜만 쓴다."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401  (테스트용 환경 설정)
from fastapi import HTTPException

from app.ai.service import TranslationService, parse_numbered
from app.core.database import Change, SQLiteDatabase
from app.media import playback
from app.media import routes as media_routes
from app.media.service import MediaService
from app.media.storage import LocalStorage
from app.rooms.repository import RoomRepository

SECRET = "test-signing-secret"


class TokenTests(unittest.TestCase):
    def test_roundtrip_and_rejections(self):
        token = playback.make_token(SECRET, "m1", "owner", ttl=100, now=1000)
        self.assertEqual(playback.read_token(SECRET, token, "m1", now=1050), "owner")
        self.assertIsNone(playback.read_token(SECRET, token, "m2", now=1050))      # 다른 파일
        self.assertIsNone(playback.read_token(SECRET, token, "m1", now=1101))      # 만료
        self.assertIsNone(playback.read_token("other-secret", token, "m1", now=1050))  # 서명 키가 다름
        self.assertIsNone(playback.read_token(SECRET, token[:-2] + "00", "m1", now=1050))  # 변조
        for bad in ("", "abc", "a.b", "....", "x" * 50):
            self.assertIsNone(playback.read_token(SECRET, bad, "m1", now=1050))

    def test_user_cannot_be_swapped(self):
        token = playback.make_token(SECRET, "m1", "owner", now=1000)
        payload, signature = token.rsplit(".", 1)
        import base64, json
        forged = base64.urlsafe_b64encode(json.dumps({"m": "m1", "u": "intruder", "e": 9999999999}).encode()).decode() + "." + signature
        self.assertIsNone(playback.read_token(SECRET, forged, "m1", now=1000))


class RangeTests(unittest.TestCase):
    def test_parse_range(self):
        self.assertEqual(playback.parse_range(None, 100), (0, 99))
        self.assertEqual(playback.parse_range("bytes=0-", 100), (0, 99))
        self.assertEqual(playback.parse_range("bytes=10-19", 100), (10, 19))
        self.assertEqual(playback.parse_range("bytes=90-500", 100), (90, 99))
        self.assertEqual(playback.parse_range("bytes=-10", 100), (90, 99))
        for bad in ("bytes=100-", "bytes=20-10", "bytes=-", "bytes=-0", "items=0-1", "bytes=a-b", "bytes=0-1,5-6"):
            self.assertIsNone(playback.parse_range(bad, 100), bad)
        self.assertIsNone(playback.parse_range("bytes=0-1", 0))


class StreamRouteTests(unittest.TestCase):
    """라우트 함수를 직접 불러 재생 주소와 부분 전송을 검사한다(가짜 저장소·방·서명 키를 끼운다)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = LocalStorage(Path(self.temp.name))
        self.data = bytes(range(256)) * 4  # 1024바이트
        self.storage.put("original/m1", self.data)
        self.deleted, self.members = False, {"owner"}
        outer = self

        class FakeMedia:
            storage = self.storage

            def asset(self, media_id):
                if media_id != "m1":
                    from app.media.service import MediaError
                    raise MediaError(404, "MEDIA_NOT_FOUND")
                return {"mediaId": "m1", "roomId": "r", "ext": "wav", "s3Key": "original/m1", "uploader": "owner"}

            def is_deleted(self, asset):
                return outer.deleted

        class FakeRooms:
            def require_member(self, room_id, user):
                if user not in outer.members:
                    raise HTTPException(403, "not a member")

        for item in (patch.object(media_routes, "media_svc", FakeMedia()), patch.object(media_routes, "room_repo", FakeRooms()),
                     patch.object(media_routes, "signing_secret", SECRET)):
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def request(range_header=None):
        class Request:
            headers = {"range": range_header} if range_header else {}
        return Request()

    def token(self, media_id="m1", user="owner"):
        return playback.make_token(SECRET, media_id, user)

    def stream(self, token=None, range_header=None):
        return media_routes.stream_media("m1", token or self.token(), self.request(range_header))

    def test_play_url_then_full_and_partial_stream(self):
        url = media_routes.play_url("m1", "owner")["url"]
        self.assertTrue(url.startswith("/api/media/m1/stream?token="))
        token = url.split("token=")[1]
        full = self.stream(token)
        self.assertEqual((full.status_code, full.body, full.media_type), (200, self.data, "audio/wav"))
        self.assertEqual(full.headers["accept-ranges"], "bytes")
        part = self.stream(token, "bytes=100-199")
        self.assertEqual((part.status_code, part.body), (206, self.data[100:200]))
        self.assertEqual(part.headers["content-range"], "bytes 100-199/1024")
        self.assertEqual(self.stream(token, "bytes=-24").body, self.data[-24:])
        bad = self.stream(token, "bytes=5000-")
        self.assertEqual((bad.status_code, bad.headers["content-range"]), (416, "bytes */1024"))

    def test_rejects_bad_or_foreign_tokens(self):
        for token in ("garbage", self.token("m2")):  # 엉터리 토큰, 다른 파일용 토큰
            with self.assertRaises(HTTPException) as error:
                self.stream(token)
            self.assertEqual(error.exception.status_code, 401)

    def test_membership_and_deletion_are_rechecked(self):
        token = self.token()
        self.members = set()  # 토큰을 받은 뒤 방에서 나갔다
        with self.assertRaises(HTTPException) as error:
            self.stream(token)
        self.assertEqual(error.exception.status_code, 404)
        self.members, self.deleted = {"owner"}, True  # 파일이 삭제됐다
        for call in (lambda: self.stream(token), lambda: media_routes.play_url("m1", "owner")):
            with self.assertRaises(HTTPException) as error:
                call()
            self.assertEqual(error.exception.status_code, 404)


class FakeSegmentTranslator:
    configured = True

    def __init__(self, drop_marker_for=None):
        self.calls, self.drop = [], drop_marker_for

    def translate(self, text, target_name, source_name=None):
        self.calls.append(text)
        lines = []
        for line in text.splitlines():
            if self.drop and line.startswith(f"[{self.drop}]"):
                lines.append(line.split("] ", 1)[1].upper())  # 번호를 잃어버린 줄
            else:
                lines.append(line.upper() if not line.startswith("[") else "[" + line[1:].split("] ", 1)[0] + "] " + line.split("] ", 1)[1].upper())
        return "\n".join(lines) if "\n" in text or text.startswith("[") else text.upper()


class SegmentTranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = SQLiteDatabase(Path(self.temp.name) / "t.db")
        self.rooms = RoomRepository(self.db)
        self.rooms.create({"roomId": "r", "createdBy": "owner", "createdAt": 0, "name": "r", "key": "r", "visibility": "public"})
        self.media = MediaService(LocalStorage(Path(self.temp.name)), lambda *a: None, self.db, self.rooms)
        asset = {"mediaId": "m1", "roomId": "r", "uploader": "owner", "filename": "a.wav", "type": "audio", "ext": "wav", "size": 5,
                 "status": "COMPLETED", "jobId": "m1", "transcriptKey": "extracted/m1/t.json"}
        self.db.commit([Change("MEDIA", "ASSET#m1", None, asset)])
        import json
        self.segments = [{"start": 0, "end": 2, "text": "hello"}, {"start": 2, "end": 4, "text": "good morning"}, {"start": 4, "end": 6, "text": "bye"}]
        self.media.storage.put("extracted/m1/t.json", json.dumps({"pages": [], "segments": self.segments, "language": "ko"}).encode())

    def service(self, translator):
        return TranslationService(self.db, self.rooms, translator, media=self.media)

    def test_translates_all_segments_in_one_call(self):
        translator = FakeSegmentTranslator()
        out = self.service(translator).translate_segments("m1", "en", "owner")
        self.assertEqual([s["translated"] for s in out["segments"]], ["HELLO", "GOOD MORNING", "BYE"])
        self.assertEqual((out["segments"][1]["start"], out["segments"][1]["end"], out["segments"][1]["text"]), (2, 4, "good morning"))
        self.assertEqual(len(translator.calls), 1)  # 구간이 몇 개든 묶어서 한 번
        again = self.service(translator).translate_segments("m1", "en", "owner")
        self.assertEqual(len(translator.calls), 1)  # 같은 요청은 캐시
        self.assertEqual(again, out)

    def test_batch_with_lost_marker_is_retranslated_per_segment(self):
        translator = FakeSegmentTranslator(drop_marker_for=2)
        out = self.service(translator).translate_segments("m1", "ja", "owner")
        self.assertEqual([s["translated"] for s in out["segments"]], ["HELLO", "GOOD MORNING", "BYE"])
        self.assertEqual(len(translator.calls), 4)  # 묶음 1번 + 구간마다 따로 3번

    def test_same_language_skips_the_call(self):
        translator = FakeSegmentTranslator()
        out = self.service(translator).translate_segments("m1", "ko", "owner")
        self.assertEqual([s["translated"] for s in out["segments"]], ["hello", "good morning", "bye"])
        self.assertEqual(translator.calls, [])

    def test_permissions_and_limits(self):
        service = self.service(FakeSegmentTranslator())
        with self.assertRaises(HTTPException) as error:
            service.translate_segments("m1", "en", "outsider")
        self.assertEqual(error.exception.status_code, 404)
        with self.assertRaises(HTTPException) as error:
            service.translate_segments("m1", "xx", "owner")
        self.assertEqual(error.exception.detail, "INVALID_LANGUAGE")
        self.db.commit([Change("ROOM#r", "MEDIA_DELETED#m1", None, {"mediaId": "m1"})])
        with self.assertRaises(HTTPException) as error:
            service.translate_segments("m1", "en", "owner")
        self.assertEqual(error.exception.status_code, 404)


class FakeSummaryModel:
    configured = True

    def __init__(self):
        self.calls = []

    def summarize(self, text, language):
        self.calls.append(text)
        return "전체 요약입니다 [1]"


class TranscriptSummaryTests(unittest.TestCase):
    """음성 전체를 구간별이 아니라 하나의 근거로 요약한다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = SQLiteDatabase(Path(self.temp.name) / "t.db")
        self.rooms = RoomRepository(self.db)
        self.rooms.create({"roomId": "r", "createdBy": "owner", "createdAt": 0, "name": "r", "key": "r", "visibility": "public"})
        self.media = MediaService(LocalStorage(Path(self.temp.name)), lambda *a: None, self.db, self.rooms)
        self.db.commit([Change("MEDIA", "ASSET#m1", None, {"mediaId": "m1", "roomId": "r", "uploader": "owner", "filename": "a.wav", "type": "audio",
                                                           "ext": "wav", "size": 5, "status": "COMPLETED", "jobId": "m1", "transcriptKey": "extracted/m1/t.json"})])
        import json
        self.segments = [{"start": 0, "end": 2, "text": "안녕하세요."}, {"start": 2, "end": 5, "text": "회의를 시작합니다."}, {"start": 5, "end": 7, "text": "  "}]
        self.media.storage.put("extracted/m1/t.json", json.dumps({"pages": [], "segments": self.segments, "language": "ko"}).encode())
        from app.ai.jobs import AIJobs
        self.model = FakeSummaryModel()
        self.jobs = AIJobs(self.db, self.rooms, self.media, TranslationService(self.db, self.rooms, self.model, media=self.media))
        self.body = {"roomId": "r", "kind": "summarize", "target": "ko", "sourceLanguage": "auto", "consent": True,
                     "sources": [{"kind": "transcript", "mediaId": "m1", "startChar": 0}]}

    def test_whole_recording_is_one_evidence_without_timestamps(self):
        job = self.jobs.submit(self.body, "owner")
        self.assertTrue(self.jobs.work(job["jobId"]))
        result = self.jobs.get(job["jobId"], "owner")
        self.assertEqual(result["status"], "COMPLETED")
        evidence = result["result"]["evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["text"], "안녕하세요. 회의를 시작합니다.")  # 시각 표시 없이 이어 붙이고, 빈 구간은 뺀다
        self.assertEqual(self.model.calls, ["[1] 안녕하세요. 회의를 시작합니다."])  # Lambda 호출은 한 번
        again = self.jobs.submit(self.body, "owner")
        self.assertTrue(again["existing"])  # 같은 음성은 이미 만든 요약이라 다시 만들지 않는다

    def test_empty_long_and_foreign_requests(self):
        import json
        self.media.storage.put("extracted/m1/t.json", json.dumps({"pages": [], "segments": [{"start": 0, "end": 1, "text": " "}]}).encode())
        with self.assertRaises(HTTPException) as error:
            self.jobs.submit(self.body, "owner")
        self.assertEqual(error.exception.detail, "NO_TRANSCRIPT_SEGMENTS")
        self.media.storage.put("extracted/m1/t.json", json.dumps({"pages": [], "segments": [{"start": 0, "end": 1, "text": "가" * 3600}]}).encode())
        with self.assertRaises(HTTPException) as error:
            self.jobs.submit(self.body, "owner")
        self.assertEqual(error.exception.detail, "SELECTION_TOO_LONG")
        with self.assertRaises(HTTPException):
            self.jobs.submit(self.body, "outsider")  # 방 멤버가 아니면 거부


class ParseTests(unittest.TestCase):
    def test_parse_numbered(self):
        self.assertEqual(parse_numbered("[1] 안녕\n[2] 하세요\n이어진 줄\n[3] 끝"), {1: "안녕", 2: "하세요 이어진 줄", 3: "끝"})
        self.assertEqual(parse_numbered("번호 없는 글"), {})


if __name__ == "__main__":
    unittest.main()

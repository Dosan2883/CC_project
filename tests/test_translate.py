"""메시지 번역 API 검사. 실제 AWS·외부 AI는 부르지 않고, 가짜 번역기와 로컬 가짜 함수 URL을 쓴다."""
import json
import socket
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import support  # noqa: F401  (임시 저장소와 테스트용 환경변수를 먼저 설정한다)
import uvicorn

from app.ai.client import LambdaTranslator, TranslationUnavailable
from app.ai.service import CHUNK_CHARS, MAX_CHARS, MAX_PAGE_CHARS, split_text
from app.core import runtime
from app.core.database import Change
from main import app


class FakeTranslator:
    """외부를 부르지 않는 번역기. 몇 번 불렸는지 센다."""

    def __init__(self):
        self.configured = True
        self.fail = False
        self.calls = []

    def translate(self, text, target_name):
        self.calls.append((text, target_name))
        if self.fail:
            raise TranslationUnavailable("boom")
        return f"[{target_name}] {text}"


class ApiTestBase(unittest.TestCase):
    """실제 서버를 띄우고 가짜 번역기를 끼우는 공통 준비. 이 클래스 자체에는 테스트가 없다."""

    @classmethod
    def setUpClass(cls):
        cls.sock = socket.socket()
        cls.sock.bind(("127.0.0.1", 0))
        cls.base = f"http://127.0.0.1:{cls.sock.getsockname()[1]}"
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.sock]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started:
            if not cls.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("test server did not start")
            time.sleep(0.01)
        cls.original = runtime.translation.translator
        cls.original_limit = runtime.translation.hourly_limit

    @classmethod
    def tearDownClass(cls):
        runtime.translation.translator = cls.original
        runtime.translation.hourly_limit = cls.original_limit
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls.sock.close()

    def setUp(self):
        self.fake = FakeTranslator()
        runtime.translation.translator = self.fake
        runtime.translation.hourly_limit = 60

    def request(self, path, method="GET", body=None, token=None):
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read() or b"null")

    def signup(self, nickname):
        status, account = self.request("/api/auth/signup", "POST", {"nickname": nickname, "password": "secret123"})
        self.assertEqual(status, 200)
        return account["token"]

    def room_with_message(self, name, text, source="ko", members=("owner",)):
        token = self.signup(name + "own")
        status, room = self.request("/api/rooms", "POST", {"name": name, "visibility": "public"}, token)
        self.assertEqual(status, 200)
        user = name + "own"
        message = runtime.store.append(room["roomId"], user, text, source)
        return room["roomId"], message["messageSeq"], token

    def translate(self, token, room, seq, target="en"):
        return self.request("/api/ai/translate", "POST", {"roomId": room, "messageSeq": seq, "target": target}, token)


class TranslateApiTests(ApiTestBase):
    def test_translates_and_caches(self):
        room, seq, token = self.room_with_message("cache-room", "다음 주 회의는 금요일입니다.")
        status, first = self.translate(token, room, seq)
        self.assertEqual((status, first["translated"], first["cached"]), (200, "[English] 다음 주 회의는 금요일입니다.", False))
        status, second = self.translate(token, room, seq)
        self.assertEqual((status, second["cached"]), (200, True))
        self.assertEqual(len(self.fake.calls), 1)  # 두 번째는 외부를 부르지 않았다

    def test_cache_is_shared_without_using_quota(self):
        room, seq, owner = self.room_with_message("share-room", "공유 캐시 확인")
        guest = self.signup("shareguest")
        self.assertEqual(self.request(f"/api/rooms/{room}/join", "POST", token=guest)[0], 200)
        self.assertEqual(self.translate(owner, room, seq)[0], 200)
        runtime.translation.hourly_limit = 0  # 한도가 0이어도 캐시된 번역은 받을 수 있다
        status, result = self.translate(guest, room, seq)
        self.assertEqual((status, result["cached"]), (200, True))

    def test_same_language_does_not_call_out(self):
        room, seq, token = self.room_with_message("same-lang", "Hello there", source="en")
        status, result = self.translate(token, room, seq, "en")
        self.assertEqual((status, result["translated"], result["sameLanguage"]), (200, "Hello there", True))
        self.assertEqual(self.fake.calls, [])

    def test_requires_login_and_membership(self):
        room, seq, owner = self.room_with_message("perm-room", "비공개 내용", members=())
        self.assertEqual(self.request("/api/ai/translate", "POST", {"roomId": room, "messageSeq": seq, "target": "en"})[0], 401)
        outsider = self.signup("permoutsider")
        self.assertEqual(self.translate(outsider, room, seq)[0], 403)
        self.assertEqual(self.fake.calls, [])  # 멤버가 아니면 외부로 글자가 나가지 않는다

    def test_input_validation(self):
        room, seq, token = self.room_with_message("valid-room", "검증")
        self.assertEqual(self.translate(token, room, seq, "xx")[0], 422)           # 지원하지 않는 언어
        self.assertEqual(self.translate(token, room, seq + 50)[0], 404)            # 없는 메시지
        self.assertEqual(self.translate(token, "nope", seq)[0], 404)               # 없는 방
        self.assertEqual(self.request("/api/ai/translate", "POST", {"roomId": room, "messageSeq": 0, "target": "en"}, token)[0], 422)
        long_seq = runtime.store.append(room, "valid-roomown", "가" * (MAX_CHARS + 1), "ko")["messageSeq"]
        status, error = self.translate(token, room, long_seq)
        self.assertEqual((status, error["detail"]), (422, "TEXT_TOO_LONG"))
        card = {"mediaId": "m1", "filename": "a.pdf", "kind": "document", "ext": "pdf", "size": 1}
        file_seq = runtime.store.append(room, "valid-roomown", "a.pdf", "und", card)["messageSeq"]
        status, error = self.translate(token, room, file_seq)
        self.assertEqual((status, error["detail"]), (422, "NOT_TEXT_MESSAGE"))
        self.assertEqual(self.fake.calls, [])

    def test_extra_fields_are_rejected(self):
        room, seq, token = self.room_with_message("extra-room", "추가 필드")
        status, _ = self.request("/api/ai/translate", "POST", {"roomId": room, "messageSeq": seq, "target": "en", "text": "임의의 글"}, token)
        self.assertEqual(status, 422)  # 클라이언트가 번역할 글자를 직접 보낼 수 없다

    def test_hourly_limit(self):
        room, _, token = self.room_with_message("limit-room", "한도")
        seqs = [runtime.store.append(room, "limit-roomown", f"문장 {i}", "ko")["messageSeq"] for i in range(3)]
        runtime.translation.hourly_limit = 2
        self.assertEqual([self.translate(token, room, seq)[0] for seq in seqs], [200, 200, 429])
        self.assertEqual(len(self.fake.calls), 2)  # 한도를 넘은 요청은 외부를 부르지 않았다

    def test_not_configured_and_upstream_failure(self):
        room, seq, token = self.room_with_message("fail-room", "실패 확인")
        self.fake.configured = False
        self.assertEqual(self.translate(token, room, seq)[0], 503)
        self.fake.configured = True
        self.fake.fail = True
        status, error = self.translate(token, room, seq)
        self.assertEqual((status, error["detail"]), (502, "TRANSLATION_UNAVAILABLE"))
        self.fake.fail = False
        self.assertEqual(self.translate(token, room, seq)[0], 200)  # 실패한 결과는 캐시되지 않는다


class DocumentTranslateTests(ApiTestBase):
    """업로드한 문서의 페이지 번역."""

    def make_document(self, room, user, pages, ready=True):
        media_id = uuid.uuid4().hex
        asset = {"mediaId": media_id, "roomId": room, "uploader": user, "filename": "doc.pdf", "ext": "pdf",
                 "type": "document", "size": 1, "status": "UPLOADED", "transcriptKey": None}
        if ready:
            key = f"extracted/{media_id}/test.json"
            runtime.media_svc.storage.put(key, json.dumps({"pages": pages, "needsOcr": False}).encode())
            asset["transcriptKey"] = key
        runtime.db.commit([Change("MEDIA", "ASSET#" + media_id, None, asset)])
        return media_id

    def translate_page(self, token, media, page, target="en"):
        return self.request("/api/ai/translate-page", "POST", {"mediaId": media, "page": page, "target": target}, token)

    def test_translates_pages_and_caches(self):
        room, _, token = self.room_with_message("doc-room", "문서")
        media = self.make_document(room, "doc-roomown", ["첫 페이지 내용", "둘째 페이지 내용"])
        status, first = self.translate_page(token, media, 1)
        self.assertEqual((status, first["translated"], first["page"], first["cached"]), (200, "[English] 첫 페이지 내용", 1, False))
        self.assertEqual(self.translate_page(token, media, 1)[1]["cached"], True)
        status, second = self.translate_page(token, media, 2)
        self.assertEqual((status, second["translated"]), (200, "[English] 둘째 페이지 내용"))
        self.assertEqual(len(self.fake.calls), 2)  # 첫 페이지의 두 번째 요청은 캐시라서 부르지 않았다

    def test_permissions_and_validation(self):
        room, _, owner = self.room_with_message("doc-perm", "문서")
        media = self.make_document(room, "doc-permown", ["본문", "", "   "])
        self.assertEqual(self.request("/api/ai/translate-page", "POST", {"mediaId": media, "page": 1, "target": "en"})[0], 401)
        outsider = self.signup("docoutsider")
        self.assertEqual(self.translate_page(outsider, media, 1)[0], 404)             # 방 밖 사용자에게는 파일의 존재도 숨긴다
        self.assertEqual(self.translate_page(owner, "no-such-media", 1)[0], 404)
        self.assertEqual(self.translate_page(owner, media, 1, "xx")[0], 422)          # 지원하지 않는 언어
        self.assertEqual(self.translate_page(owner, media, 0)[0], 422)
        status, error = self.translate_page(owner, media, 9)
        self.assertEqual((status, error["detail"]), (422, "INVALID_PAGE"))
        for page in (2, 3):
            status, error = self.translate_page(owner, media, page)
            self.assertEqual((status, error["detail"]), (422, "EMPTY_PAGE"))
        pending = self.make_document(room, "doc-permown", [], ready=False)
        status, error = self.translate_page(owner, pending, 1)
        self.assertEqual((status, error["detail"]), (409, "NOT_READY"))               # 아직 추출이 끝나지 않음
        huge = self.make_document(room, "doc-permown", ["가" * (MAX_PAGE_CHARS + 1)])
        status, error = self.translate_page(owner, huge, 1)
        self.assertEqual((status, error["detail"]), (422, "PAGE_TOO_LONG"))
        status, _ = self.request("/api/ai/translate-page", "POST", {"mediaId": media, "page": 1, "target": "en", "text": "임의의 글"}, owner)
        self.assertEqual(status, 422)                                                  # 번역할 글자를 클라이언트가 직접 보낼 수 없다
        self.assertEqual(self.fake.calls, [])

    def test_long_page_is_split_and_counts_quota_per_chunk(self):
        paragraphs = ["가" * 1500, "나" * 1500, "다" * 1500]  # 합치면 한 번에 못 보내는 길이 -> 2조각
        room, _, token = self.room_with_message("doc-long", "문서")
        media = self.make_document(room, "doc-longown", ["\n\n".join(paragraphs)])
        status, result = self.translate_page(token, media, 1)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.fake.calls), 2)
        self.assertTrue(all(len(text) <= CHUNK_CHARS for text, _ in self.fake.calls))
        self.assertLess(result["translated"].index("가"), result["translated"].index("다"))  # 순서 유지

        room2, _, token2 = self.room_with_message("doc-limit", "문서")
        other = ["라" * 1500, "마" * 1500, "바" * 1500]  # 첫 번째와 같은 글자면 캐시가 먹어서 한도 검사까지 가지 못한다
        media2 = self.make_document(room2, "doc-limitown", ["\n\n".join(other)])
        runtime.translation.hourly_limit = 1  # 2조각이 필요한데 한도가 1이면 하나도 부르지 않는다
        before = len(self.fake.calls)
        self.assertEqual(self.translate_page(token2, media2, 1)[0], 429)
        self.assertEqual(len(self.fake.calls), before)

    def test_not_configured_and_upstream_failure(self):
        room, _, token = self.room_with_message("doc-fail", "문서")
        media = self.make_document(room, "doc-failown", ["문서 페이지 실패 확인"])  # 다른 테스트와 같은 글자면 캐시가 먹어서 일부러 다르게 쓴다
        self.fake.configured = False
        self.assertEqual(self.translate_page(token, media, 1)[0], 503)
        self.fake.configured, self.fake.fail = True, True
        self.assertEqual(self.translate_page(token, media, 1)[0], 502)
        self.fake.fail = False
        self.assertEqual(self.translate_page(token, media, 1)[1]["cached"], False)  # 실패한 결과는 캐시되지 않는다


class SplitTextTests(unittest.TestCase):
    def test_short_text_stays_whole(self):
        self.assertEqual(split_text("안녕\n\n세상"), ["안녕\n\n세상"])
        self.assertEqual(split_text("  \n\n  "), [])

    def test_respects_limit_and_keeps_paragraph_order(self):
        text = "\n\n".join(f"문단 {i} " + "가" * 400 for i in range(20))
        chunks = split_text(text, 1000)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
        self.assertEqual("\n\n".join(chunks), text)  # 문단 경계에서만 나눴으므로 다시 붙이면 원문과 같다

    def test_long_paragraph_splits_by_sentence_then_by_characters(self):
        sentences = " ".join(["이것은 문장입니다."] * 300)
        chunks = split_text(sentences, 500)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks) and len(chunks) > 1)
        self.assertEqual("".join("".join(chunks).split()), "".join(sentences.split()))  # 글자 손실 없음
        solid = "가" * 1200  # 공백도 문장 부호도 없는 경우
        chunks = split_text(solid, 500)
        self.assertEqual([len(chunk) for chunk in chunks], [500, 500, 200])
        self.assertEqual("".join(chunks), solid)


class FakeFunctionUrl(BaseHTTPRequestHandler):
    """Lambda 함수 URL을 흉내 내는 로컬 서버. 받은 요청을 기록한다."""
    seen = []
    reply = (200, {"translated": "Hello", "finishReason":"end_turn", "truncated":False, "modelVersion":"v1"})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        FakeFunctionUrl.seen.append({"secret": self.headers.get("x-api-secret"), "body": json.loads(body)})
        status, payload = FakeFunctionUrl.reply
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class LambdaTranslatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), FakeFunctionUrl)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        FakeFunctionUrl.seen.clear()
        FakeFunctionUrl.reply = (200, {"translated": "Hello", "finishReason":"end_turn", "truncated":False, "modelVersion":"v1"})

    def test_sends_secret_header_and_parses_reply(self):
        self.assertEqual(LambdaTranslator(self.url, "s3cret").translate("안녕하세요", "English"), "Hello")
        self.assertEqual(FakeFunctionUrl.seen, [{"secret": "s3cret", "body": {"text": "안녕하세요", "target": "English"}}])

    def test_bad_replies_become_unavailable(self):
        translator = LambdaTranslator(self.url, "s3cret")
        for reply in [(403, {"error": "forbidden"}), (502, {"error": "AccessDeniedException"}), (200, {"nope": 1}),
                      (200, {"translated": "   "}), (200, b"not json")]:
            FakeFunctionUrl.reply = reply
            with self.subTest(reply=reply), self.assertRaises(TranslationUnavailable):
                translator.translate("x", "English")

    def test_unreachable_server_becomes_unavailable(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(TranslationUnavailable):
            LambdaTranslator(f"http://127.0.0.1:{port}/", "s3cret", timeout=2).translate("x", "English")

    def test_unconfigured_never_calls_out(self):
        translator = LambdaTranslator("", "")
        self.assertFalse(translator.configured)
        with self.assertRaises(TranslationUnavailable):
            translator.translate("x", "English")
        self.assertEqual(FakeFunctionUrl.seen, [])

    def test_rejects_unsafe_url_schemes(self):
        for url in ["http://example.com/fn", "file:///etc/passwd", "ftp://example.com/x"]:
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                LambdaTranslator(url, "s3cret")
        LambdaTranslator("https://abc.lambda-url.ap-southeast-2.on.aws/", "s3cret")  # https는 허용


if __name__ == "__main__":
    unittest.main()

"""실제 HTTP/WebSocket을 사용하는 데모 통합 검사. 추가 테스트 의존성은 없다."""
import json
import re
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
import support
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import uvicorn
from websockets.sync.client import connect

from app.core import runtime
from app.media.storage import LocalStorage
from main import app


class DemoIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.original_storage = runtime.media_svc.storage
        runtime.media_svc.storage = LocalStorage(Path(cls.temp.name))
        cls.sock = socket.socket()
        cls.sock.bind(("127.0.0.1", 0))
        cls.port = cls.sock.getsockname()[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.sock]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started:
            if not cls.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("test server did not start")
            time.sleep(0.01)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls.sock.close()
        runtime.media_svc.storage = cls.original_storage
        cls.temp.cleanup()

    def request(self, path, method="GET", body=None, token=None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=body, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            content = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
            return response.status, content

    def signup(self, nickname):
        status, account = self.request("/api/auth/signup", "POST", {"nickname": nickname, "password": "secret123"})
        self.assertEqual(status, 200)
        return account["token"]

    def room(self, token, name, visibility="public"):
        status, room = self.request("/api/rooms", "POST", {"name": name, "visibility": visibility, "password": "pass123"}, token)
        self.assertEqual(status, 200)
        self.assertTrue(room["joined"])
        self.assertTrue({"key", "salt", "pwHash"}.isdisjoint(room))
        return room["roomId"]

    def test_auth_and_room_permissions(self):
        owner = self.signup("owner")
        guest = self.signup("guest")
        self.assertEqual(self.request("/api/rooms")[0], 401)
        status, account = self.request("/api/auth/login", "POST", {"nickname": "OWNER", "password": "secret123"})
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/api/auth/me", token=account["token"])[1], {"nickname": "owner"})
        public = self.room(owner, "Public room")
        private = self.room(owner, "Private room", "private")
        self.assertEqual(self.request("/api/rooms/public", token=guest)[1][0]["roomId"], public)
        self.assertEqual(self.request(f"/api/rooms/{private}/join", "POST", token=guest)[0], 404)
        self.assertEqual(self.request("/api/rooms/enter-private", "POST", {"name": "Private room", "password": "wrong"}, guest)[0], 403)
        self.assertEqual(self.request("/api/rooms/enter-private", "POST", {"name": "Private room", "password": "pass123"}, guest)[0], 200)
        self.assertEqual(self.request(f"/api/rooms/{public}/join", "POST", token=guest)[0], 200)
        self.assertEqual(len(self.request("/api/rooms", token=guest)[1]), 2)
        self.assertEqual(self.request(f"/api/rooms/{private}/leave", "POST", token=guest)[0], 200)
        self.assertEqual(len(self.request("/api/rooms", token=guest)[1]), 1)

    def test_chat_and_reconnect_history(self):
        token = self.signup("chatter")
        room = self.room(token, "Chat room")
        address = f"ws://127.0.0.1:{self.port}/ws/{room}"
        with connect(address, proxy=None, open_timeout=5) as ws:
            ws.send(json.dumps({"type": "auth", "token": token}))
            self.assertEqual(json.loads(ws.recv(timeout=5))["type"], "presence")

            ws.send(json.dumps({"type": "message:send", "body": "hello", "sourceLanguage": "en"}))
            message = json.loads(ws.recv(timeout=5))
            self.assertEqual((message["bodyOriginal"], message["messageSeq"]), ("hello", 1))
        with connect(address + "?lastSeq=0", proxy=None, open_timeout=5) as ws:
            ws.send(json.dumps({"type": "auth", "token": token}))
            self.assertEqual(json.loads(ws.recv(timeout=5))["bodyOriginal"], "hello")
            self.assertEqual(json.loads(ws.recv(timeout=5))["type"], "presence")
        with connect(address + "?lastSeq=1", proxy=None, open_timeout=5) as ws:
            ws.send(json.dumps({"type": "auth", "token": token}))
            self.assertEqual(json.loads(ws.recv(timeout=5))["type"], "presence")

    def test_websocket_accepts_messages_during_presence_outage(self):
        token = self.signup("presenceOutageUser")
        room = self.room(token, "Presence outage room")
        with patch.object(runtime.manager.bus, "presence", AsyncMock(side_effect=ConnectionError("Redis down"))):
            with connect(f"ws://127.0.0.1:{self.port}/ws/{room}", proxy=None, open_timeout=5) as ws:
                ws.send(json.dumps({"type":"auth", "token":token}))
                self.assertEqual(json.loads(ws.recv(timeout=5))["type"], "presence")
                ws.send(json.dumps({"type":"message:send", "body":"still connected", "sourceLanguage":"en"}))
                event = json.loads(ws.recv(timeout=5))
                self.assertEqual(event["bodyOriginal"], "still connected")

    def test_document_upload_and_extraction(self):
        token = self.signup("uploader")
        outsider = self.signup("outsider")
        room = self.room(token, "Files room")
        payload = "문서 추출 확인".encode()
        body = {"roomId": room, "filename": "demo.txt", "size": len(payload)}
        self.assertEqual(self.request("/api/files/upload-url", "POST", body, outsider)[0], 403)
        status, upload = self.request("/api/files/upload-url", "POST", body, token)
        self.assertEqual(status, 200)
        self.assertEqual(self.request(upload["uploadUrl"], "PUT", payload)[0], 200)
        self.assertEqual(self.request(upload["uploadUrl"], "PUT", payload)[0], 409)
        path = f'/api/media/{upload["mediaId"]}'
        self.assertEqual(self.request(path, token=outsider)[0], 404)
        self.assertEqual(self.request(path + "/process", "POST", token=token)[0], 200)
        deadline = time.monotonic() + 5
        while self.request(path, token=token)[1]["status"] != "COMPLETED":
            if time.monotonic() > deadline:
                self.fail("document processing did not complete")
            time.sleep(0.01)
        self.assertEqual(self.request(path + "/transcript", token=token)[1], {"pages": [payload.decode()], "needsOcr": False, "version": 1})

    def test_page_and_all_static_assets(self):
        status, page = self.request("/")
        self.assertEqual(status, 200)
        assets = re.findall(r'(?:src|href)="(/static/[^\"]+)"', page.decode())
        self.assertTrue(assets)
        for asset in assets:
            with self.subTest(asset=asset):
                status, content = self.request(asset)
                self.assertEqual(status, 200)
                self.assertTrue(content)
        self.assertEqual(self.request("/api/languages")[1], ["ko", "en", "ja", "zh-CN", "es", "fr"])

    def test_preferences_and_explicit_read_receipts(self):
        token = self.signup("reader")
        room = self.room(token, "Read room")
        path = "/api/users/me/preferences"
        changes = {"uiLocale": "en", "sourceLanguage": "ja", "defaultTargetLanguage": "fr", "preferredSubtitleLanguage": "es"}
        self.assertEqual(self.request(path, "PATCH", changes, token), (200, changes))
        self.assertEqual(self.request(path, token=token)[1], changes)
        self.assertEqual(self.request(path, "PATCH", {"uiLocale": "invalid"}, token)[0], 422)
        self.assertEqual(self.request(path, "PATCH", {"sourceLanguage": None}, token)[0], 422)
        self.assertEqual(self.request(path, "PATCH", {"unknown": "ko"}, token)[0], 422)
        runtime.store.append(room, "reader", "unread message", "en")
        listed = next(item for item in self.request("/api/rooms", token=token)[1] if item["roomId"] == room)
        self.assertEqual((listed["role"], listed["lastReadSeq"], listed["unreadCount"]), ("owner", 0, 1))
        self.assertEqual(self.request(f"/api/rooms/{room}/read", "POST", {"lastReadSeq": 9}, token)[0], 422)
        self.assertEqual(self.request(f"/api/rooms/{room}/read", "POST", {"lastReadSeq": 1}, token)[1]["lastReadSeq"], 1)
        self.assertEqual(self.request(f"/api/rooms/{room}/read", "POST", {"lastReadSeq": 0}, token)[1]["lastReadSeq"], 1)

    def test_creator_only_deletion_disconnects_and_revokes_files(self):
        owner = self.signup("deleter")
        guest = self.signup("deleteGuest")
        room = self.room(owner, "Delete room")
        self.request(f"/api/rooms/{room}/join", "POST", token=guest)
        _, upload = self.request("/api/files/upload-url", "POST", {"roomId": room, "filename": "demo.txt", "size": 3}, owner)
        self.request(upload["uploadUrl"], "PUT", b"abc")
        self.assertEqual(self.request(f"/api/rooms/{room}", "DELETE", token=guest)[0], 403)
        with connect(f"ws://127.0.0.1:{self.port}/ws/{room}?lastSeq=1", proxy=None, open_timeout=5) as ws:
            ws.send(json.dumps({"type": "auth", "token": guest}))
            self.assertEqual(json.loads(ws.recv(timeout=5))["type"], "presence")
            self.assertEqual(self.request(f"/api/rooms/{room}", "DELETE", token=owner)[0], 200)
            deadline = time.monotonic() + 5
            while True:
                event = json.loads(ws.recv(timeout=max(.01, deadline - time.monotonic())))
                if event["type"] == "room:deleted":
                    break
                if time.monotonic() >= deadline:
                    self.fail("room deletion event not received")
        self.assertEqual(self.request(f'/api/media/{upload["mediaId"]}', token=owner)[0], 404)
        self.assertEqual(self.request(f"/api/rooms/{room}/read", "POST", {"lastReadSeq": 1}, owner)[0], 404)
        self.assertTrue(all(item["roomId"] != room for item in self.request("/api/rooms/public", token=owner)[1]))

    def test_duplicate_upload_completion_has_one_file_message(self):
        owner = self.signup("completeOwner")
        room = self.room(owner, "Complete room")
        _, upload = self.request("/api/files/upload-url", "POST", {"roomId": room, "filename": "demo.txt", "size": 3}, owner)
        self.assertEqual(self.request(upload["uploadUrl"], "PUT", b"abc")[0], 200)
        for _ in range(3):
            self.assertEqual(self.request(upload["completeUrl"], "POST", token=owner)[1]["messageSeq"], 1)
        self.assertEqual(len(runtime.store.after(room, 0)), 1)

    def test_health_checks(self):
        self.assertEqual(self.request("/health/live"), (200, {"status": "ok"}))
        self.assertEqual(self.request("/health/ready"), (200, {"status": "ok"}))


if __name__ == "__main__":
    unittest.main()

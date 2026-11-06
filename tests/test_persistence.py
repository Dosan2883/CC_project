import concurrent.futures
import tempfile
import time
import json
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import support
import boto3
from moto import mock_aws
from fastapi import HTTPException

from app.auth.repository import AccountRepository, PersistentThrottle
from app.auth.service import AuthService, TokenSigner
from app.chat.repository import MessageStore
from app.core.database import Change, Conflict, DynamoDatabase, SQLiteDatabase
from app.rooms.repository import RoomRepository
from app.users.service import PreferencesService


def create_room(repo, name="room"):
    repo.create({"roomId": name, "name": name, "key": name, "visibility": "public", "createdBy": "owner", "createdAt": int(time.time() * 1000)})


class RepositoryChecks:
    def test_accounts_settings_roles_and_messages_survive_reopening(self):
        first = self.make_db()
        accounts = AccountRepository(first)
        service = AuthService(accounts, TokenSigner("persistent-test-secret-key-00000000"), PersistentThrottle(first, "test"))
        _, token = service.signup("owner", "secret123")
        PreferencesService(first).update("owner", {"sourceLanguage": "ja", "defaultTargetLanguage": "fr"})
        rooms = RoomRepository(first)
        create_room(rooms)
        rooms.join("room", "guest")
        store = MessageStore(first, rooms)
        store.append("room", "owner", "hello", "en")
        rooms.mark_read("room", "guest", 1)
        reopened = self.make_db()
        auth = AuthService(AccountRepository(reopened), service.signer, PersistentThrottle(reopened, "test"))
        self.assertEqual(auth.verify(token), "owner")
        self.assertEqual(auth.login("OWNER", "secret123")[0], "owner")
        restored_rooms = RoomRepository(reopened)
        self.assertEqual(restored_rooms.member("room", "owner")["role"], "owner")
        self.assertEqual(restored_rooms.member("room", "guest")["lastReadSeq"], 1)
        self.assertEqual(PreferencesService(reopened).get("owner")["defaultTargetLanguage"], "fr")
        self.assertEqual(MessageStore(reopened, restored_rooms).after("room", 0)[0]["bodyOriginal"], "hello")

    def test_transaction_rolls_back_and_stale_version_is_rejected(self):
        db = self.make_db()
        db.commit([Change("TEST", "a", None, {"value": 1})])
        with self.assertRaises(Conflict):
            db.commit([Change("TEST", "b", None, {}), Change("TEST", "a", None, {})])
        self.assertIsNone(db.get("TEST", "b"))
        record = db.get("TEST", "a")
        db.commit([record.change({"value": 2})])
        with self.assertRaises(Conflict):
            db.commit([record.change({"value": 3})])
        self.assertEqual(db.get("TEST", "a").value["value"], 2)

    def test_duplicate_room_name_is_atomic_and_delete_keeps_access_revoked(self):
        db = self.make_db()
        repo = RoomRepository(db)
        create_room(repo)
        with self.assertRaises(HTTPException) as duplicate:
            repo.create({"roomId": "second", "name": "room", "key": "room", "createdBy": "owner", "createdAt": 1})
        self.assertEqual(duplicate.exception.status_code, 409)
        self.assertIsNone(db.get("CATALOG", "ROOM#second"))
        repo.delete("room", "owner")
        with self.assertRaises(HTTPException):
            MessageStore(db, repo).append("room", "owner", "must fail", "en")
        self.assertIsNone(repo.find_by_name("room"))


class SQLiteChecks(RepositoryChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def make_db(self):
        return SQLiteDatabase(Path(self.temp.name) / "data.sqlite3")

    def test_independent_writers_assign_ordered_unique_sequences(self):
        db = self.make_db()
        create_room(RoomRepository(db))
        def send(number):
            independent = self.make_db()
            return MessageStore(independent, RoomRepository(independent)).append("room", "owner", str(number), "en")["messageSeq"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            sequences = list(pool.map(send, range(32)))
        self.assertEqual(sorted(sequences), list(range(1, 33)))
        messages = MessageStore(db, RoomRepository(db)).after("room", 0)
        self.assertEqual([message["messageSeq"] for message in messages], list(range(1, 33)))

    def test_new_process_restores_account_and_messages(self):
        db = self.make_db()
        auth = AuthService(AccountRepository(db), TokenSigner("restart-test-signing-secret-0000000"), PersistentThrottle(db, "auth"))
        _, token = auth.signup("owner", "secret123")
        rooms = RoomRepository(db)
        create_room(rooms)
        MessageStore(db, rooms).append("room", "owner", "survives process restart", "en")
        program = '''
import json, sys
from pathlib import Path
from app.core.database import SQLiteDatabase
from app.auth.repository import AccountRepository, PersistentThrottle
from app.auth.service import AuthService, TokenSigner
from app.chat.repository import MessageStore
from app.rooms.repository import RoomRepository
data = json.load(sys.stdin)
db = SQLiteDatabase(Path(data["path"]))
auth = AuthService(AccountRepository(db), TokenSigner("restart-test-signing-secret-0000000"), PersistentThrottle(db, "auth"))
print(json.dumps({"user": auth.verify(data["token"]), "messages": MessageStore(db, RoomRepository(db)).after("room", 0)}))
'''
        result = subprocess.run([sys.executable, "-c", program], input=json.dumps({"path": str(db.path), "token": token}), text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        restored = json.loads(result.stdout)
        self.assertEqual(restored["user"], "owner")
        self.assertEqual(restored["messages"][0]["bodyOriginal"], "survives process restart")


class DynamoChecks(RepositoryChecks, unittest.TestCase):
    def setUp(self):
        context = mock_aws()
        context.start()
        self.addCleanup(context.stop)
        self.client = boto3.client("dynamodb", region_name="us-east-1")
        self.client.create_table(TableName="test-messenger", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}], AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")

    def make_db(self):
        return DynamoDatabase("test-messenger", self.client)

    def test_upload_completion_atomically_saves_file_message_and_pending_job(self):
        from app.media.service import MediaService
        from app.media.storage import LocalStorage
        db = self.make_db()
        rooms = RoomRepository(db)
        create_room(rooms)
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService(LocalStorage(Path(directory)), lambda *_: None, db, rooms)
            upload = media.request_upload("room", "owner", "demo.txt", 5)
            token = parse_qs(urlparse(upload["uploadUrl"]).query)["token"][0]
            media.receive_upload(upload["mediaId"], token, b"hello")
            media.complete_upload(upload["mediaId"], "owner")
            self.assertEqual(len(MessageStore(db, rooms).after("room", 0)), 1)
            self.assertEqual(db.get("MEDIA_JOBS", upload["mediaId"]).value["status"], "PENDING")
            self.assertTrue(media.work(upload["mediaId"]))
            self.assertEqual(media.transcript(upload["mediaId"])["pages"], ["hello"])

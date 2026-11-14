"""서버 한 대 AWS 구성(DynamoDB + S3) 준비 코드 검사. 실제 AWS 없이 가짜 클라이언트만 쓴다."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401  (테스트용 환경 설정)
from app.core import config
from app.core.database import DynamoDatabase
from app.media.storage import S3Storage
from scripts import migrate_to_aws, setup_aws


class FakeDynamo:
    class exceptions:
        class ConditionalCheckFailedException(Exception):
            pass
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, existing=()):
        self.items, self.existing, self.queries = [], set(existing), []

    def put_item(self, **args):
        key = (args["Item"]["pk"]["S"], args["Item"]["sk"]["S"])
        if key in self.existing:
            raise self.exceptions.ConditionalCheckFailedException()
        self.items.append(args["Item"])

    def query(self, **args):
        self.queries.append(args)
        return {"Items": []}


class FakeS3:
    class exceptions:
        class ClientError(Exception):
            def __init__(self, code):
                self.response = {"Error": {"Code": code}}

    def __init__(self, existing=()):
        self.existing, self.uploaded, self.presigned = set(existing), [], []

    def head_object(self, Bucket, Key):
        if Key not in self.existing:
            raise self.exceptions.ClientError("404")

    def upload_file(self, path, bucket, key):
        self.uploaded.append((bucket, key, Path(path).read_bytes()))

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.presigned.append((operation, Params, ExpiresIn))
        return "https://example-bucket.s3.amazonaws.com/signed"


class ConfigTests(unittest.TestCase):
    def create(self, **overrides):
        values = dict(BACKEND="dynamodb", DYNAMODB_TABLE="messenger", S3_BUCKET="bucket", MEDIA_QUEUE_URL="", REDIS_URL="", SINGLE_NODE=True)
        values.update(overrides)
        patches = [patch.object(config, key, value) for key, value in values.items()] + [patch.object(config, "aws_client", lambda service: FakeDynamo())]
        for item in patches:
            item.start()
        try:
            return config.create_database()
        finally:
            for item in patches:
                item.stop()

    def test_single_node_needs_only_secret_and_bucket(self):
        with patch.dict("os.environ", {"AUTH_SECRET": "x" * 40}):
            self.assertIsInstance(self.create(), DynamoDatabase)
            with self.assertRaises(RuntimeError):
                self.create(S3_BUCKET="")
        with patch.dict("os.environ", {"AUTH_SECRET": ""}), self.assertRaises(RuntimeError):
            self.create()

    def test_multi_node_still_requires_queue_and_redis(self):
        with patch.dict("os.environ", {"AUTH_SECRET": "x" * 40}), self.assertRaises(RuntimeError):
            self.create(SINGLE_NODE=False)
        with patch.dict("os.environ", {"AUTH_SECRET": "x" * 40}):
            self.assertIsInstance(self.create(SINGLE_NODE=False, MEDIA_QUEUE_URL="q", REDIS_URL="r"), DynamoDatabase)


class DynamoQueryTests(unittest.TestCase):
    def test_empty_prefix_does_not_use_begins_with(self):
        client = FakeDynamo()
        database = DynamoDatabase("t", client)
        database.query("MEDIA_JOBS")
        database.query("ROOM#r", "MEMBER#")
        self.assertEqual(client.queries[0]["KeyConditionExpression"], "pk = :pk")
        self.assertNotIn(":prefix", client.queries[0]["ExpressionAttributeValues"])
        self.assertIn("begins_with", client.queries[1]["KeyConditionExpression"])


class StorageTests(unittest.TestCase):
    def test_download_url_is_signed_with_content_type(self):
        client = FakeS3()
        url = S3Storage("bucket", client).download_url("original/m1", 3600, "audio/wav")
        self.assertTrue(url.startswith("https://"))
        self.assertEqual(client.presigned[0], ("get_object", {"Bucket": "bucket", "Key": "original/m1", "ResponseContentType": "audio/wav"}, 3600))


class SetupTests(unittest.TestCase):
    def test_upsert_env_changes_only_managed_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            env = Path(temp) / ".env"
            env.write_text("TRANSLATE_URL=https://example.com\nDATA_BACKEND=sqlite\n# S3_BUCKET=old\nSTT_BACKEND=whisper\n", encoding="utf-8")
            setup_aws.upsert_env(env, {"DATA_BACKEND": "dynamodb", "S3_BUCKET": "b"})
            lines = env.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, ["TRANSLATE_URL=https://example.com", "DATA_BACKEND=dynamodb", "# S3_BUCKET=old", "STT_BACKEND=whisper", "S3_BUCKET=b"])
            self.assertEqual(setup_aws.existing_keys(env), {"TRANSLATE_URL", "DATA_BACKEND", "STT_BACKEND", "S3_BUCKET"})

    def test_cors_and_public_access_settings(self):
        rule = setup_aws.CORS["CORSRules"][0]
        self.assertIn("PUT", rule["AllowedMethods"])
        self.assertIn("ETag", rule["ExposeHeaders"])


class MigrateTests(unittest.TestCase):
    def test_copies_records_and_files_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            connection = sqlite3.connect(data / "messenger.sqlite3")
            connection.execute("CREATE TABLE records (pk TEXT, sk TEXT, version INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(pk, sk))")
            connection.executemany("INSERT INTO records VALUES (?, ?, ?, ?)", [
                ("USER#a", "ACCOUNT", 3, json.dumps({"nickname": "a"})), ("ROOM#r", "MESSAGE#1", 1, json.dumps({"bodyOriginal": "안녕"}, ensure_ascii=False)),
                ("CONFIG", "AUTH_SECRET", 1, json.dumps({"secret": "do-not-copy"}))])
            connection.commit()
            connection.close()
            for rel, content in [("original/m1", b"audio"), ("extracted/m1/t.json", b"{}"), ("ai/results/j/r.json", b"{}"), ("other/skip.txt", b"x")]:
                path = data / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            dynamo = FakeDynamo(existing=[("USER#a", "ACCOUNT")])  # 이미 있는 항목
            copied, skipped = migrate_to_aws.copy_records(data / "messenger.sqlite3", dynamo, "t")
            self.assertEqual((copied, skipped), (1, 2))  # 새 항목 1개, 이미 있음 1개 + 비밀(CONFIG) 1개
            self.assertEqual(dynamo.items[0]["payload"]["S"], json.dumps({"bodyOriginal": "안녕"}, ensure_ascii=False))
            self.assertEqual(dynamo.items[0]["version"], {"N": "1"})
            self.assertFalse(any(item["pk"]["S"] == "CONFIG" for item in dynamo.items))
            s3 = FakeS3(existing={"extracted/m1/t.json"})
            copied, skipped = migrate_to_aws.copy_files(data, s3, "bucket")
            self.assertEqual((copied, skipped), (2, 1))
            self.assertEqual(sorted(key for _, key, _ in s3.uploaded), ["ai/results/j/r.json", "original/m1"])  # other/는 옮기지 않는다
            preview = FakeS3()
            self.assertEqual(migrate_to_aws.copy_files(data, preview, "bucket", dry_run=True), (3, 0))
            self.assertEqual(preview.uploaded, [])


if __name__ == "__main__":
    unittest.main()

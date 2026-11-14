"""SQLite/DynamoDB에 동일한 버전 조건부 트랜잭션을 제공한다."""
import base64
import json
import random
import sqlite3
import time
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=lambda item: {"$bytes": base64.b64encode(item).decode()})


def decode(value):
    return json.loads(value, object_hook=lambda item: base64.b64decode(item["$bytes"]) if set(item) == {"$bytes"} else item)


@dataclass
class Record:
    pk: str
    sk: str
    value: dict
    version: int

    def change(self, value, check=False):
        return Change(self.pk, self.sk, self.version, value, check)


@dataclass
class Change:
    pk: str
    sk: str
    expected: int | None
    value: dict | None
    check: bool = False


class Conflict(Exception):
    pass


class Database:
    def update(self, pk, sk, transform, default=None):
        for attempt in range(30):
            record = self.get(pk, sk)
            value = transform(record.value if record else default)
            try:
                self.commit([Change(pk, sk, record.version if record else None, value)])
                return value
            except Conflict:
                time.sleep(random.uniform(0, min(0.002 * 2 ** attempt, 0.1)))
        raise Conflict("concurrent update retry limit")

    def query_range(self, pk, lower, upper):
        return [record for record in self.query(pk, lower) if record.sk < upper]


class SQLiteDatabase(Database):
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS records (pk TEXT, sk TEXT, version INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(pk, sk))")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, pk, sk):
        with self.connect() as connection:
            row = connection.execute("SELECT version, payload FROM records WHERE pk=? AND sk=?", (pk, sk)).fetchone()
        return Record(pk, sk, decode(row[1]), row[0]) if row else None

    def query(self, pk, prefix=""):
        with self.connect() as connection:
            rows = connection.execute("SELECT sk, version, payload FROM records WHERE pk=? AND sk>=? AND sk<? ORDER BY sk", (pk, prefix, prefix + chr(0x10ffff))).fetchall()
        return [Record(pk, sk, decode(payload), version) for sk, version, payload in rows]

    def query_range(self, pk, lower, upper):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT sk, version, payload FROM records WHERE pk=? AND sk>=? AND sk<? ORDER BY sk",
                (pk, lower, upper),
            ).fetchall()
        return [Record(pk, sk, decode(payload), version) for sk, version, payload in rows]

    def commit(self, changes):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for change in changes:
                row = connection.execute("SELECT version FROM records WHERE pk=? AND sk=?", (change.pk, change.sk)).fetchone()
                if (row[0] if row else None) != change.expected:
                    raise Conflict()
                if change.check:
                    continue
                if change.value is None:
                    connection.execute("DELETE FROM records WHERE pk=? AND sk=?", (change.pk, change.sk))
                else:
                    connection.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?)", (change.pk, change.sk, (change.expected or 0) + 1, encode(change.value)))

    def ping(self):
        with self.connect() as connection:
            connection.execute("SELECT 1")


class DynamoDatabase(Database):
    def __init__(self, table_name, client):
        self.table_name, self.client = table_name, client

    def get(self, pk, sk):
        item = self.client.get_item(TableName=self.table_name, Key={"pk": {"S": pk}, "sk": {"S": sk}}, ConsistentRead=True).get("Item")
        return self.record(item) if item else None

    @staticmethod
    def record(item):
        return Record(item["pk"]["S"], item["sk"]["S"], decode(item["payload"]["S"]), int(item["version"]["N"]))

    def query(self, pk, prefix=""):
        if prefix:
            args = dict(TableName=self.table_name, KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)", ExpressionAttributeValues={":pk": {"S": pk}, ":prefix": {"S": prefix}}, ConsistentRead=True)
        else:  # DynamoDB는 빈 문자열을 키 조건에 쓸 수 없다. 파티션 전체를 읽는다
            args = dict(TableName=self.table_name, KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": {"S": pk}}, ConsistentRead=True)
        records = []
        while True:
            response = self.client.query(**args)
            records.extend(self.record(item) for item in response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                return records
            args["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def query_range(self, pk, lower, upper):
        args = dict(
            TableName=self.table_name,
            KeyConditionExpression="pk = :pk AND sk BETWEEN :lower AND :upper",
            ExpressionAttributeValues={
                ":pk": {"S": pk}, ":lower": {"S": lower}, ":upper": {"S": upper},
            },
            ConsistentRead=True,
        )
        records = []
        while True:
            response = self.client.query(**args)
            records.extend(self.record(item) for item in response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                return records
            args["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def commit(self, changes):
        actions = []
        for change in changes:
            key = {"pk": {"S": change.pk}, "sk": {"S": change.sk}}
            args = dict(TableName=self.table_name, ConditionExpression="attribute_not_exists(pk)" if change.expected is None else "#version = :version")
            if change.expected is not None:
                args.update(ExpressionAttributeNames={"#version": "version"}, ExpressionAttributeValues={":version": {"N": str(change.expected)}})
            if change.check:
                actions.append({"ConditionCheck": {**args, "Key": key}})
            elif change.value is None:
                actions.append({"Delete": {**args, "Key": key}})
            else:
                payload = encode(change.value)
                if len(payload.encode()) > 350_000:
                    raise ValueError("record exceeds DynamoDB payload limit")
                actions.append({"Put": {**args, "Item": {**key, "version": {"N": str((change.expected or 0) + 1)}, "payload": {"S": payload}}}})
        try:
            self.client.transact_write_items(TransactItems=actions)
        except self.client.exceptions.TransactionCanceledException as error:
            reasons = error.response.get("CancellationReasons", [])
            if any(reason.get("Code") not in (None, "None", "ConditionalCheckFailed", "TransactionConflict") for reason in reasons):
                raise
            raise Conflict() from error

    def ping(self):
        self.get("HEALTH", "CHECK")

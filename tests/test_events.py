import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import support
import fakeredis
from fakeredis.aioredis import FakeRedis

from app.chat.connections import ConnectionManager
from app.chat.repository import MessageStore
from app.core.database import SQLiteDatabase
from app.rooms.repository import RoomRepository
from test_persistence import create_room


class Socket:
    def __init__(self):
        self.events, self.closed = [], None

    async def send_json(self, event):
        self.events.append(event)

    async def close(self, code=1000):
        self.closed = code


class DistributedEventsChecks(unittest.IsolatedAsyncioTestCase):
    async def test_resubscribes_after_a_receive_failure(self):
        redis = FakeRedis(decode_responses=True)
        manager = ConnectionManager(None, None, "redis://fake", "reconnect")
        with patch("redis.asyncio.Redis.from_url", return_value=redis):
            await manager.start()
        try:
            original = manager.bus.pubsub
            with self.assertLogs("app.chat.events", level="ERROR"), patch.object(original, "get_message", side_effect=ConnectionError("connection lost")):
                for _ in range(200):
                    replacement = manager.bus.pubsub
                    if replacement is not None and replacement is not original:
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(manager.bus.pubsub)
                self.assertIsNot(manager.bus.pubsub, original)
            self.assertTrue(manager.bus.pubsub.subscribed)
        finally:
            await manager.stop()

    async def test_leaver_is_removed_before_presence_and_presence_skips_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            db = SQLiteDatabase(Path(directory) / "presence.sqlite3")
            rooms = RoomRepository(db)
            create_room(rooms)
            rooms.join("room", "guest")
            store = MessageStore(db, rooms)
            manager = ConnectionManager(store, rooms)
            owner, guest = Socket(), Socket()
            manager.add("room", "owner", owner)
            manager.add("room", "guest", guest)
            rooms.leave("room", "guest")
            await manager.deliver("room", {"type":"member:left", "user":"guest"})
            self.assertEqual(guest.closed, 4000)
            self.assertEqual(manager.connection_count, 1)
            with patch.object(store, "after", wraps=store.after) as replay:
                await manager.publish_presence()
                replay.assert_not_called()
            self.assertEqual(owner.events[-1], {"type":"presence", "members":["owner"]})

    async def test_cross_server_delivery_reorders_from_database_and_revokes_deleted_room(self):
        with tempfile.TemporaryDirectory() as directory:
            db = SQLiteDatabase(Path(directory) / "test.sqlite3")
            rooms = RoomRepository(db)
            create_room(rooms)
            rooms.join("room", "guest")
            store = MessageStore(db, rooms)
            first = ConnectionManager(store, rooms, "redis://fake", "test")
            second = ConnectionManager(store, rooms, "redis://fake", "test")
            redis_server = fakeredis.FakeServer()
            clients = [FakeRedis(server=redis_server, decode_responses=True) for _ in range(2)]
            with patch("redis.asyncio.Redis.from_url", side_effect=clients):
                await first.start()
                await second.start()
            owner, guest = Socket(), Socket()
            first.add("room", "owner", owner)
            second.add("room", "guest", guest)
            try:
                await first.publish_presence()
                await second.publish_presence()
                for _ in range(100):
                    if first.members("room") == ["guest", "owner"]:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(first.members("room"), ["guest", "owner"])
                earlier = store.append("room", "owner", "first", "en")
                later = store.append("room", "owner", "second", "en")
                await first.broadcast("room", later)
                await first.broadcast("room", earlier)
                for _ in range(100):
                    if len([event for event in guest.events if event["type"] == "message:new"]) == 2:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual([event["messageSeq"] for event in guest.events if event["type"] == "message:new"], [1, 2])
                rooms.delete("room", "owner")
                await first.broadcast("room", {"type": "room:deleted", "roomId": "room"})
                for _ in range(100):
                    if guest.closed:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(guest.closed, 4404)
                first.bus.remote["stopped"] = (time.monotonic() - 1, {"room": ["ghost"]})
                self.assertNotIn("ghost", first.members("room"))
            finally:
                await first.stop()
                await second.stop()

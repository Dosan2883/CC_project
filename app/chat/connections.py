"""연결은 각 서버, 이벤트와 접속 현황은 Redis, 기록은 DB에서 관리한다."""
import asyncio
import logging
from contextlib import suppress

from fastapi import HTTPException
from app.chat.events import EventBus

log = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self, store, rooms, redis_url="", namespace="messenger"):
        self.store, self.rooms = store, rooms
        self.conns, self.cursors, self.locks = {}, {}, {}
        self.bus = EventBus(redis_url, namespace, self.deliver, self.snapshot)

    def snapshot(self):
        return {room: list(users) for room, users in self.conns.items() if users}

    def members(self, room):
        return sorted(set(self.conns.get(room, {})) | self.bus.members(room))

    def add(self, room, user, ws, last_seq=0):
        self.conns.setdefault(room, {}).setdefault(user, set()).add(ws)
        self.cursors[ws], self.locks[ws] = last_seq, asyncio.Lock()

    def remove(self, room, user, ws):
        people = self.conns.get(room, {})
        sockets = people.get(user, set())
        sockets.discard(ws)
        if not sockets:
            people.pop(user, None)
        if not people:
            self.conns.pop(room, None)
        self.cursors.pop(ws, None)
        self.locks.pop(ws, None)

    async def replay(self, room, ws):
        for message in await asyncio.to_thread(self.store.after, room, self.cursors.get(ws, 0)):
            await ws.send_json(message)
            self.cursors[ws] = message["messageSeq"]

    async def deliver(self, room, payload):
        if payload["type"] == "presence:refresh":
            for rid in list(self.conns):
                await self.deliver(rid, {"type": "presence", "members": self.members(rid)})
            return
        for user, sockets in list(self.conns.get(room, {}).items()):
            for ws in list(sockets):
                lock = self.locks.get(ws)
                if lock is None:
                    continue
                try:
                    async with lock:
                        try:
                            await asyncio.to_thread(self.rooms.require_member, room, user)
                        except HTTPException as error:
                            if payload["type"] == "room:deleted":
                                await ws.send_json(payload)
                            await ws.close(code=4404 if error.status_code == 404 else 4000)
                            continue
                        await self.replay(room, ws)
                        if payload["type"] != "message:new":
                            await ws.send_json(payload)
                except Exception:
                    with suppress(Exception):
                        await ws.close()
                    self.remove(room, user, ws)

    async def broadcast(self, room, payload):
        # DB 저장은 호출자에게서 먼저 완료된다. Redis가 끊겨도 이 서버의
        # 연결에는 즉시 전달하고, DB replay가 복구된 다른 서버의 누락을 채운다.
        try:
            await self.bus.publish(room, payload)
        except Exception:
            log.exception("Redis publish failed; delivering from the local durable record")
        await self.deliver(room, payload)

    async def publish_presence(self):
        try:
            await self.bus.presence()
        except Exception:
            log.exception("Redis presence failed; local sockets remain available")
        await self.deliver("", {"type": "presence:refresh"})

    async def start(self):
        await self.bus.start()

    async def stop(self):
        for users in list(self.conns.values()):
            for sockets in list(users.values()):
                for ws in list(sockets):
                    with suppress(Exception):
                        await ws.close(code=1001)
        await self.bus.stop()

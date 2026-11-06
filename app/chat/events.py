"""Redis Pub/Sub와 TTL이 있는 서버별 접속 현황."""
import asyncio
import json
import logging
import time
import uuid
from contextlib import suppress

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, redis_url, namespace, handler, snapshot):
        self.url, self.channel = redis_url, namespace + ":events"
        self.handler, self.snapshot = handler, snapshot
        self.node = uuid.uuid4().hex
        self.remote = {}
        self.tasks = []
        self.redis = None

    async def start(self):
        if not self.url:
            return
        from redis.asyncio import Redis
        self.redis = Redis.from_url(self.url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        await self.redis.ping()
        self.pubsub = self.redis.pubsub()
        await self.pubsub.subscribe(self.channel)
        self.tasks = [asyncio.create_task(self.listen()), asyncio.create_task(self.heartbeat())]
        await self.publish("", {"type": "presence:request"})

    async def publish(self, room, payload):
        if self.redis:
            await self.redis.publish(self.channel, json.dumps({"node": self.node, "room": room, "payload": payload}))

    async def presence(self):
        payload = {"type": "presence:node", "rooms": self.snapshot()}
        await self.publish("", payload)

    async def listen(self):
        while True:
            try:
                message = await self.pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                if not message:
                    await asyncio.sleep(0.01)
                    continue
                data = json.loads(message["data"])
                if data["node"] == self.node:
                    continue
                payload = data["payload"]
                if payload["type"] == "presence:request":
                    await self.presence()
                elif payload["type"] == "presence:node":
                    self.remote[data["node"]] = (time.monotonic() + 35, payload["rooms"])
                    await self.handler("", {"type": "presence:refresh"})
                else:
                    await self.handler(data["room"], payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Redis event listener failed; reconnecting")
                await asyncio.sleep(1)

    async def heartbeat(self):
        while True:
            try:
                await self.presence()
                await self.handler("", {"type": "presence:refresh"})
            except Exception:
                log.exception("Redis presence heartbeat failed")
            await asyncio.sleep(10)

    def members(self, room):
        users = set()
        for node, (expires, rooms) in list(self.remote.items()):
            if expires <= time.monotonic():
                self.remote.pop(node, None)
            else:
                users.update(rooms.get(room, []))
        return users

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        self.tasks.clear()
        if self.redis:
            await self.pubsub.aclose()
            await self.redis.aclose()
            self.redis = None

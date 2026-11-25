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
        self.pubsub = None

    async def start(self):
        if not self.url:
            return
        from redis.asyncio import Redis
        self.redis = Redis.from_url(self.url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        try:
            await self.redis.ping()
            await self.subscribe()
        except Exception:
            log.exception("Redis startup failed; the event listener will retry")
        self.tasks = [asyncio.create_task(self.listen()), asyncio.create_task(self.heartbeat())]

    async def subscribe(self):
        pubsub = self.redis.pubsub()
        try:
            await pubsub.subscribe(self.channel)
            await self.publish("", {"type": "presence:request"})
        except BaseException:
            with suppress(Exception):
                await pubsub.aclose()
            raise
        self.pubsub = pubsub

    async def publish(self, room, payload):
        if self.redis:
            await self.redis.publish(self.channel, json.dumps({"node": self.node, "room": room, "payload": payload}))

    async def presence(self):
        payload = {"type": "presence:node", "rooms": self.snapshot()}
        await self.publish("", payload)

    async def listen(self):
        while True:
            try:
                if self.pubsub is None:
                    await self.subscribe()
                    await self.handler("", {"type": "events:reconnected"})
                message = await self.pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
            except Exception:
                log.exception("Redis subscription failed; reconnecting")
                await self.close_subscription()
                await asyncio.sleep(1)
                continue
            if not message:
                await asyncio.sleep(0.01)
                continue
            try:
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
            except Exception:
                log.exception("Redis event delivery failed")

    async def close_subscription(self):
        pubsub, self.pubsub = self.pubsub, None
        if pubsub is not None:
            with suppress(Exception):
                await pubsub.aclose()

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
        await self.close_subscription()
        redis, self.redis = self.redis, None
        self.remote.clear()
        if redis is not None:
            await redis.aclose()

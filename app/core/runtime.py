"""설정에 따라 공용 영속 저장소·이벤트 버스·워커를 구성한다."""
import asyncio
import logging
import os
import secrets

from app.auth.repository import AccountRepository, PersistentThrottle
from app.auth.service import AuthService, TokenSigner, load_secret
from app.chat.connections import ConnectionManager
from app.chat.repository import MessageStore
from app.core import config
from app.media.service import MediaService
from app.media.storage import LocalStorage, S3Storage
from app.rooms.repository import RoomRepository
from app.users.service import PreferencesService

db = config.create_database()
room_repo = RoomRepository(db)
store = MessageStore(db, room_repo)
preferences = PreferencesService(db)
room_throttle = PersistentThrottle(db, "rooms")
if os.getenv("AUTH_SECRET"):
    secret = load_secret()
else:
    secret = db.update("CONFIG", "AUTH_SECRET", lambda value: value or {"secret": secrets.token_urlsafe(48)})["secret"]
auth = AuthService(AccountRepository(db), TokenSigner(secret), PersistentThrottle(db, "auth"))
manager = ConnectionManager(store, room_repo, config.REDIS_URL, config.DYNAMODB_TABLE or "messenger")
loop = None
sync_redis = None


def notify_room(room_id, payload):
    global sync_redis
    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(manager.broadcast(room_id, payload), loop)
        def completed(result):
            try:
                result.result()
            except Exception:
                logging.getLogger(__name__).exception("Media notification failed")
        future.add_done_callback(completed)
    elif config.REDIS_URL:
        import json
        from redis import Redis
        if sync_redis is None:
            sync_redis = Redis.from_url(config.REDIS_URL, socket_connect_timeout=5, socket_timeout=5)
        sync_redis.publish(manager.bus.channel, json.dumps({"node": "worker", "room": room_id, "payload": payload}))


storage = S3Storage(config.S3_BUCKET, config.aws_client("s3")) if config.S3_BUCKET else LocalStorage(config.DATA_DIR)
sqs = config.aws_client("sqs") if config.MEDIA_QUEUE_URL else None
media_svc = MediaService(storage, notify_room, db, room_repo, sqs, config.MEDIA_QUEUE_URL)

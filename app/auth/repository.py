"""계정과 실패 횟수를 공용 영속 저장소에 기록한다."""
import time
import uuid

from app.core.database import Change, Conflict


class ThrottleBlocked(Exception):
    pass


class AccountRepository:
    def __init__(self, db):
        self.db = db

    def get(self, key):
        record = self.db.get("USER#" + key, "ACCOUNT")
        return record.value if record else None

    def create(self, key, record, preferences=None):
        try:
            changes = [Change("USER#" + key, "ACCOUNT", None, record)]
            if preferences is not None:
                changes.append(Change("USER#" + key, "PREFERENCES", None, preferences))
            self.db.commit(changes)
            return True
        except Conflict:
            return False


class PersistentThrottle:
    def __init__(self, db, namespace, limit=5, window=60):
        self.db, self.namespace, self.limit, self.window = db, namespace, limit, window

    def reserve(self, key):
        attempt = uuid.uuid4().hex

        def claim(value):
            now = time.time()
            value = value or {}
            fails = [stamp for stamp in value.get("fails", []) if stamp > now - self.window]
            pending = {item: stamp for item, stamp in value.get("pending", {}).items() if stamp > now - self.window}
            if len(fails) + len(pending) >= self.limit:
                raise ThrottleBlocked
            return {"fails": fails, "pending": {**pending, attempt: now}}

        try:
            self.db.update("THROTTLE#" + self.namespace, key, claim)
        except ThrottleBlocked:
            return None
        return attempt

    def finish(self, key, attempt, success):
        def finish(value):
            now = time.time()
            value = value or {}
            pending = {item: stamp for item, stamp in value.get("pending", {}).items() if stamp > now - self.window}
            reserved_at = pending.pop(attempt, None)
            fails = [stamp for stamp in value.get("fails", []) if stamp > now - self.window]
            if reserved_at is not None:
                fails = [stamp for stamp in fails if stamp > reserved_at] if success else [*fails, reserved_at]
            return {"fails": fails, "pending": pending}

        self.db.update("THROTTLE#" + self.namespace, key, finish)

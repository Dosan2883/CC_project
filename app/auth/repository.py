"""계정과 실패 횟수를 공용 영속 저장소에 기록한다."""
import time

from app.core.database import Change, Conflict


class AccountRepository:
    def __init__(self, db):
        self.db = db

    def get(self, key):
        record = self.db.get("USER#" + key, "ACCOUNT")
        return record.value if record else None

    def create(self, key, record):
        try:
            self.db.commit([Change("USER#" + key, "ACCOUNT", None, record)])
            return True
        except Conflict:
            return False


class PersistentThrottle:
    def __init__(self, db, namespace, limit=5, window=60):
        self.db, self.namespace, self.limit, self.window = db, namespace, limit, window

    def blocked(self, key):
        record = self.db.get("THROTTLE#" + self.namespace, key)
        return bool(record and len([stamp for stamp in record.value["fails"] if stamp > time.time() - self.window]) >= self.limit)

    def fail(self, key):
        self.db.update("THROTTLE#" + self.namespace, key, lambda value: {"fails": [stamp for stamp in (value or {}).get("fails", []) if stamp > time.time() - self.window] + [time.time()]})

    def clear(self, key):
        self.db.update("THROTTLE#" + self.namespace, key, lambda _: {"fails": []})

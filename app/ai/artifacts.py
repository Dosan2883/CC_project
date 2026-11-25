"""S3-first results with durable write recovery and cross-process leases."""
import hashlib
import json
import logging
import threading
import time
import uuid
from fastapi import HTTPException
from app.core.database import Conflict

log = logging.getLogger(__name__)
MAX_INLINE_RESULT = 300_000

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class Artifacts:
    def __init__(self, db, storage_provider):
        self.db = db
        self._storage_provider = storage_provider if callable(storage_provider) else lambda: storage_provider

    @property
    def storage(self):
        return self._storage_provider()

    def read(self, identity, key):
        try:
            raw = self.storage.optional(key)
        except Exception as error:
            raise HTTPException(503, "RESULT_STORAGE_UNAVAILABLE") from error
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise HTTPException(502, "RESULT_STORAGE_INVALID") from error
        if not isinstance(value, dict) or value.get("identity") != identity or "result" not in value:
            raise HTTPException(409, "RESULT_IDENTITY_MISMATCH")
        return value["result"]

    def run(self, identity, key, producer, verify=lambda: None, *, allow_uncertain=False):
        verify()
        existing = self.read(identity, key)
        if existing is not None:
            self.reconcile_saved(identity, key)
            verify()
            return existing, True
        result_id, token = digest(identity), uuid.uuid4().hex
        deadline = time.monotonic() + 30
        while True:
            now = time.time()
            def claim(value):
                if value and value.get("leaseUntil", 0) > now:
                    return value
                return {"phase":"CLAIMED", **(value or {}), "leaseToken":token, "leaseUntil":now+180}
            value = self.db.update("AI_ARTIFACTS", result_id, claim)
            if value.get("leaseToken") == token:
                break
            existing = self.read(identity, key)
            if existing is not None:
                verify()
                return existing, True
            if time.monotonic() >= deadline:
                raise HTTPException(503, "RESULT_IN_PROGRESS")
            time.sleep(.05)
        def save(changes, remove=()):
            def update(current):
                if not current or current.get("leaseToken") != token:
                    raise HTTPException(409, "RESULT_LEASE_LOST")
                updated = {**current, **changes}
                for field in remove:
                    updated.pop(field, None)
                return updated
            return self.db.update("AI_ARTIFACTS", result_id, update)
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(30):
                try:
                    save({"leaseUntil":time.time()+180})
                except Exception:
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        pending = value.get("pendingResult")
        stage = value.get("pendingKey")
        completed = False
        try:
            verify()
            if pending is None and stage:
                pending = self.read(identity, stage)
            if pending is None:
                if value.get("phase") in ("CALLING", "SAVING") and not allow_uncertain:
                    raise HTTPException(409, "AI_CALL_UNCERTAIN")
                parts = key.split("/", 2)
                parent = "/".join(parts[:2]) + "/" if len(parts) == 3 and parts[0] == "rooms" else ""
                stage = f"{parent}ai-staging/{token}.json"
                save({"phase":"CALLING", "pendingKey":stage}, remove=("pendingResult",))
                try:
                    pending = producer()
                except Exception:
                    save({"phase":"FAILED"}, remove=("pendingKey",))
                    raise
                raw = json.dumps({"identity":identity, "result":pending}, ensure_ascii=False).encode()
                if len(raw) > MAX_INLINE_RESULT:
                    self.write(stage, raw)
                    save({"phase":"SAVING"})
                else:
                    save({"phase":"SAVING", "pendingResult":pending}, remove=("pendingKey",))
                    stage = None
            verify()
            self.write(key, json.dumps({"identity":identity, "result":pending}, ensure_ascii=False).encode())
            stop.set()
            thread.join(timeout=1)
            save({"phase":"COMPLETED", "leaseUntil":0, "resultKey":key}, remove=("pendingResult", "pendingKey", "leaseToken"))
            completed = True
            self.remove_stage(stage)
            verify()
            return pending, False
        finally:
            stop.set()
            thread.join(timeout=1)
            # Keep a produced response durable when S3 alone failed; never call AI again for it.
            if not completed:
                try:
                    save({"leaseUntil":0})
                except Exception:
                    pass

    def write(self, key, data):
        try:
            self.storage.put(key, data)
        except Exception as error:
            raise HTTPException(503, "RESULT_STORAGE_UNAVAILABLE") from error

    def reconcile_saved(self, identity, key):
        try:
            record = self.db.get("AI_ARTIFACTS", digest(identity))
            if not record or record.value.get("leaseUntil", 0) > time.time():
                return
            value = dict(record.value)
            stage = value.pop("pendingKey", None)
            if "pendingResult" not in value and stage is None:
                return
            value.pop("pendingResult", None)
            value.pop("leaseToken", None)
            self.db.commit([record.change({**value, "phase":"COMPLETED", "leaseUntil":0, "resultKey":key})])
            self.remove_stage(stage)
        except Conflict:
            pass
        except Exception:
            log.exception("AI artifact metadata cleanup failed")

    def remove_stage(self, key):
        if key is not None:
            try:
                self.storage.delete(key)
            except Exception:
                log.exception("AI staging object cleanup failed")

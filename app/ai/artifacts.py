"""S3-first results with durable write recovery and cross-process leases."""
import hashlib
import json
import threading
import time
import uuid
from fastapi import HTTPException


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class Artifacts:
    def __init__(self, db, storage):
        self.db, self.storage = db, storage

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
        if value.get("identity") != identity:
            raise HTTPException(409, "RESULT_IDENTITY_MISMATCH")
        return value["result"]

    def run(self, identity, key, producer, verify=lambda: None):
        verify()
        existing = self.read(identity, key)
        if existing is not None:
            verify()
            return existing, True
        result_id, token = digest(identity), uuid.uuid4().hex
        deadline = time.monotonic() + 30
        while True:
            now = time.time()
            def claim(value):
                if value and value.get("leaseUntil", 0) > now:
                    return value
                if value and value.get("phase") == "CALLING" and not value.get("pendingResult"):
                    raise HTTPException(409, "AI_CALL_UNCERTAIN")
                return {**(value or {}), "leaseToken":token, "leaseUntil":now+180, "phase":"CLAIMED"}
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
        def save(changes):
            def update(current):
                if not current or current.get("leaseToken") != token:
                    raise HTTPException(409, "RESULT_LEASE_LOST")
                return {**current, **changes}
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
        try:
            verify()
            if pending is None:
                save({"phase":"CALLING"})
                try:
                    pending = producer()
                except Exception:
                    save({"phase":"FAILED", "leaseUntil":0})
                    raise
                save({"phase":"SAVING", "pendingResult":pending})
            verify()
            try:
                self.storage.put(key, json.dumps({"identity":identity, "result":pending}, ensure_ascii=False).encode())
            except Exception as error:
                raise HTTPException(503, "RESULT_STORAGE_UNAVAILABLE") from error
            save({"phase":"COMPLETED", "leaseUntil":0, "resultKey":key})
            verify()
            return pending, False
        finally:
            stop.set()
            thread.join(timeout=1)
            # Keep a produced response durable when S3 alone failed; never call AI again for it.
            try:
                save({"leaseUntil":0})
            except Exception:
                pass

"""Durable source refresh outbox. Runs in the existing AI worker recovery loop."""
import hashlib
import json
import logging
import threading
import time
import uuid
from fastapi import HTTPException
from app.core.database import Change, Conflict
from app.media.queue import pending_jobs
from app.media.service import MediaError
from app.media.subtitles import webvtt

log = logging.getLogger(__name__)
PARTITION = "SOURCE_REFRESH"
LEASE_SECONDS = 180


def request(room, source, revision):
    key = hashlib.sha256(json.dumps([room, source, revision], sort_keys=True).encode()).hexdigest()
    return [Change(PARTITION, key, None, {"room":room, "source":source, "status":"PENDING", "attempts":0}),
            Change("PENDING_JOBS#" + PARTITION, key, None, {"jobId":key})]


def recover(jobs):
    for record in pending_jobs(jobs.db, PARTITION):
        task = record.value
        now = time.time()
        if max(task.get("availableAt", 0), task.get("leaseUntil", 0)) > now:
            continue
        lease = uuid.uuid4().hex
        attempts = task["attempts"] + 1
        try:
            if attempts > 3:
                jobs.db.commit([record.change({**task, "status":"FAILED", "leaseUntil":0})])
                continue
            jobs.db.commit([record.change({**task, "status":"PROCESSING", "attempts":attempts,
                                           "leaseToken":lease, "leaseUntil":now + LEASE_SECONDS})])
        except Conflict:
            continue
        run_refresh(jobs, record.sk, lease, task, attempts)


def run_refresh(jobs, key, lease, task, attempts):
    stop = threading.Event()

    def save(changes):
        def update(value):
            if value.get("leaseToken") != lease or value["status"] != "PROCESSING":
                raise Conflict("source refresh lease lost")
            return {**value, **changes}
        jobs.db.update(PARTITION, key, update)

    def heartbeat():
        while not stop.wait(30):
            try:
                save({"leaseUntil":time.time() + LEASE_SECONDS})
            except Exception:
                log.exception("Source refresh lease renewal failed")
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        refresh_source(jobs, task)
        save({"status":"COMPLETED", "error":None, "leaseUntil":0})
    except Conflict:
        log.warning("Source refresh lease lost for %s", key)
    except Exception as error:
        log.exception("Source refresh failed for %s", key)
        try:
            save({"status":"FAILED" if attempts >= 3 else "PENDING", "leaseUntil":0,
                  "availableAt":time.time() + 2 ** attempts, "error":type(error).__name__})
        except Conflict:
            log.warning("Source refresh lease lost for %s", key)
    finally:
        stop.set()
        thread.join(timeout=1)


def refresh_source(jobs, task):
    room, source = task["room"], task["source"]
    media_id = source.get("mediaId")
    asset = None
    if media_id:
        try:
            asset = jobs.media.live_asset(media_id)
        except MediaError as error:
            if error.status == 404:
                return
            raise
    failures = []

    def attempt(run):
        try:
            run()
        except (HTTPException, MediaError) as error:
            status = error.status_code if isinstance(error, HTTPException) else error.status
            if status not in (403, 404, 422):
                failures.append(error)
        except Exception as error:
            failures.append(error)

    def refresh_summary(old):
        original = jobs.db.get("AI_JOBS", old["jobId"])
        if not original or not original.value.get("consent"):
            return
        body = {k:original.value[k] for k in ("roomId", "kind", "sourceLanguage", "target", "sources", "consent")}
        body["title"] = old.get("title") or original.value.get("title")
        jobs.translation.authorize_external(old["requester"], room)
        jobs.submit(body, old["requester"], restore_hidden=False)

    for record in jobs.db.query("ROOM_RESULTS#" + room):
        old = record.value
        if old.get("kind") != "translate" and any(
            all(s.get(k) == v for k, v in source.items()) for s in old.get("sources", [])
        ) and not jobs.db.get("AI_RESULT_HIDDEN#" + room, old["jobId"]):
            attempt(lambda: refresh_summary(old))

    if asset and asset.get("type") in ("audio", "video"):
        def refresh_original():
            transcript = jobs.media.transcript(media_id)
            jobs.media.storage.put(f"subtitles/{media_id}/{transcript.get('version', 1)}/original.vtt",
                                   webvtt(transcript.get("segments", [])).encode())

        def refresh_translation(user, target):
            jobs.translation.authorize_external(user, room)
            result = jobs.translation.translate_segments(media_id, target, user)
            key = f"subtitles/{media_id}/{result['version']}/{target}.vtt"
            jobs.media.storage.put(key, webvtt(result["segments"], True).encode())

        attempt(refresh_original)
        for sub in jobs.db.query("SUBTITLE#" + media_id):
            attempt(lambda: refresh_translation(sub.value["user"], sub.value["target"]))
    if failures:
        raise failures[0]

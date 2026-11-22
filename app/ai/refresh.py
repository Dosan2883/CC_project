"""Durable source refresh outbox. Runs in the existing AI worker recovery loop."""
import hashlib
import json
import time
from fastapi import HTTPException
from app.core.database import Change


def request(room, source, revision):
    key = hashlib.sha256(json.dumps([room, source, revision], sort_keys=True).encode()).hexdigest()
    return Change("SOURCE_REFRESH", key, None, {"room":room, "source":source, "status":"PENDING", "attempts":0})


def recover(jobs):
    for record in jobs.db.query("SOURCE_REFRESH"):
        task = record.value
        if task["status"] != "PENDING" or task.get("availableAt", 0) > time.time():
            continue
        try:
            for result in jobs.db.query("ROOM_RESULTS#" + task["room"]):
                old = result.value
                if jobs.db.get("AI_RESULT_HIDDEN#" + task["room"], old["jobId"]):
                    continue
                if old.get("kind") == "translate" or not any(
                    all(s.get(k) == v for k,v in task["source"].items()) for s in old.get("sources", [])):
                    continue
                original = jobs.db.get("AI_JOBS", old["jobId"])
                if not original or not original.value.get("consent"):
                    continue
                body = {k:original.value[k] for k in ("roomId", "kind", "sourceLanguage", "target", "sources", "consent")}
                try:
                    jobs.translation.authorize_external(old["requester"], task["room"])
                    jobs.submit(body, old["requester"])
                except HTTPException as error:
                    if error.status_code not in (403, 404):
                        raise
            if task["source"].get("mediaId"):
                from app.media.subtitles import webvtt
                media_id = task["source"]["mediaId"]
                transcript = jobs.media.transcript(media_id)
                jobs.media.storage.put(f"subtitles/{media_id}/{transcript.get('version', 1)}/original.vtt",
                                       webvtt(transcript.get("segments", [])).encode())
                for sub in jobs.db.query("SUBTITLE#" + media_id):
                    user, target = sub.value["user"], sub.value["target"]
                    try:
                        jobs.translation.authorize_external(user, task["room"])
                        result = jobs.translation.translate_segments(media_id, target, user)
                        key = f"subtitles/{media_id}/{result['version']}/{target}.vtt"
                        jobs.media.storage.put(key, webvtt(result["segments"], True).encode())
                    except HTTPException as error:
                        if error.status_code not in (403, 404):
                            raise
            jobs.db.update("SOURCE_REFRESH", record.sk, lambda value: {**value, "status":"COMPLETED"})
        except Exception as error:
            attempts = task["attempts"] + 1
            jobs.db.update("SOURCE_REFRESH", record.sk, lambda value: {
                **value, "attempts":attempts, "status":"FAILED" if attempts >= 3 else "PENDING",
                "availableAt":time.time() + 2 ** attempts, "error":type(error).__name__})

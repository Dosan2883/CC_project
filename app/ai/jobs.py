"""Durable AI selections, leased execution, retry and evidence results."""
import hashlib
import json
import os
import re
import time
import uuid
import threading

from fastapi import HTTPException
from app.core.database import Change, Conflict
from app.media.queue import MediaWorker
from app.media.service import MediaError
from app.ai.contract import MODEL_VERSION
from app.ai.service import LANGUAGE_NAMES, split_text, text_matches_language
from app.ai.artifacts import Artifacts, digest

SUMMARY_CACHE_VERSION = "2"  # 요약 프롬프트나 방식을 크게 바꾸면 올려서 이전 캐시를 쓰지 않게 한다


SCRIPTS = {"kana": re.compile("[぀-ヿ]"), "hangul": re.compile("[가-힣]"), "han": re.compile("[一-鿿]")}


def in_target_language(text, target):
    """모델이 지시를 무시하고 입력 언어로 답한 경우를 걸러 낸다. 글자 종류로만 확인하므로 일본어·한국어·중국어만 검사한다."""
    kana, hangul, han = (len(SCRIPTS[name].findall(text)) for name in ("kana", "hangul", "han"))
    if target == "ja":
        return kana > 0 and hangul <= kana
    if target == "ko":
        return hangul > 0
    if target == "zh-CN":
        return han > 0 and kana == 0 and hangul == 0
    return True


class AIJobs:
    jobs_partition = "AI_JOBS"

    def __init__(self, db, rooms, media, translation, sqs=None, queue_url=""):
        self.db, self.rooms, self.media, self.translation = db, rooms, media, translation
        self.queue = MediaWorker(self, sqs, queue_url)
        self.artifacts = Artifacts(db, lambda: self.media.storage)
        self._migrated_rooms = set()
        self._migration_lock = threading.Lock()
        self.translation.jobs = self

    def select(self, room, sources, user):
        self.rooms.require_member(room, user)
        evidence = []
        expanded = []
        for source in sources:
            if source["kind"] == "document":
                try:
                    asset = self.media.asset(source["mediaId"])
                except MediaError as error:
                    raise HTTPException(error.status, error.code)
                if asset["roomId"] != room:
                    raise HTTPException(404, "MEDIA_NOT_FOUND")
                try:
                    transcript = self.media.transcript(source["mediaId"])
                except MediaError as error:
                    raise HTTPException(error.status, error.code)
                if transcript.get("needsOcr") or any(not page.strip() for page in transcript["pages"]):
                    raise HTTPException(422, "OCR_REQUIRED")
                expanded.extend({"kind":"page", "mediaId":source["mediaId"], "page":i+1} for i in range(len(transcript["pages"])) if transcript["pages"][i].strip())
            else:
                expanded.append(source)
        sources = sorted({json.dumps(s, sort_keys=True):s for s in expanded}.values(), key=lambda s: (s["kind"], s.get("mediaId", ""), s.get("messageSeq", 0), s.get("page", 0), s.get("start", 0), s.get("startChar", 0)))
        for source in sources:
            kind = source["kind"]
            if kind == "message":
                record = self.db.get("ROOM#" + room, f'MESSAGE#{source["messageSeq"]:020d}')
                if not record or record.value.get("media"):
                    raise HTTPException(404, "MESSAGE_NOT_FOUND")
                item = {"text": record.value.get("bodyCurrent", record.value["bodyOriginal"]), "sentAt": record.value["sentAt"], "source": source,
                        "version":record.version, "sourceLanguage":record.value.get("sourceLanguage", "und")}
            else:
                try:
                    asset = self.media.asset(source["mediaId"])
                except MediaError:
                    raise HTTPException(404, "MEDIA_NOT_FOUND")
                if asset["roomId"] != room:
                    raise HTTPException(404, "MEDIA_NOT_FOUND")
                try:
                    transcript = self.media.transcript(source["mediaId"])
                except MediaError as error:
                    raise HTTPException(error.status, error.code)
                if kind == "page":
                    page = source["page"]
                    if not 1 <= page <= len(transcript["pages"]):
                        raise HTTPException(422, "INVALID_PAGE")
                    text = transcript["pages"][page - 1]
                elif kind == "segment":
                    segments = [s for s in transcript.get("segments", []) if s["end"] > source["start"] and s["start"] < source["end"]]
                    if not segments:
                        raise HTTPException(422, "NO_TRANSCRIPT_SEGMENTS")
                    text = "\n".join(f'[{s["start"]}-{s["end"]}] {s["text"]}' for s in segments)
                elif kind == "transcript":
                    text = " ".join(s["text"].strip() for s in transcript.get("segments", []) if s["text"].strip())
                    if not text:
                        raise HTTPException(422, "NO_TRANSCRIPT_SEGMENTS")
                else:
                    raise HTTPException(422, "INVALID_SOURCE")
                correction = transcript.get("corrections", {}).get(str(source.get("page")), {})
                details = transcript.get("pageDetails", [])
                detail = details[source["page"]-1] if source.get("page") and len(details) >= source["page"] else {}
                item = {"text": text, "source": source, "version": transcript.get("version", 1),
                        "sourceLanguage":correction.get("language") or detail.get("language") or transcript.get("language", "und")}
            start, end = source.get("startChar", 0), source.get("endChar")
            if start < 0 or start > len(item["text"]) or (end is not None and not start < end <= len(item["text"])):
                raise HTTPException(422, "INVALID_SELECTION")
            item["text"] = item["text"][start:end]
            if not item["text"].strip():
                raise HTTPException(422, "EMPTY_SELECTION")
            item["id"] = len(evidence) + 1
            evidence.append(item)
        if not evidence:
            raise HTTPException(422, "EMPTY_SELECTION")
        if sum(len(e["text"]) for e in evidence) > 40000:
            raise HTTPException(422, "SELECTION_TOO_LONG")
        return evidence

    @staticmethod
    def digest(evidence):
        return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def submit(self, body, user):
        body = {**body, "sources": sorted([{**s, "startChar":s.get("startChar", 0)} for s in body["sources"]], key=lambda s:json.dumps(s, sort_keys=True))}
        if not body["consent"]:
            raise HTTPException(422, "CONSENT_REQUIRED")
        if body["target"] not in LANGUAGE_NAMES or body["sourceLanguage"] not in (*LANGUAGE_NAMES, "auto", "und"):
            raise HTTPException(422, "INVALID_LANGUAGE")
        for source in body["sources"]:
            if source["kind"] != "message":
                try:
                    asset = self.media.asset(source["mediaId"])
                except MediaError as error:
                    raise HTTPException(error.status, error.code)
                self.rooms.require_member(body["roomId"], user)
                if asset["roomId"] != body["roomId"]:
                    raise HTTPException(404, "MEDIA_NOT_FOUND")
                if not asset.get("transcriptKey"):
                    self.media.start_job(source["mediaId"])
                    raise HTTPException(409, "EXTRACTION_PENDING")
        try:
            evidence = self.select(body["roomId"], body["sources"], user)
        except MediaError as error:
            raise HTTPException(error.status, error.code)
        if sum(len(split_text(e["text"], 2800)) for e in evidence) > 16:
            raise HTTPException(422, "TOO_MANY_CHUNKS")
        identity = {"roomId":body["roomId"], "kind":body["kind"], "sourceLanguage":body["sourceLanguage"], "target":body["target"],
                    "scope": body["sources"], "evidence":[digest(e) for e in evidence],
                    "modelVersion":MODEL_VERSION, "promptVersion":SUMMARY_CACHE_VERSION}
        job_id = digest(identity)
        result_key = self.result_key(body, job_id)
        saved = self.artifacts.read(identity, result_key)
        previous = self.db.get("AI_JOBS", job_id)
        if previous and saved is not None:
            if previous.value["status"] != "COMPLETED":
                self.db.update("AI_JOBS", job_id, lambda value: {**value, "status":"COMPLETED", "resultKey":result_key, "leaseUntil":0})
            self.index(self.db.get("AI_JOBS", job_id).value)
            result = {**self.get(job_id, user), "cached":True}
            if body["kind"] != "translate":
                result.update({"existing":True, "entryId":job_id})
            return result
        if previous and saved is None and previous.value["status"] != "COMPLETED":
            return self.get(job_id, user)
        created_at = int(time.time() * 1000)
        custom_title = " ".join(str(body.get("title") or "").split())[:60]
        default_title = ("요약 · " if body["kind"] != "translate" else "번역 · ") + evidence[0]["text"][:32]
        job = {**body, "jobId": job_id, "requester": user, "status": "PENDING", "attempts": 0,
               "availableAt": 0, "leaseUntil": 0, "deadLetter": False, "evidence": evidence,
               "sourceHash": self.digest(evidence), "resultKey": result_key, "identity":identity, "resultId":job_id,
               "sourceVersions":[{"source":e["source"], "version":e.get("version", 1)} for e in evidence],
               "cached": saved is not None, "title":custom_title or default_title, "createdAt":created_at, "updatedAt":created_at}
        if saved is None:
            legacy = self.legacy_result(job)
            if legacy is not None:
                saved, _ = self.artifacts.run(identity, result_key, lambda:legacy, lambda:self.rooms.require_member(body["roomId"], user))
                job["cached"] = True
        if saved is None and not self.translation.translator.configured:
            raise HTTPException(503, "TRANSLATION_NOT_CONFIGURED")
        if saved is not None:
            job["status"] = "COMPLETED"
        room = self.rooms.record(body["roomId"])
        member = self.db.get("ROOM#" + body["roomId"], "MEMBER#" + user)
        if not member:
            raise HTTPException(403, "NOT_A_MEMBER")
        try:
            self.db.commit([room.change(room.value, check=True), member.change(member.value, check=True), Change("AI_JOBS", job_id, previous.version if previous else None, job)])
        except Conflict:
            return self.get(job_id, user)
        if saved is not None:
            self.index(job)
            return self.get(job_id, user)
        try:
            self.queue.send(job_id)
        except Exception:
            pass  # durable recovery republishes pending records
        return self.get(job_id, user)

    def get(self, job_id, user):
        record = self.db.get("AI_JOBS", job_id)
        if not record:
            raise HTTPException(404, "JOB_NOT_FOUND")
        job = record.value
        try:
            self.rooms.require_member(job["roomId"], user)
        except HTTPException:
            raise HTTPException(404, "JOB_NOT_FOUND")
        current = self.select(job["roomId"], job["sources"], user)
        if self.digest(current) != job["sourceHash"]:
            raise HTTPException(409, "SOURCE_VERSION_CHANGED")
        result = {k: v for k, v in job.items() if k not in ("evidence", "leaseToken", "consent", "identity")}
        if job.get("kind") != "translate":
            result["entryId"] = job_id
            result["title"] = job.get("title") or self.default_title(job)
            result["updatedAt"] = job.get("updatedAt") or job.get("createdAt")
        if job["status"] == "COMPLETED":
            raw = json.loads(self.media.storage.get(job["resultKey"]))
            result["result"] = raw.get("result", raw)
        return result

    @staticmethod
    def default_title(job):
        evidence = job.get("evidence") or [{}]
        prefix = "요약 · " if job.get("kind") != "translate" else "번역 · "
        return prefix + evidence[0].get("text", "")[:32]

    @staticmethod
    def result_key(body, result_id):
        files = {s.get("mediaId") for s in body["sources"]}
        if len(files) == 1 and None not in files:
            prefix = f'rooms/{body["roomId"]}/files/{next(iter(files))}'
        elif body["kind"] == "translate" and len(body["sources"]) == 1 and body["sources"][0]["kind"] == "message":
            prefix = f'rooms/{body["roomId"]}/messages/{body["sources"][0]["messageSeq"]}'
        else:
            prefix = f'rooms/{body["roomId"]}'
        folder = "translations" if body["kind"] == "translate" else "summaries"
        return f"{prefix}/{folder}/{result_id}/result.json"

    def index(self, job):
        keys = ("jobId", "resultId", "roomId", "kind", "target", "sourceLanguage", "sources", "sourceHash", "sourceVersions", "resultKey", "createdAt", "updatedAt", "title", "requester")
        value = {k:job.get(k) for k in keys}
        value["sourceVersions"] = value["sourceVersions"] or [{"source":e["source"], "version":e.get("version", 1)} for e in job.get("evidence", [])]
        value["title"] = value["title"] or self.default_title(job)
        value["updatedAt"] = value["updatedAt"] or value["createdAt"]
        self.db.update("ROOM_RESULTS#" + job["roomId"], job["jobId"], lambda _: value)
        hidden = self.db.get("AI_RESULT_HIDDEN#" + job["roomId"], job["jobId"])
        if hidden:
            self.db.commit([Change(hidden.pk, hidden.sk, hidden.version, None)])

    def list_results(self, room, user):
        self.rooms.require_member(room, user)
        hidden = {record.sk for record in self.db.query("AI_RESULT_HIDDEN#" + room)}
        with self._migration_lock:
            if room not in self._migrated_rooms:
                # 이전 버전은 완료 결과를 AI_JOBS에만 기록했다. 기존 결과는 방별로 한 번만 새 목록으로 옮긴다.
                indexed = {record.value.get("jobId") for record in self.db.query("ROOM_RESULTS#" + room)}
                old_summaries = {record.value.get("jobId"):record.value for record in self.db.query("ROOM_SUMMARIES#" + room)}
                for record in self.db.query("AI_JOBS"):
                    job = record.value
                    if job.get("roomId") == room and job.get("status") == "COMPLETED" and job.get("resultKey") and job.get("jobId") not in indexed and job.get("jobId") not in hidden:
                        legacy = old_summaries.get(job.get("jobId"))
                        if legacy and legacy.get("title"):
                            job = self.db.update("AI_JOBS", job["jobId"], lambda value: {**value, "title":legacy["title"]})
                        self.index(job)
                self._migrated_rooms.add(room)
        values = []
        for record in self.db.query("ROOM_RESULTS#" + room):
            value = dict(record.value)
            if value.get("jobId") in hidden:
                continue
            try:
                value["stale"] = not self.source_versions_match(room, value, user)
            except HTTPException as error:
                if error.status_code not in (404, 409, 422):
                    raise
                value["stale"] = True
            values.append(value)
        return sorted(values, key=lambda v:v["createdAt"], reverse=True)

    def list_summaries(self, room, user):
        return [({key:value for key, value in item.items() if key != "resultKey"} | {"entryId":item["jobId"]})
                for item in self.list_results(room, user) if item.get("kind") != "translate"]

    @staticmethod
    def _version_signature(versions):
        return sorted((json.dumps(item["source"], sort_keys=True, separators=(",", ":")), item.get("version", 1)) for item in versions)

    def source_versions_match(self, room, result, user):
        saved = result.get("sourceVersions") or []
        if not saved or any(not isinstance(item.get("source"), dict) for item in saved):
            current = self.select(room, result["sources"], user)
            return self.digest(current) == result.get("sourceHash")

        current = []
        for item in saved:
            source = item["source"]
            if source.get("kind") == "message":
                record = self.db.get("ROOM#" + room, f'MESSAGE#{source["messageSeq"]:020d}')
                if not record or record.value.get("media"):
                    raise HTTPException(404, "MESSAGE_NOT_FOUND")
                version = record.version
            else:
                try:
                    asset = self.media.live_asset(source["mediaId"])
                except MediaError as error:
                    raise HTTPException(error.status, error.code)
                if asset["roomId"] != room:
                    raise HTTPException(404, "MEDIA_NOT_FOUND")
                if not asset.get("transcriptKey"):
                    raise HTTPException(409, "SOURCE_VERSION_CHANGED")
                version = asset.get("extractionVersion")
                if version is None:
                    # Legacy assets do not store extractionVersion; load their evidence once for a correct comparison.
                    current = self.select(room, result["sources"], user)
                    current = [{"source":entry["source"], "version":entry.get("version", 1)} for entry in current]
                    return self._version_signature(saved) == self._version_signature(current)
            current.append({"source":source, "version":version})
        return self._version_signature(saved) == self._version_signature(current)

    def read_summary(self, room, entry_id, user):
        self.rooms.require_member(room, user)
        job = self.db.get("AI_JOBS", entry_id)
        if not job or job.value.get("roomId") != room or job.value.get("kind") == "translate":
            raise HTTPException(404, "SUMMARY_NOT_FOUND")
        stale = False
        try:
            result = self.get(entry_id, user).get("result")
        except HTTPException as error:
            if error.status_code not in (404, 409, 422) or job.value.get("status") != "COMPLETED":
                raise
            raw = json.loads(self.media.storage.get(job.value["resultKey"]))
            result, stale = raw.get("result", raw), True
        return {"entryId":entry_id, "title":job.value.get("title") or self.default_title(job.value), "result":result, "stale":stale}

    def rename_summary(self, room, entry_id, title, user):
        self.rooms.require_member(room, user)
        title = " ".join(str(title or "").split())[:60]
        if not title:
            raise HTTPException(422, "TITLE_REQUIRED")
        record = self.db.get("AI_JOBS", entry_id)
        if not record or record.value.get("roomId") != room or record.value.get("kind") == "translate":
            raise HTTPException(404, "SUMMARY_NOT_FOUND")
        if record.value.get("requester") != user:
            raise HTTPException(403, "NOT_SUMMARY_OWNER")
        updated_at = int(time.time() * 1000)
        updated = self.db.update("AI_JOBS", entry_id, lambda value: {**value, "title":title, "updatedAt":updated_at})
        self.index(updated)
        return {"entryId":entry_id, "title":title}

    def delete_summary(self, room, entry_id, user):
        self.rooms.require_member(room, user)
        record = self.db.get("ROOM_RESULTS#" + room, entry_id)
        if not record or record.value.get("kind") == "translate":
            raise HTTPException(404, "SUMMARY_NOT_FOUND")
        if record.value.get("requester") != user:
            raise HTTPException(403, "NOT_SUMMARY_OWNER")
        self.db.update("AI_RESULT_HIDDEN#" + room, entry_id, lambda _: {"entryId":entry_id, "deletedAt":int(time.time() * 1000)})
        self.db.commit([Change(record.pk, record.sk, record.version, None)])
        return {"entryId":entry_id, "deleted":True}

    def sync_translation(self, source, room, target, user):
        # Legacy endpoints and asynchronous buttons share the exact result identity.
        self.rooms.require_member(room, user)
        if source["kind"] == "message":
            record = self.db.get("ROOM#" + room, f'MESSAGE#{source["messageSeq"]:020d}')
            if record and record.value.get("media"):
                raise HTTPException(422, "NOT_TEXT_MESSAGE")
        try:
            evidence = self.select(room, [source], user)
        except HTTPException as error:
            if source["kind"] == "page" and error.detail == "EMPTY_SELECTION":
                raise HTTPException(422, "EMPTY_PAGE")
            raise
        from app.ai.service import MAX_CHARS, MAX_PAGE_CHARS
        if len(evidence[0]["text"]) > (MAX_CHARS if source["kind"] == "message" else MAX_PAGE_CHARS):
            raise HTTPException(422, "TEXT_TOO_LONG" if source["kind"] == "message" else "PAGE_TOO_LONG")
        job = self.submit({"roomId":room, "kind":"translate", "sourceLanguage":"auto", "target":target, "sources":[source], "consent":True}, user)
        cached = bool(job.get("cached"))
        if not cached and not self.translation.translator.configured:
            raise HTTPException(503, "TRANSLATION_NOT_CONFIGURED")
        if job.get("errorCode") and job["errorCode"] != "AI_CALL_UNCERTAIN":
            self.db.update("AI_JOBS", job["jobId"], lambda value: {**value, "requester":user, "status":"PENDING", "attempts":0, "errorCode":None, "availableAt":0, "leaseUntil":0})
            job = self.get(job["jobId"], user)
        deadline = time.monotonic() + 30
        while job["status"] != "COMPLETED":
            if job.get("errorCode"):
                raise HTTPException(job.get("errorStatus", 502), "TRANSLATION_UNAVAILABLE" if job.get("errorStatus",502) == 502 else job["errorCode"])
            self.work(job["jobId"])
            job = self.get(job["jobId"], user)
            if job["status"] == "COMPLETED":
                break
            if time.monotonic() >= deadline:
                raise HTTPException(503, "RESULT_IN_PROGRESS")
            time.sleep(.05)
        flags = job["result"].get("flags", {})
        result = {"translated":job["result"]["text"], "target":target, "cached":cached or bool(job.get("cached")) or flags.get("cached", False)}
        if source["kind"] == "message":
            result["sameLanguage"] = flags.get("sameLanguage", False)
        else:
            result["page"] = source["page"]
            result["sameLanguage"] = flags.get("sameLanguage", False)
        if flags.get("languageMismatch"):
            result["languageMismatch"] = True
        return result

    def summary_cache_key(self, job):
        """같은 방·종류·대상 언어·선택 자료(sourceHash)이면 같은 키. 선택한 자료가 바뀌면 키가 달라진다."""
        raw = json.dumps([SUMMARY_CACHE_VERSION, MODEL_VERSION, job["roomId"], job["kind"], job["target"], job["sourceHash"]])
        key = hashlib.sha256(raw.encode()).hexdigest()
        return "AI_SUMMARY#" + key[:2], key

    def cached_summary(self, job):
        pk, key = self.summary_cache_key(job)
        record = self.db.get(pk, key)
        text = record.value.get("text") if record else None
        return text if isinstance(text, str) and text.strip() else None

    def remember_summary(self, job, text):
        pk, key = self.summary_cache_key(job)
        self.db.update(pk, key, lambda value: value if value and value.get("text") else {"text": text, "createdAt": int(time.time() * 1000)})

    def budget(self, job, text):
        now = time.time()
        # UTF-8 byte length is a conservative input token bound; reserve the full output limit.
        tokens = len(text.encode()) + 4000
        limits = json.loads(os.getenv("AI_LANGUAGE_LIMITS", "{}"))
        profile = limits.get(job["target"], {})
        def reserve(value):
            calls = [c for c in (value or {}).get("calls", []) if c["at"] > now - 3600]
            if len(calls) >= profile.get("calls", 60) or sum(c["tokens"] for c in calls) + tokens > profile.get("tokens", 120000):
                raise HTTPException(429, "AI_LANGUAGE_LIMIT")
            return {"calls": calls + [{"at": now, "tokens": tokens}]}
        self.db.update("AI_QUOTA", job["requester"] + "#" + job["target"], reserve)

    def produce(self, job):
        if job["kind"] == "translate" and job["sourceLanguage"] == "auto" and len(job["sources"]) == 1:
            source = job["sources"][0]
            if not source.get("startChar") and source.get("endChar") is None:
                flags = None
                if source["kind"] == "message":
                    flags = self.translation._translate_message(job["roomId"], source["messageSeq"], job["target"], job["requester"])
                elif source["kind"] == "page":
                    flags = self.translation._translate_page(source["mediaId"], source["page"], job["target"], job["requester"])
                if flags is not None:
                    return {"text":flags["translated"], "flags":flags, "evidence":job["evidence"], "sourceHash":job["sourceHash"]}
        allowed = {e["id"] for e in job["evidence"]}
        def check(text):
            citations = {int(n) for n in re.findall(r"\[(\d+)\]", text)}
            if not in_target_language(text, job["target"]) or not citations or not citations.issubset(allowed):
                raise ValueError("SUMMARY_LANGUAGE_OR_EVIDENCE_INVALID")
            return text
        legacy = self.legacy_result(job)
        if legacy is not None:
            return legacy
        pieces = []
        translation_contexts = []
        for evidence in job["evidence"]:
            translation_context = self.translation_context(job, evidence) if job["kind"] == "translate" else None
            for chunk in split_text(evidence["text"], 2800):
                pieces.append(f'[{evidence["id"]}] {chunk}' if job["kind"] != "translate" else chunk)
                if translation_context is not None:
                    translation_contexts.append(translation_context)
        if len(pieces) > 16:
            raise HTTPException(422, "TOO_MANY_CHUNKS")
        return self.generate_pieces(job, pieces, check, translation_contexts)

    def translation_context(self, job, evidence):
        source = evidence["source"]
        version = evidence.get("version", 1)
        if source["kind"] == "message":
            scope = {"room":job["roomId"], "message":source["messageSeq"], "version":version}
        elif source["kind"] == "page":
            scope = {"room":job["roomId"], "media":source["mediaId"], "page":source["page"], "version":version}
        else:
            scope = {"room":job["roomId"], "media":source.get("mediaId"), "kind":source["kind"], "source":source, "version":version}
        source_language = self.translation.source_language(evidence.get("sourceLanguage"))
        if not source_language:
            source_language = self.translation.source_language(job.get("sourceLanguage"))
        return {"scope":scope, "sourceLanguage":source_language}

    def legacy_result(self, job):
        legacy_evidence = [{k:v for k,v in e.items() if k != "sourceLanguage" and not (k == "version" and e["source"]["kind"] == "message")} for e in job["evidence"]]
        legacy_hash = self.digest(legacy_evidence)
        legacy_allowed = all(e["source"]["kind"] != "message" or e.get("version", 1) == 1 for e in job["evidence"])
        legacy = self.cached_summary(job) if job["kind"] != "translate" and job["sourceLanguage"] == "auto" else None
        if not legacy and legacy_allowed and job["kind"] != "translate" and job["sourceLanguage"] == "auto":
            legacy = self.cached_summary({**job, "sourceHash":legacy_hash})
        if legacy:
            citations = {int(n) for n in re.findall(r"\[(\d+)\]", legacy)}
            if in_target_language(legacy, job["target"]) and citations and citations.issubset({e["id"] for e in job["evidence"]}):
                return {"text":legacy, "evidence":job["evidence"], "sourceHash":job["sourceHash"]}
        for record in self.db.query("AI_JOBS"):
            old = record.value
            if old.get("identity") or old.get("status") != "COMPLETED":
                continue
            if legacy_allowed and all(old.get(k) == job.get(k) for k in ("roomId", "kind", "target", "sourceLanguage")) and old.get("sourceHash") in (job["sourceHash"], legacy_hash):
                saved = json.loads(self.media.storage.get(old["resultKey"]))
                return {**saved, "evidence":job["evidence"], "sourceHash":job["sourceHash"]}
        return None

    def generate_pieces(self, job, pieces, check, translation_contexts=None):
        translation_flags = []

        def generate(text, label, translation_context=None):
            self.get(job["jobId"], job["requester"])
            identity = {"parent":job["identity"], "stage":label, "textHash":digest(text)}
            key = job["resultKey"].rsplit("/", 1)[0] + "/chunks/" + digest(identity) + ".json"
            def call():
                if job["kind"] == "translate":
                    source_language = (translation_context or {}).get("sourceLanguage")
                    scope = (translation_context or {}).get("scope", identity)
                    if source_language and not text_matches_language(text, source_language):
                        return {"text":text, "flags":{"cached":False, "sameLanguage":True, "languageMismatch":True}}
                    if source_language == job["target"]:
                        return {"text":text, "flags":{"cached":False, "sameLanguage":True}}
                def count(value):
                    if value.get("callsUsed", 0) >= 32:
                        raise HTTPException(422, "AI_TOTAL_CALL_LIMIT")
                    return {**value, "callsUsed":value.get("callsUsed", 0)+1}
                self.db.update("AI_JOBS", job["jobId"], count)
                self.budget(job, text)
                if job["kind"] == "translate":
                    output, cached = self.translation.translate_text(text, job["target"], job["requester"], source=source_language, context=scope)
                    return {"text":output, "flags":{"cached":cached}}
                return {"text":check(self.translation.translator.summarize(text, LANGUAGE_NAMES[job["target"]]))}
            result, cached_artifact = self.artifacts.run(identity, key, call, lambda:self.get(job["jobId"], job["requester"]))
            if job["kind"] == "translate":
                flags = result.get("flags", {})
                translation_flags.append({**flags, "cached":bool(flags.get("cached") or cached_artifact)})
            return result["text"]
        outputs = [generate(text, f"map-{i}", (translation_contexts or [])[i] if i < len(translation_contexts or []) else None)
                   for i, text in enumerate(pieces)]
        output = "\n\n".join(outputs)
        if job["kind"] != "translate" and len(pieces) > 1:
            for level in range(4):
                batches = split_text(output, 2800)
                output = "\n\n".join(generate(text, f"reduce-{level}-{i}") for i, text in enumerate(batches))
                if len(batches) == 1:
                    break
            else:
                raise HTTPException(422, "SUMMARY_REDUCTION_LIMIT")
        result = {"text":output, "evidence":job["evidence"], "sourceHash":job["sourceHash"]}
        if job["kind"] == "translate" and translation_flags:
            result["flags"] = {
                "cached":all(flags.get("cached", False) for flags in translation_flags),
                "sameLanguage":all(flags.get("sameLanguage", False) for flags in translation_flags),
                "languageMismatch":any(flags.get("languageMismatch", False) for flags in translation_flags),
            }
        return result

    def work(self, job_id):
        record = self.db.get("AI_JOBS", job_id)
        if not record:
            return True
        job = record.value
        if job["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return not job.get("deadLetter")
        if max(job.get("leaseUntil", 0), job.get("availableAt", 0)) > time.time():
            return False
        if job["attempts"] >= 3:
            try:
                self.db.commit([record.change({**job, "status": "FAILED", "deadLetter": True, "errorCode": "WORKER_INTERRUPTED"})])
            except Conflict:
                pass
            return False
        lease = uuid.uuid4().hex
        job = {**job, "status": "PROCESSING", "leaseToken": lease, "leaseUntil": time.time() + 180, "attempts": job["attempts"] + 1}
        try:
            self.db.commit([record.change(job)])
        except Conflict:
            return False
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(30):
                try:
                    self.finish(job_id, lease, {"leaseUntil":time.time()+180})
                except Exception:
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            self.get(job_id, job["requester"])
            if "identity" not in job:  # recover a durable pre-migration pending job
                job["identity"] = {"legacyJob":job_id, "sourceHash":job["sourceHash"], "target":job["target"]}
                job["resultKey"] = self.result_key(job, job_id)
            result, cached = self.artifacts.run(job["identity"], job["resultKey"], lambda:self.produce(job), lambda:self.get(job_id, job["requester"]))
            self.finish(job_id, lease, {"status": "COMPLETED", "resultKey": job["resultKey"], "leaseUntil": 0, "cached":cached})
            self.index(job)
            self.media.on_event(job["roomId"], {"type":"ai:updated", "jobId":job_id})
            return True
        except Exception as error:
            terminal = isinstance(error, HTTPException) and error.status_code in (403, 404, 409, 422)
            failed = terminal or job["attempts"] >= 3
            self.finish(job_id, lease, {"status": "FAILED" if failed else "PENDING", "leaseUntil": 0,
                        "availableAt": time.time() + min(60, 2 ** job["attempts"]), "deadLetter": failed and not terminal,
                        "errorStatus": error.status_code if isinstance(error, HTTPException) else 502,
                        "errorCode": error.detail if isinstance(error, HTTPException) else "AI_UNAVAILABLE"})
            return terminal
        finally:
            stop.set()
            thread.join(timeout=1)

    def finish(self, job_id, lease, changes):
        def update(value):
            if value.get("leaseToken") != lease or value["status"] != "PROCESSING":
                raise HTTPException(409, "LEASE_LOST")
            return {**value, **changes}
        self.db.update("AI_JOBS", job_id, update)

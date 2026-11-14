"""Durable AI selections, leased execution, retry and evidence results."""
import hashlib
import json
import os
import re
import time
import uuid

from fastapi import HTTPException
from app.core.database import Change, Conflict
from app.media.queue import MediaWorker
from app.media.service import MediaError
from app.ai.contract import MODEL_VERSION
from app.ai.service import LANGUAGE_NAMES

SUMMARY_KINDS = ("summarize", "meeting-summary")
MAX_TITLE = 60
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

    def select(self, room, sources, user):
        self.rooms.require_member(room, user)
        evidence = []
        for source in sources:
            kind = source["kind"]
            if kind == "message":
                record = self.db.get("ROOM#" + room, f'MESSAGE#{source["messageSeq"]:020d}')
                if not record or record.value.get("media"):
                    raise HTTPException(404, "MESSAGE_NOT_FOUND")
                item = {"text": record.value["bodyOriginal"], "sentAt": record.value["sentAt"], "source": source}
            else:
                try:
                    asset = self.media.live_asset(source["mediaId"])
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
                elif kind == "transcript":  # 음성 전체. 구간 시각 없이 이어 붙여 하나의 근거[1]로 요약한다
                    text = " ".join(s["text"].strip() for s in transcript.get("segments", []) if s["text"].strip())
                    if not text:
                        raise HTTPException(422, "NO_TRANSCRIPT_SEGMENTS")
                else:
                    raise HTTPException(422, "INVALID_SOURCE")
                item = {"text": text, "source": source, "version": transcript.get("version", 1)}
            start, end = source.get("startChar", 0), source.get("endChar")
            if start < 0 or start > len(item["text"]) or (end is not None and not start < end <= len(item["text"])):
                raise HTTPException(422, "INVALID_SELECTION")
            item["text"] = item["text"][start:end]
            if not item["text"].strip():
                raise HTTPException(422, "EMPTY_SELECTION")
            item["id"] = len(evidence) + 1
            evidence.append(item)
        if sum(len(e["text"]) for e in evidence) > 3500:
            raise HTTPException(422, "SELECTION_TOO_LONG")
        return evidence

    @staticmethod
    def digest(evidence):
        return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def submit(self, body, user):
        if not body["consent"]:
            raise HTTPException(422, "CONSENT_REQUIRED")
        evidence = self.select(body["roomId"], body["sources"], user)
        if body["target"] not in LANGUAGE_NAMES or body["sourceLanguage"] not in (*LANGUAGE_NAMES, "auto", "und"):
            raise HTTPException(422, "INVALID_LANGUAGE")
        existing = self.existing_summary(body, evidence)
        if existing:  # 같은 선택·종류·대상 언어의 요약이 이미 있으면 새 작업도 새 목록 항목도 만들지 않는다
            return existing
        if not self.translation.translator.configured:
            raise HTTPException(503, "TRANSLATION_NOT_CONFIGURED")
        job_id = uuid.uuid4().hex
        job = {**body, "jobId": job_id, "requester": user, "status": "PENDING", "attempts": 0,
               "availableAt": 0, "leaseUntil": 0, "deadLetter": False, "evidence": evidence,
               "sourceHash": self.digest(evidence), "resultKey": None, "createdAt": int(time.time() * 1000)}
        room = self.rooms.record(body["roomId"])
        member = self.db.get("ROOM#" + body["roomId"], "MEMBER#" + user)
        if not member:
            raise HTTPException(403, "NOT_A_MEMBER")
        self.db.commit([room.change(room.value, check=True), member.change(member.value, check=True), Change("AI_JOBS", job_id, None, job)])
        try:
            self.queue.send(job_id)
        except Exception:
            pass  # durable recovery republishes pending records
        return self.get(job_id, user)

    def get(self, job_id, user):
        record = self.db.get("AI_JOBS", job_id)
        if not record or record.value["requester"] != user:
            raise HTTPException(404, "JOB_NOT_FOUND")
        job = record.value
        current = self.select(job["roomId"], job["sources"], user)
        if self.digest(current) != job["sourceHash"]:
            raise HTTPException(409, "SOURCE_VERSION_CHANGED")
        result = {k: v for k, v in job.items() if k not in ("evidence", "leaseToken", "consent")}
        if job["status"] == "COMPLETED":
            result["result"] = json.loads(self.media.storage.get(job["resultKey"]))
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

    @staticmethod
    def clean_title(title):
        """사용자가 정한 요약 이름. 공백을 정리하고 길이를 제한한다. 비면 None."""
        title = " ".join(str(title or "").split())[:MAX_TITLE]
        return title or None

    @staticmethod
    def index_partition(room_id):
        return "ROOM_SUMMARIES#" + room_id

    def record_summary(self, job, result_key):
        """방별 요약 목록에 항목을 올린다. 같은 선택(같은 키)의 요약은 한 항목으로 합치고, 이름은 새로 정했을 때만 바꾼다."""
        _, entry_id = self.summary_cache_key(job)
        now = int(time.time() * 1000)
        first = " ".join(job["evidence"][0]["text"].split())
        default_title = first[:30] + ("…" if len(first) > 30 else "")
        def upsert(value):
            value = value or {}
            return {"entryId": entry_id, "roomId": job["roomId"], "kind": job["kind"], "target": job["target"],
                    "requester": value.get("requester", job["requester"]), "createdAt": value.get("createdAt", now), "updatedAt": now,
                    "title": self.clean_title(job.get("title")) or value.get("title") or default_title,
                    "count": len(job["evidence"]), "resultKey": result_key, "jobId": job["jobId"]}
        self.db.update(self.index_partition(job["roomId"]), entry_id, upsert)
        return entry_id

    def existing_summary(self, body, evidence):
        if body["kind"] not in SUMMARY_KINDS:
            return None
        probe = {"roomId": body["roomId"], "kind": body["kind"], "target": body["target"], "sourceHash": self.digest(evidence)}
        _, entry_id = self.summary_cache_key(probe)
        record = self.db.get(self.index_partition(body["roomId"]), entry_id)
        if not record:
            return None
        return {"existing": True, "entryId": entry_id, "roomId": body["roomId"], "title": record.value["title"]}

    def list_summaries(self, room_id, user):
        self.rooms.require_member(room_id, user)
        entries = [{k: v for k, v in record.value.items() if k != "resultKey"} for record in self.db.query(self.index_partition(room_id))]
        return sorted(entries, key=lambda entry: entry["updatedAt"], reverse=True)

    def read_summary(self, room_id, entry_id, user):
        self.rooms.require_member(room_id, user)
        record = self.db.get(self.index_partition(room_id), entry_id)
        if not record:
            raise HTTPException(404, "SUMMARY_NOT_FOUND")
        entry = {k: v for k, v in record.value.items() if k != "resultKey"}
        entry["result"] = json.loads(self.media.storage.get(record.value["resultKey"]))
        return entry

    def rename_summary(self, room_id, entry_id, title, user):
        self.rooms.require_member(room_id, user)
        title = self.clean_title(title)
        if not title:
            raise HTTPException(422, "TITLE_REQUIRED")
        def rename(value):
            if not value:
                raise HTTPException(404, "SUMMARY_NOT_FOUND")
            if value["requester"] != user:
                raise HTTPException(403, "NOT_SUMMARY_OWNER")
            return {**value, "title": title}
        self.db.update(self.index_partition(room_id), entry_id, rename)
        return {"entryId": entry_id, "title": title}

    def delete_summary(self, room_id, entry_id, user):
        """목록에서 요약을 지운다. 만든 사람만 지울 수 있고, 같은 선택을 다시 요약하면 새로 만들어진다(캐시도 함께 지움)."""
        self.rooms.require_member(room_id, user)
        record = self.db.get(self.index_partition(room_id), entry_id)
        if not record:
            raise HTTPException(404, "SUMMARY_NOT_FOUND")
        if record.value["requester"] != user:
            raise HTTPException(403, "NOT_SUMMARY_OWNER")
        changes = [Change(record.pk, record.sk, record.version, None)]
        cached = self.db.get("AI_SUMMARY#" + entry_id[:2], entry_id)
        if cached:
            changes.append(Change(cached.pk, cached.sk, cached.version, None))
        try:
            self.db.commit(changes)
        except Conflict:
            raise HTTPException(409, "SUMMARY_CHANGED")
        return {"entryId": entry_id, "deleted": True}

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
        try:
            self.get(job_id, job["requester"])
            text = "\n\n".join(f'[{e["id"]}] {e["text"]}' for e in job["evidence"])
            output = self.cached_summary(job) if job["kind"] != "translate" else None
            if output is None:  # 캐시가 있으면 한도 차감과 Lambda 호출을 모두 건너뛴다
                self.budget(job, text)
                if job["kind"] == "translate":
                    output, _ = self.translation.translate_text(text, job["target"], job["requester"])
                else:
                    output = self.translation.translator.summarize(text, LANGUAGE_NAMES[job["target"]])
                    if not in_target_language(output, job["target"]):
                        raise ValueError("summary is not in the target language")
                    citations = set(int(number) for number in re.findall(r"\[(\d+)\]", output))
                    if not citations or not citations.issubset({e["id"] for e in job["evidence"]}):
                        raise ValueError("summary has invalid evidence references")
                    self.remember_summary(job, output)  # 근거 번호 검사를 통과한 요약만 저장한다
            self.get(job_id, job["requester"])
            key = f"ai/results/{job_id}/{lease}.json"
            self.media.storage.put(key, json.dumps({"text": output, "evidence": job["evidence"], "sourceHash": job["sourceHash"]}, ensure_ascii=False).encode())
            changes = {"status": "COMPLETED", "resultKey": key, "leaseUntil": 0}
            if job["kind"] in SUMMARY_KINDS:
                changes["entryId"] = self.record_summary(job, key)
            self.finish(job_id, lease, changes)
            return True
        except Exception as error:
            terminal = isinstance(error, HTTPException) and error.status_code in (403, 404, 409, 422)
            failed = terminal or job["attempts"] >= 3
            self.finish(job_id, lease, {"status": "FAILED" if failed else "PENDING", "leaseUntil": 0,
                        "availableAt": time.time() + min(60, 2 ** job["attempts"]), "deadLetter": failed and not terminal,
                        "errorCode": error.detail if isinstance(error, HTTPException) else "AI_UNAVAILABLE"})
            return terminal

    def finish(self, job_id, lease, changes):
        def update(value):
            if value.get("leaseToken") != lease or value["status"] != "PROCESSING":
                raise HTTPException(409, "LEASE_LOST")
            return {**value, **changes}
        self.db.update("AI_JOBS", job_id, update)

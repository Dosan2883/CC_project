"""영속 미디어 작업, 멱등 업로드 완료, 임대 기반 워커 복구."""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid

from fastapi import HTTPException
from app.core.database import Change, Conflict
from app.media.extractors import ExtractError, extract
from app.media.queue import MediaWorker
from app.rooms.repository import retry_conflicts

log = logging.getLogger(__name__)
ALLOWED_EXT = {"pdf": "document", "docx": "document", "txt": "document", "jpg": "image", "jpeg": "image", "png": "image",
               "mp3": "audio", "m4a": "audio", "wav": "audio", "ogg": "audio", "flac": "audio", "mp4": "video", "webm": "video"}
MAX_BYTES = 20 * 1024 * 1024
# 음성·영상은 문서보다 커서 종류별로 따로 제한한다. 서버 메모리가 작아 한 번에 메모리에 올리는 크기를 넘기지 않는다.
MAX_BYTES_BY_TYPE = {"audio": int(os.getenv("MAX_AUDIO_MB", "30")) * 1024 * 1024, "video": int(os.getenv("MAX_VIDEO_MB", "60")) * 1024 * 1024}


def max_bytes_for(ext):
    return MAX_BYTES_BY_TYPE.get(ALLOWED_EXT.get(ext), MAX_BYTES)
UPLOAD_TTL = 300
MAX_RETRIES = 3

class MediaError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def safe_filename(name: str) -> str:
    name = re.split(r"[\\/]", name)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:100]


def ext_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def looks_valid(ext: str, data: bytes) -> bool:
    """확장자만 믿지 않고 파일 앞부분(매직 바이트)도 확인한다."""
    if ext == "pdf":
        return data[:5] == b"%PDF-"
    if ext == "docx":
        return data[:4] == b"PK\x03\x04"
    if ext in ("jpg", "jpeg", "png"):
        try:
            from PIL import Image
            import io
            with Image.open(io.BytesIO(data)) as source:
                if source.width * source.height > 25_000_000 or source.format != ("PNG" if ext == "png" else "JPEG"):
                    return False
                source.verify()
            return True
        except Exception:
            return False
    if ext == "mp3":  # 태그(ID3) 또는 프레임 동기 신호
        return data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    if ext == "wav":
        return data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if ext in ("m4a", "mp4"):
        return data[4:8] == b"ftyp"
    if ext == "ogg":
        return data[:4] == b"OggS"
    if ext == "flac":
        return data[:4] == b"fLaC"
    if ext == "webm":
        return data[:4] == b"\x1a\x45\xdf\xa3"
    return True



class LeaseLost(Exception):
    pass


class MediaService:
    def __init__(self, storage, on_event, db, rooms, sqs=None, queue_url=""):
        self.storage, self.on_event, self.db, self.rooms = storage, on_event, db, rooms
        self.queue = MediaWorker(self, sqs, queue_url)

    def asset(self, media_id):
        record = self.db.get("MEDIA", "ASSET#" + media_id)
        if not record:
            raise MediaError(404, "MEDIA_NOT_FOUND")
        return record.value

    def is_deleted(self, asset):
        # 삭제 표시는 자산 기록과 따로 둔다. 처리 워커가 자산을 다시 저장해도 지워지지 않는다.
        return bool(self.db.get("ROOM#" + asset["roomId"], "MEDIA_DELETED#" + asset["mediaId"]))

    def live_asset(self, media_id):
        """삭제되지 않은 파일만 돌려준다. 삭제된 파일은 없는 파일처럼 다룬다."""
        asset = self.asset(media_id)
        if self.is_deleted(asset):
            raise MediaError(404, "MEDIA_NOT_FOUND")
        return asset

    def delete_file(self, media_id, user):
        """올린 사람만 지울 수 있다. 논리 삭제라서 원본과 추출 결과 파일은 저장소에 보관한다."""
        asset = self.asset(media_id)
        self.rooms.require_member(asset["roomId"], user)
        if asset.get("uploader") != user:
            raise MediaError(403, "NOT_FILE_OWNER")
        self.db.update("ROOM#" + asset["roomId"], "MEDIA_DELETED#" + media_id,
                       lambda value: value or {"mediaId": media_id, "deletedBy": user, "deletedAt": int(time.time() * 1000)})
        return {"mediaId": media_id, "status": "DELETED"}

    @retry_conflicts
    def request_upload(self, room_id, user, filename, size):
        room = self.rooms.record(room_id)
        member = self.db.get("ROOM#" + room_id, "MEMBER#" + user)
        if not member:
            raise MediaError(403, "NOT_A_MEMBER")
        filename = safe_filename(filename)
        ext = ext_of(filename)
        if ext not in ALLOWED_EXT:
            raise MediaError(400, "UNSUPPORTED_TYPE")
        if not 0 < size <= max_bytes_for(ext):
            raise MediaError(413, "FILE_TOO_LARGE")
        media_id, token = uuid.uuid4().hex, secrets.token_urlsafe(24)
        asset = {"mediaId": media_id, "roomId": room_id, "uploader": user, "filename": filename,
                 "ext": ext, "type": ALLOWED_EXT[ext], "size": size, "status": "UPLOADING", "s3Key": "original/" + media_id,
                 "uploadTokenHash": hashlib.sha256(token.encode()).hexdigest(), "uploadExpiresAt": time.time() + UPLOAD_TTL,
                 "transcriptKey": None, "detectedLanguages": [], "recognitionModel": None, "createdAt": int(time.time() * 1000)}
        self.db.commit([room.change(room.value, check=True), member.change(member.value, check=True), Change("MEDIA", "ASSET#" + media_id, None, asset)])
        if self.storage.direct_upload:
            upload = self.storage.upload_url(asset["s3Key"], size, UPLOAD_TTL)
        else:
            upload = {"uploadUrl": f"/api/files/upload/{media_id}?token={token}", "method": "PUT", "headers": {}}
        return {"mediaId": media_id, **upload, "expiresIn": UPLOAD_TTL, "completeUrl": f"/api/files/{media_id}/complete"}

    def check_token(self, media_id, token):
        asset = self.asset(media_id)
        if self.storage.direct_upload:
            raise MediaError(409, "USE_DIRECT_UPLOAD")
        if asset["status"] != "UPLOADING":
            raise MediaError(409, "ALREADY_UPLOADED")
        if time.time() > asset["uploadExpiresAt"] or not hmac.compare_digest(asset["uploadTokenHash"], hashlib.sha256(token.encode()).hexdigest()):
            raise MediaError(403, "INVALID_OR_EXPIRED_TOKEN")
        self.rooms.require_member(asset["roomId"], asset["uploader"])
        return asset

    def validate_data(self, asset, data):
        if not data or len(data) > max_bytes_for(asset["ext"]):
            raise MediaError(413, "FILE_TOO_LARGE")
        if len(data) != asset["size"]:
            raise MediaError(400, "FILE_SIZE_MISMATCH")
        if not looks_valid(asset["ext"], data):
            raise MediaError(400, "FILE_CONTENT_MISMATCH")

    def receive_upload(self, media_id, token, data):
        asset = self.check_token(media_id, token)
        self.validate_data(asset, data)
        try:
            self.storage.put_original(asset["s3Key"], data)
        except FileExistsError:
            if self.storage.get_limited(asset["s3Key"], max_bytes_for(asset["ext"])) != data:
                raise MediaError(409, "ALREADY_UPLOADED")
        return self.complete_upload(media_id, asset["uploader"])

    @retry_conflicts
    def complete_upload(self, media_id, user):
        record = self.db.get("MEDIA", "ASSET#" + media_id)
        if not record:
            raise MediaError(404, "MEDIA_NOT_FOUND")
        asset = record.value
        room = self.rooms.record(asset["roomId"])
        member = self.db.get("ROOM#" + asset["roomId"], "MEMBER#" + user)
        if not member or asset["uploader"] != user:
            raise MediaError(403, "NOT_THE_UPLOADER")
        if asset["status"] != "UPLOADING":
            return asset
        if time.time() > asset["uploadExpiresAt"]:
            raise MediaError(403, "INVALID_OR_EXPIRED_TOKEN")
        try:
            if self.storage.size(asset["s3Key"]) != asset["size"]:
                raise MediaError(400, "FILE_SIZE_MISMATCH")
            data = self.storage.get_limited(asset["s3Key"], max_bytes_for(asset["ext"]))
        except FileNotFoundError:
            raise MediaError(409, "UPLOAD_NOT_FOUND")
        self.validate_data(asset, data)
        asset.update(status="UPLOADED", contentHash=hashlib.sha256(data).hexdigest(), jobId=media_id)
        asset.pop("uploadTokenHash", None)
        seq = room.value["lastMessageSeq"] + 1
        card = {"mediaId": media_id, "filename": asset["filename"], "kind": asset["type"], "ext": asset["ext"], "size": asset["size"]}
        message = {"type": "message:new", "roomId": asset["roomId"], "messageSeq": seq, "sender": user,
                   "bodyOriginal": asset["filename"], "sourceLanguage": "und", "sentAt": int(time.time() * 1000), "media": card}
        self.db.commit([
            room.change({**room.value, "lastMessageSeq": seq}), member.change(member.value, check=True), record.change(asset),
            Change("ROOM#" + asset["roomId"], f"MESSAGE#{seq:020d}", None, message),
            Change("ROOM#" + asset["roomId"], "MEDIA_MESSAGE#" + media_id, None, {"messageSeq": seq}),
            Change("MEDIA_JOBS", media_id, None, self.new_job(asset)),
        ])
        self.emit(asset)
        self.enqueue(media_id)
        return asset

    @staticmethod
    def new_job(asset):
        return {"jobId": asset["mediaId"], "mediaId": asset["mediaId"], "roomId": asset["roomId"], "status": "PENDING", "progress": 0,
                "retryCount": 0, "attempts": 0, "errorCode": None, "leaseUntil": 0, "availableAt": 0, "deadLetter": False}

    def enqueue(self, media_id):
        try:
            self.queue.send(media_id)
        except Exception:
            log.exception("SQS send failed; durable pending job will be recovered")

    @retry_conflicts
    def start_job(self, media_id, language=None):
        record = self.db.get("MEDIA", "ASSET#" + media_id)
        if not record:
            raise MediaError(404, "MEDIA_NOT_FOUND")
        asset = record.value
        room = self.rooms.record(asset["roomId"])
        if asset["status"] != "UPLOADED":
            raise MediaError(409, "NOT_UPLOADED")
        previous = self.db.get("MEDIA_JOBS", media_id)
        if previous and (previous.value["status"] not in ("FAILED", "CANCELLED", "COMPLETED") or (previous.value["status"] == "COMPLETED" and language is None)):
            return previous.value
        job = self.new_job(asset)
        if language is not None:
            from app.media.ocr import LANGUAGES
            if language != "auto" and language not in LANGUAGES:
                raise MediaError(422, "INVALID_LANGUAGE")
            job["ocrLanguage"] = language
        self.db.commit([room.change(room.value, check=True), record.change({**asset, "jobId": media_id}),
                        Change("MEDIA_JOBS", media_id, previous.version if previous else None, job)])
        self.emit(asset)
        self.enqueue(media_id)
        return job

    def status(self, media_id):
        asset = self.asset(media_id)
        record = self.db.get("MEDIA_JOBS", asset.get("jobId", ""))
        job = record.value if record else {}
        return {"mediaId": media_id, "filename": asset["filename"], "kind": asset["type"], "size": asset["size"],
                "status": job.get("status", asset["status"]), "progress": job.get("progress", 0), "errorCode": job.get("errorCode"),
                "needsOcr": asset.get("needsOcr", False), "pageCount": asset.get("pageCount", 0), "retryCount": job.get("retryCount", 0)}

    def list_room_files(self, room_id):
        """방에 올라온 파일 목록(최신순). 파일 카드가 들어 있는 메시지와 처리 상태를 합친다."""
        files = []
        deleted = {record.sk.split("#", 1)[1] for record in self.db.query("ROOM#" + room_id, "MEDIA_DELETED#")}
        for record in self.db.query("ROOM#" + room_id, "MEDIA_MESSAGE#"):
            media_id = record.sk.split("#", 1)[1]
            if media_id in deleted:
                continue
            message = self.db.get("ROOM#" + room_id, f"MESSAGE#{record.value['messageSeq']:020d}")
            if not message:
                continue
            try:
                state = self.status(media_id)
            except MediaError:
                continue
            card = message.value.get("media") or {}
            files.append({"mediaId": media_id, "filename": state["filename"], "ext": card.get("ext", ""), "kind": state["kind"],
                          "size": state["size"], "status": state["status"], "progress": state["progress"], "pageCount": state["pageCount"],
                          "sender": message.value.get("sender", ""), "sentAt": message.value.get("sentAt", 0), "messageSeq": record.value["messageSeq"]})
        return sorted(files, key=lambda item: item["sentAt"], reverse=True)

    def transcript(self, media_id):
        asset = self.asset(media_id)
        if not asset.get("transcriptKey"):
            raise MediaError(409, "NOT_READY")
        return json.loads(self.storage.get(asset["transcriptKey"]))

    def emit(self, asset):
        try:
            status = self.status(asset["mediaId"])
            self.on_event(asset["roomId"], {"type": "media:ready" if status["status"] == "COMPLETED" else "media:update", **status})
        except Exception:
            log.exception("Media event delivery failed; status API remains authoritative")

    def update_leased(self, job_id, lease, changes):
        def transform(value):
            if not value or value.get("leaseToken") != lease or value["status"] != "PROCESSING":
                raise LeaseLost()
            return {**value, **changes}
        return self.db.update("MEDIA_JOBS", job_id, transform)

    def work(self, job_id):
        record = self.db.get("MEDIA_JOBS", job_id)
        if not record:
            return True
        job = record.value
        if job["status"] in ("COMPLETED", "CANCELLED", "FAILED"):
            return not job.get("deadLetter", False)
        if job.get("leaseUntil", 0) > time.time() or job.get("availableAt", 0) > time.time():
            return False
        asset = self.asset(job["mediaId"])
        try:
            room = self.rooms.record(asset["roomId"])
        except HTTPException:
            try:
                self.db.commit([record.change({**job, "status": "CANCELLED", "errorCode": "ROOM_DELETED"})])
            except Conflict:
                pass
            return True
        lease = uuid.uuid4().hex
        if job.get("attempts", 0) >= MAX_RETRIES:
            try:
                self.db.commit([record.change({**job, "status": "FAILED", "errorCode": "PROCESSING_ERROR", "deadLetter": True})])
            except Conflict:
                return False
            self.emit(asset)
            return False
        job.update(status="PROCESSING", progress=0, errorCode=None, leaseToken=lease, leaseUntil=time.time() + 90, attempts=job.get("attempts", 0) + 1)
        try:
            self.db.commit([room.change(room.value, check=True), record.change(job)])
        except Conflict:
            return False
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(20):
                try:
                    self.update_leased(job_id, lease, {"leaseUntil": time.time() + 90})
                except Exception:
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        self.emit(asset)
        try:
            def progress(done, total):
                self.update_leased(job_id, lease, {"progress": int(done * 100 / max(1, total))})
                self.emit(asset)
            result = extract(asset["ext"], self.storage.get_limited(asset["s3Key"], max_bytes_for(asset["ext"])), progress, job.get("ocrLanguage", "auto"))
            result["version"] = asset.get("extractionVersion", 0) + 1
            key = f'extracted/{asset["mediaId"]}/{lease}.json'
            self.storage.put(key, json.dumps(result, ensure_ascii=False).encode())
            self.finish(job_id, lease, asset, result, key)
            terminal = True
        except LeaseLost:
            return False
        except ExtractError as error:
            self.update_leased(job_id, lease, {"status": "FAILED", "errorCode": error.code, "leaseUntil": 0})
            terminal = True
        except Exception:
            log.exception("Media processing failed")
            failed = job["attempts"] >= MAX_RETRIES
            self.update_leased(job_id, lease, {"status": "FAILED" if failed else "PENDING", "errorCode": "PROCESSING_ERROR" if failed else "RETRYING",
                               "retryCount": job["attempts"], "deadLetter": failed, "leaseUntil": 0, "availableAt": time.time() + 2 ** job["attempts"]})
            terminal = False
        finally:
            stop.set()
            thread.join(timeout=1)
        self.emit(asset)
        return terminal

    @retry_conflicts
    def finish(self, job_id, lease, asset, result, key):
        record = self.db.get("MEDIA_JOBS", job_id)
        if record.value.get("leaseToken") != lease or record.value["status"] != "PROCESSING":
            raise LeaseLost()
        media = self.db.get("MEDIA", "ASSET#" + asset["mediaId"])
        if media.value.get("extractionVersion", 0) != asset.get("extractionVersion", 0):
            self.db.commit([record.change({**record.value, "status": "CANCELLED", "errorCode": "SOURCE_VERSION_CHANGED", "leaseUntil": 0})])
            return
        try:
            room = self.rooms.record(asset["roomId"])
        except HTTPException:
            self.db.commit([record.change({**record.value, "status": "CANCELLED", "errorCode": "ROOM_DELETED", "leaseUntil": 0})])
            return
        self.db.commit([room.change(room.value, check=True),
                        record.change({**record.value, "status": "COMPLETED", "progress": 100, "errorCode": None, "leaseUntil": 0}),
                        media.change({**media.value, "transcriptKey": key, "extractionVersion": result["version"], "pageCount": len(result["pages"]), "needsOcr": result["needsOcr"],
                                      "detectedLanguages": sorted({p["language"] for p in result.get("pageDetails", []) if p.get("language")}),
                                      "recognitionModel": sorted({p["model"] for p in result.get("pageDetails", []) if p.get("model")})})])

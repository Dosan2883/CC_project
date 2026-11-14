"""파일 업로드, 처리 요청, 진행 상태 및 추출 결과 API."""
import asyncio
import io
import json
import uuid
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from app.core.database import Conflict
from app.core.runtime import db
from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.dependencies import current_user
from app.core.runtime import manager, media_svc, store, room_repo
from app.core.runtime import secret as signing_secret
from app.media import playback
from app.media.schemas import UploadRequest
from app.media.service import MAX_BYTES, MediaError, max_bytes_for
from app.rooms.service import require_member

router = APIRouter()


def media_member(media_id: str, user: str, allow_deleted: bool = False) -> dict:
    """미디어가 속한 방의 멤버인지 확인한다. 방 밖 사용자는 존재 여부도 알 수 없게 404로 답한다. 삭제된 파일은 없는 파일로 본다."""
    try:
        asset = media_svc.asset(media_id)
    except MediaError as e:
        raise HTTPException(e.status, e.code)
    if not allow_deleted and media_svc.is_deleted(asset):
        raise HTTPException(404, "MEDIA_NOT_FOUND")
    try:
        room_repo.require_member(asset["roomId"], user)
    except HTTPException:
        raise HTTPException(404, "MEDIA_NOT_FOUND")
    return asset


async def read_limited(request: Request, limit: int) -> bytes:
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(413, "FILE_TOO_LARGE")
    return bytes(buf)


@router.post("/api/files/upload-url")
def upload_url(body: UploadRequest, user: str = Depends(current_user)):
    """업로드 권한 검사 + 임시 업로드 주소 발급. AWS에서는 S3 Presigned URL을 돌려준다."""
    require_member(body.roomId, user)
    try:
        return media_svc.request_upload(body.roomId, user, body.filename, body.size)
    except MediaError as e:
        raise HTTPException(e.status, e.code)


@router.put("/api/files/upload/{media_id}")
async def upload_file(media_id: str, token: str, request: Request):
    """로컬 데모에서만 쓰는 업로드 수신부. AWS에서는 클라이언트가 S3로 직접 올리므로 사라진다.
    로그인 토큰이 아니라 upload-url에서 받은 1회용 토큰으로 권한을 확인한다(S3 Presigned URL과 같은 방식)."""
    try:
        asset = await asyncio.to_thread(media_svc.check_token, media_id, token)
        data = await read_limited(request, max_bytes_for(asset["ext"]))
        asset = await asyncio.to_thread(media_svc.receive_upload, media_id, token, data)
    except MediaError as e:
        raise HTTPException(e.status, e.code)
    await publish_file_card(media_id, asset)
    return {"ok": True}


async def publish_file_card(media_id, asset):
    card = {"mediaId": media_id, "filename": asset["filename"], "kind": asset["type"], "ext": asset["ext"], "size": asset["size"]}
    message = await asyncio.to_thread(store.append, asset["roomId"], asset["uploader"], asset["filename"], "und", card)
    await manager.broadcast(asset["roomId"], message)
    return message


@router.post("/api/files/{media_id}/complete")
async def complete_upload(media_id: str, user: str = Depends(current_user)):
    await asyncio.to_thread(media_member, media_id, user)
    try:
        asset = await asyncio.to_thread(media_svc.complete_upload, media_id, user)
    except MediaError as error:
        raise HTTPException(error.status, error.code)
    message = await publish_file_card(media_id, asset)
    return {"ok": True, "messageSeq": message["messageSeq"]}


@router.post("/api/media/{media_id}/process")
def process_media(media_id: str, language: str | None = None, user: str = Depends(current_user)):
    """추출 작업을 큐에 등록한다. 이미 있는 작업은 새로 만들지 않는다. AWS에서는 SQS Media에 등록."""
    media_member(media_id, user)
    try:
        media_svc.start_job(media_id, language)
        return media_svc.status(media_id)
    except MediaError as e:
        raise HTTPException(e.status, e.code)


class PageCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    text: str = Field(max_length=12000)
    language: str


@router.patch("/api/media/{media_id}/pages/{page}")
def correct_page(media_id: str, page: int, body: PageCorrection, user: str = Depends(current_user)):
    from app.media.ocr import LANGUAGES
    asset = media_member(media_id, user)
    if body.language not in LANGUAGES:
        raise HTTPException(422, "INVALID_LANGUAGE")
    record = db.get("MEDIA", "ASSET#" + media_id)
    result = media_svc.transcript(media_id)
    if not 1 <= page <= len(result["pages"]):
        raise HTTPException(422, "INVALID_PAGE")
    if body.version != result.get("version", 1):
        raise HTTPException(409, "VERSION_CONFLICT")
    result.setdefault("originalPages", list(result["pages"]))
    result["pages"][page - 1] = body.text
    result["version"] = body.version + 1
    corrections = result.setdefault("corrections", {})
    corrections[str(page)] = {"language": body.language, "user": user, "version": result["version"]}
    key = f"extracted/{media_id}/revision-{uuid.uuid4().hex}.json"
    media_svc.storage.put(key, json.dumps(result, ensure_ascii=False).encode())
    room = room_repo.record(asset["roomId"])
    member = db.get("ROOM#" + asset["roomId"], "MEMBER#" + user)
    if not member:
        raise HTTPException(403, "NOT_A_MEMBER")
    try:
        db.commit([room.change(room.value, check=True), member.change(member.value, check=True),
                   record.change({**record.value, "transcriptKey": key, "extractionVersion": result["version"]})])
    except Conflict:
        raise HTTPException(409, "VERSION_CONFLICT")
    return result


@router.get("/api/media/{media_id}/pages/{page}/image")
def page_image(media_id: str, page: int, user: str = Depends(current_user)):
    from app.media.ocr import raster
    asset = media_member(media_id, user)
    if asset["ext"] not in ("pdf", "jpg", "jpeg", "png"):
        raise HTTPException(422, "NO_PAGE_IMAGE")
    try:
        source = raster(media_svc.storage.get_limited(asset["s3Key"], MAX_BYTES), asset["ext"], page)
        output = io.BytesIO()
        source.save(output, format="PNG")
        return Response(output.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})
    except (ValueError, IndexError):
        raise HTTPException(422, "INVALID_PAGE")


@router.get("/api/media/{media_id}/play-url")
def play_url(media_id: str, user: str = Depends(current_user)):
    """음성·영상 재생 주소. <audio>는 로그인 토큰을 실을 수 없어 한 파일에만 유효한 임시 토큰을 붙인다."""
    asset = media_member(media_id, user)
    if asset["ext"] not in playback.CONTENT_TYPES:
        raise HTTPException(422, "NOT_PLAYABLE")
    if hasattr(media_svc.storage, "download_url"):  # S3 모드: 서버를 거치지 않고 S3가 직접 보낸다
        return {"url": media_svc.storage.download_url(asset["s3Key"], playback.PLAY_TTL, playback.CONTENT_TYPES[asset["ext"]])}
    return {"url": f"/api/media/{media_id}/stream?token={playback.make_token(signing_secret, media_id, user)}"}


@router.get("/api/media/{media_id}/stream")
def stream_media(media_id: str, token: str, request: Request):
    user = playback.read_token(signing_secret, token, media_id)
    if not user:
        raise HTTPException(401, "INVALID_PLAY_TOKEN")
    asset = media_member(media_id, user)  # 토큰을 받은 뒤 방에서 나갔거나 파일이 삭제됐으면 거부한다
    key = asset["s3Key"]
    try:
        size = media_svc.storage.size(key)
    except FileNotFoundError:
        raise HTTPException(404, "MEDIA_NOT_FOUND")
    header = request.headers.get("range")
    span = playback.parse_range(header, size)
    if span is None:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    start, end = span
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store"}
    if header:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(media_svc.storage.read_range(key, start, end), status_code=206 if header else 200,
                    media_type=playback.CONTENT_TYPES.get(asset["ext"], "application/octet-stream"), headers=headers)


@router.get("/api/rooms/{room_id}/files")
def room_files(room_id: str, user: str = Depends(current_user)):
    room_repo.require_member(room_id, user)
    return media_svc.list_room_files(room_id)


@router.get("/api/media/{media_id}")
def media_status(media_id: str, user: str = Depends(current_user)):
    asset = media_member(media_id, user, allow_deleted=True)
    if media_svc.is_deleted(asset):  # 채팅의 파일 카드가 삭제된 파일로 표시할 수 있게 상태만 알려 준다
        return {"mediaId": media_id, "filename": asset["filename"], "kind": asset["type"], "size": asset["size"], "status": "DELETED",
                "progress": 100, "errorCode": None, "needsOcr": False, "pageCount": 0, "retryCount": 0}
    return media_svc.status(media_id)


@router.delete("/api/files/{media_id}")
def delete_file(media_id: str, user: str = Depends(current_user)):
    try:
        return media_svc.delete_file(media_id, user)
    except MediaError as e:
        raise HTTPException(e.status, e.code)


@router.get("/api/media/{media_id}/transcript")
def media_transcript(media_id: str, user: str = Depends(current_user)):
    media_member(media_id, user)
    try:
        return media_svc.transcript(media_id)
    except MediaError as e:
        raise HTTPException(e.status, e.code)

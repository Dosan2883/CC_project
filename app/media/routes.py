"""파일 업로드, 처리 요청, 진행 상태 및 추출 결과 API."""
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.dependencies import current_user
from app.core.runtime import manager, media_svc, store, room_repo
from app.media.schemas import UploadRequest
from app.media.service import MAX_BYTES, MediaError
from app.rooms.service import require_member

router = APIRouter()


def media_member(media_id: str, user: str) -> dict:
    """미디어가 속한 방의 멤버인지 확인한다. 방 밖 사용자는 존재 여부도 알 수 없게 404로 답한다."""
    try:
        asset = media_svc.asset(media_id)
    except MediaError as e:
        raise HTTPException(e.status, e.code)
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
        await asyncio.to_thread(media_svc.check_token, media_id, token)
        data = await read_limited(request, MAX_BYTES)
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
def process_media(media_id: str, user: str = Depends(current_user)):
    """추출 작업을 큐에 등록한다. 이미 있는 작업은 새로 만들지 않는다. AWS에서는 SQS Media에 등록."""
    media_member(media_id, user)
    try:
        media_svc.start_job(media_id)
        return media_svc.status(media_id)
    except MediaError as e:
        raise HTTPException(e.status, e.code)


@router.get("/api/media/{media_id}")
def media_status(media_id: str, user: str = Depends(current_user)):
    media_member(media_id, user)
    return media_svc.status(media_id)


@router.get("/api/media/{media_id}/transcript")
def media_transcript(media_id: str, user: str = Depends(current_user)):
    media_member(media_id, user)
    try:
        return media_svc.transcript(media_id)
    except MediaError as e:
        raise HTTPException(e.status, e.code)

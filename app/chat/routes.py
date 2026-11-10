"""WebSocket 인증, 영속 메시지와 명시적 읽음 갱신."""
import asyncio
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.auth.dependencies import current_user
from app.core.config import LANGUAGES
from app.core.runtime import auth, manager, preferences, room_repo, store

router = APIRouter()


class ReadRequest(BaseModel):
    lastReadSeq: int = Field(ge=0)


@router.post("/api/rooms/{room_id}/read")
async def mark_read(room_id: str, body: ReadRequest, user: str = Depends(current_user)):
    member = await asyncio.to_thread(room_repo.mark_read, room_id, user, body.lastReadSeq)
    await manager.broadcast(room_id, {"type": "read:update", **member})
    return member


@router.websocket("/ws/{room}")
async def chat(ws: WebSocket, room: str, lastSeq: int = 0):
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=5)
        user = await asyncio.to_thread(auth.verify, first.get("token")) if isinstance(first, dict) and first.get("type") == "auth" else None
    except Exception:
        user = None
    if not user:
        await ws.close(code=4401)
        return
    try:
        await asyncio.to_thread(room_repo.require_member, room, user)
        saved_room = await asyncio.to_thread(room_repo.get, room)
    except HTTPException as error:
        await ws.close(code=4404 if error.status_code == 404 else 4403)
        return
    if lastSeq < 0 or lastSeq > saved_room["lastMessageSeq"]:
        await ws.close(code=4400)
        return
    manager.add(room, user, ws, lastSeq)
    try:
        async with manager.locks[ws]:
            await manager.replay(room, ws)
        await manager.publish_presence()
        while True:
            data = await ws.receive_json()
            try:
                await asyncio.to_thread(room_repo.require_member, room, user)
                if not isinstance(data, dict):
                    raise HTTPException(422, "invalid event")
                if data.get("type") == "message:send":
                    body = str(data.get("body", "")).strip()
                    if len(body) > 8000:
                        raise HTTPException(422, "message too long")
                    if body:
                        lang = data.get("sourceLanguage")
                        client_message_id = data.get("clientMessageId")
                        message = await asyncio.to_thread(
                            store.append, room, user, body,
                            lang if lang in LANGUAGES else "und", None, client_message_id,
                        )
                        await manager.broadcast(room, message)
                        await ws.send_json({"type": "message:ack", "clientMessageId": message.get("clientMessageId"), "messageSeq": message["messageSeq"]})
                elif data.get("type") == "preferences:update":
                    saved = await asyncio.to_thread(preferences.update, user, {key: value for key, value in data.items() if key != "type"})
                    await ws.send_json({"type": "preferences:updated", **saved})
                elif data.get("type") == "read:update":
                    read = ReadRequest.model_validate(data)
                    member = await asyncio.to_thread(room_repo.mark_read, room, user, read.lastReadSeq)
                    await manager.broadcast(room, {"type": "read:update", **member})
            except HTTPException as error:
                if error.status_code in (403, 404):
                    await ws.close(code=4404 if error.status_code == 404 else 4403)
                    break
                await ws.send_json({"type": "error", "detail": error.detail, "clientMessageId": data.get("clientMessageId") if isinstance(data, dict) else None})
            except ValueError:
                await ws.send_json({"type": "error", "detail": "invalid event"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        manager.remove(room, user, ws)
        await manager.publish_presence()

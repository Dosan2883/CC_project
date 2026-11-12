"""채팅방 목록, 생성, 입장 및 나가기 API."""
import os
import asyncio
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import current_user
from app.auth.service import hash_password
from app.core.runtime import manager, room_repo, room_throttle
from app.rooms.schemas import CreateRoom, EnterPrivate
from app.rooms.service import check_room_password, find_by_name, get_room, newest_first, normalize, room_view

router = APIRouter()


def room_list(user, public=False):
    views = []
    for room in room_repo.all():
        if public and room["visibility"] != "public":
            continue
        if not public and not room_repo.member(room["roomId"], user):
            continue
        try:
            views.append(room_view(room["roomId"], user))
        except HTTPException as error:
            if error.status_code != 404:
                raise
    return newest_first(views)


@router.get("/api/rooms")
def my_rooms(user: str = Depends(current_user)):
    """내가 들어가 있는 방(공개·비공개 모두)."""
    return room_list(user)


@router.get("/api/rooms/public")
def public_rooms(user: str = Depends(current_user)):
    """공개방 목록. 비공개방은 절대 포함하지 않는다."""
    return room_list(user, public=True)


@router.post("/api/rooms")
async def create_room(body: CreateRoom, user: str = Depends(current_user)):
    name = normalize(body.name)
    if not name:
        raise HTTPException(400, "name is required")
    if len(name) > 40:
        raise HTTPException(400, "room name is too long")
    if await asyncio.to_thread(find_by_name, name):
        raise HTTPException(409, "room name already exists")
    room = {"roomId": uuid.uuid4().hex, "name": name, "key": name.casefold(), "visibility": body.visibility,
            "createdBy": user, "createdAt": int(time.time() * 1000)}
    if body.visibility == "private":
        if not 4 <= len(body.password) <= 64:
            raise HTTPException(400, "password must be 4-64 characters")
        room["salt"] = os.urandom(16)
        room["pwHash"] = hash_password(body.password, room["salt"])
    await asyncio.to_thread(room_repo.create, room)
    return await asyncio.to_thread(room_view, room["roomId"], user)


@router.post("/api/rooms/{room_id}/join")
async def join_public_room(room_id: str, user: str = Depends(current_user)):
    """공개방은 비밀번호 없이 들어간다. 비공개방은 이 경로로 들어갈 수 없다."""
    room = await asyncio.to_thread(get_room, room_id)
    if room["visibility"] != "public":
        raise HTTPException(404, "room not found")  # 비공개방의 존재를 드러내지 않는다
    if await asyncio.to_thread(room_repo.join, room_id, user):
        await manager.broadcast(room_id, {"type": "system", "event": "joined", "user": user})
    return await asyncio.to_thread(room_view, room_id, user)


@router.post("/api/rooms/enter-private")
async def enter_private_room(body: EnterPrivate, user: str = Depends(current_user)):
    """비공개방은 방 이름과 비밀번호가 모두 맞아야 들어간다."""
    if await asyncio.to_thread(room_throttle.blocked, user):
        raise HTTPException(429, "too many attempts, try again later")
    room_id = await asyncio.to_thread(find_by_name, body.name)
    room = await asyncio.to_thread(get_room, room_id) if room_id else None
    # 이름이 틀렸는지 비밀번호가 틀렸는지 알려 주지 않는다 (비공개방 존재 노출 방지).
    if not room or room["visibility"] != "private" or not check_room_password(room, body.password):
        await asyncio.to_thread(room_throttle.fail, user)
        raise HTTPException(403, "invalid room name or password")
    await asyncio.to_thread(room_throttle.clear, user)
    if await asyncio.to_thread(room_repo.join, room_id, user):
        await manager.broadcast(room_id, {"type": "system", "event": "joined", "user": user})
    return await asyncio.to_thread(room_view, room_id, user)


@router.post("/api/rooms/{room_id}/leave")
async def leave_room(room_id: str, user: str = Depends(current_user)):
    await asyncio.to_thread(get_room, room_id)
    if await asyncio.to_thread(room_repo.leave, room_id, user):
        await manager.broadcast(room_id, {"type": "member:left", "user": user})
        await manager.broadcast(room_id, {"type": "system", "event": "left", "user": user})
        await manager.publish_presence()
    return {"ok": True}


@router.delete("/api/rooms/{room_id}")
async def delete_room(room_id: str, user: str = Depends(current_user)):
    await asyncio.to_thread(room_repo.delete, room_id, user)
    await manager.broadcast(room_id, {"type": "room:deleted", "roomId": room_id})
    return {"ok": True}


@router.get("/api/rooms/{room_id}/members")
def room_members(room_id: str, user: str = Depends(current_user)):
    room_repo.require_member(room_id, user)
    return room_repo.members(room_id)

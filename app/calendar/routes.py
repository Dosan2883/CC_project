"""방 일정(캘린더) API. 일정이 바뀌면 방 안의 접속자에게 calendar:changed를 보내 화면이 목록을 다시 읽게 한다."""
import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.ai.routes import Source
from app.auth.dependencies import current_user
from app.calendar.service import CalendarService
from app.calendar.suggest import DeadlineFinder
from app.core.runtime import ai_jobs, db, manager, room_repo, translation

router = APIRouter()
service = CalendarService(db, room_repo)
finder = DeadlineFinder(db, ai_jobs, translation, translation.translator)
log = logging.getLogger(__name__)


class EventBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    date: str
    time: str = ""
    note: str = ""


class EventPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    date: str | None = None
    time: str | None = None
    note: str | None = None


class SuggestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[Source] = Field(min_length=1, max_length=20)
    consent: bool
    tzOffsetMinutes: int = Field(default=0, ge=-840, le=840)  # 사용자 시간대(UTC보다 몇 분 앞서는지, 한국은 +540)


async def announce(room_id, action, event_id, user):
    try:
        await manager.broadcast(room_id, {"type": "calendar:changed", "roomId": room_id, "action": action, "eventId": event_id, "by": user})
    except Exception:
        log.exception("Calendar notification failed")  # 일정 저장은 이미 끝났으므로 알림 실패로 요청을 실패시키지 않는다


@router.post("/api/rooms/{room_id}/events/suggest")
async def suggest_events(room_id: str, body: SuggestBody, user: str = Depends(current_user)):
    """선택한 자료에서 마감·일정 후보를 찾는다. 후보는 저장하지 않는다(저장은 일정 추가 API)."""
    sources = [source.model_dump() for source in body.sources]
    return await asyncio.to_thread(finder.suggest, room_id, user, sources, body.tzOffsetMinutes, body.consent)


@router.get("/api/rooms/{room_id}/events")
def list_events(room_id: str, month: str | None = None, user: str = Depends(current_user)):
    return service.list(room_id, user, month)


@router.post("/api/rooms/{room_id}/events", status_code=201)
async def create_event(room_id: str, body: EventBody, user: str = Depends(current_user)):
    event = await asyncio.to_thread(service.create, room_id, user, body.model_dump())
    await announce(room_id, "created", event["id"], user)
    return event


@router.patch("/api/rooms/{room_id}/events/{event_id}")
async def update_event(room_id: str, event_id: str, body: EventPatch, user: str = Depends(current_user)):
    event = await asyncio.to_thread(service.update, room_id, event_id, user, body.model_dump(exclude_unset=True))
    await announce(room_id, "updated", event_id, user)
    return event


@router.delete("/api/rooms/{room_id}/events/{event_id}")
async def delete_event(room_id: str, event_id: str, user: str = Depends(current_user)):
    result = await asyncio.to_thread(service.delete, room_id, event_id, user)
    await announce(room_id, "deleted", event_id, user)
    return result

"""방 단위로 공유하는 일정. 저장은 공용 DB의 CALENDAR#{방} 파티션(sk=EVENT#{일정 번호})에 한다.

방 구성원이면 누구나 일정을 보고 추가할 수 있고, 고치거나 지우는 것은 만든 사람과 방 주인만 할 수 있다.
방이 삭제되면 방 구성원 확인에서 막혀 일정도 보이지 않는다.
"""
import re
import time
import uuid
from datetime import date

from fastapi import HTTPException

from app.core.database import Change, Conflict

MAX_EVENTS = 500
MAX_TITLE = 100
MAX_NOTE = 500
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def clean_title(value):
    title = " ".join(str(value or "").split())
    if not title:
        raise HTTPException(422, "TITLE_REQUIRED")
    if len(title) > MAX_TITLE:
        raise HTTPException(422, "TITLE_TOO_LONG")
    return title


def clean_date(value):
    if not isinstance(value, str) or not DATE_PATTERN.match(value):
        raise HTTPException(422, "INVALID_DATE")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, "INVALID_DATE") from None
    if not 2000 <= parsed.year <= 2100:
        raise HTTPException(422, "INVALID_DATE")
    return value


def clean_time(value):
    value = value or ""
    if value and not TIME_PATTERN.match(value):
        raise HTTPException(422, "INVALID_TIME")
    return value


def clean_note(value):
    note = str(value or "").strip()
    if len(note) > MAX_NOTE:
        raise HTTPException(422, "NOTE_TOO_LONG")
    return note


class CalendarService:
    def __init__(self, db, rooms):
        self.db, self.rooms = db, rooms

    @staticmethod
    def partition(room_id):
        return "CALENDAR#" + room_id

    @staticmethod
    def sort_key(event):
        return (event["date"], event["time"] or "", event["createdAt"])

    @staticmethod
    def allowed(member, event, user):
        return user == event["createdBy"] or member.get("role") == "owner"

    def view(self, member, event, user):
        return {**event, "canEdit": self.allowed(member, event, user)}

    def list(self, room_id, user, month=None):
        member = self.rooms.require_member(room_id, user)
        if month is not None and not MONTH_PATTERN.match(month):
            raise HTTPException(422, "INVALID_MONTH")
        events = [record.value for record in self.db.query(self.partition(room_id), "EVENT#")]
        if month:
            events = [event for event in events if event["date"].startswith(month)]
        return [self.view(member, event, user) for event in sorted(events, key=self.sort_key)]

    def create(self, room_id, user, data):
        member = self.rooms.require_member(room_id, user)
        title, day = clean_title(data.get("title")), clean_date(data.get("date"))
        clock, note = clean_time(data.get("time")), clean_note(data.get("note"))
        partition = self.partition(room_id)
        if len(self.db.query(partition, "EVENT#")) >= MAX_EVENTS:
            raise HTTPException(409, "TOO_MANY_EVENTS")
        now = int(time.time() * 1000)
        event_id = uuid.uuid4().hex
        event = {"id": event_id, "roomId": room_id, "title": title, "date": day, "time": clock, "note": note,
                 "createdBy": user, "createdAt": now, "updatedBy": user, "updatedAt": now}
        self.db.commit([Change(partition, "EVENT#" + event_id, None, event)])
        return self.view(member, event, user)

    def update(self, room_id, event_id, user, patch):
        member = self.rooms.require_member(room_id, user)
        partition = self.partition(room_id)
        for _ in range(5):
            record = self.db.get(partition, "EVENT#" + event_id)
            if not record:
                raise HTTPException(404, "EVENT_NOT_FOUND")
            event = record.value
            if not self.allowed(member, event, user):
                raise HTTPException(403, "NOT_EVENT_OWNER")
            changed = dict(event)
            if patch.get("title") is not None:
                changed["title"] = clean_title(patch["title"])
            if patch.get("date") is not None:
                changed["date"] = clean_date(patch["date"])
            if patch.get("time") is not None:
                changed["time"] = clean_time(patch["time"])
            if patch.get("note") is not None:
                changed["note"] = clean_note(patch["note"])
            changed.update(updatedBy=user, updatedAt=int(time.time() * 1000))
            try:
                self.db.commit([record.change(changed)])
            except Conflict:
                continue
            return self.view(member, changed, user)
        raise HTTPException(409, "EVENT_CONFLICT")

    def delete(self, room_id, event_id, user):
        member = self.rooms.require_member(room_id, user)
        partition = self.partition(room_id)
        for _ in range(5):
            record = self.db.get(partition, "EVENT#" + event_id)
            if not record:
                raise HTTPException(404, "EVENT_NOT_FOUND")
            if not self.allowed(member, record.value, user):
                raise HTTPException(403, "NOT_EVENT_OWNER")
            try:
                self.db.commit([record.change(None)])
            except Conflict:
                continue
            return {"id": event_id, "deleted": True}
        raise HTTPException(409, "EVENT_CONFLICT")

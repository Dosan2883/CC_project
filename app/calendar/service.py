"""방 단위로 공유하는 일정. 저장은 공용 DB의 CALENDAR#{방} 파티션(sk=EVENT#{일정 번호})에 한다.

방 구성원이면 누구나 일정을 보고 추가할 수 있고, 고치거나 지우는 것은 만든 사람과 방 주인만 할 수 있다.
방이 삭제되면 방 구성원 확인에서 막혀 일정도 보이지 않는다.
"""
import re
import time
import uuid
from datetime import date

from fastapi import HTTPException

from app.core.database import Change
from app.rooms.repository import retry_conflicts

MAX_EVENTS = 500
MAX_TITLE = 100
MAX_NOTE = 500
MAX_ROOM_NAME = 40
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


def clean_room_tag(room_id, room_name):
    """개인 일정에 붙이는 '어느 방과 관련된 일정인지' 표시. 화면에서 방마다 다른 색으로 보여 주는 데만 쓰고 권한과는 무관하다."""
    room_id = str(room_id or "").strip()
    name = " ".join(str(room_name or "").split())
    if len(room_id) > 64 or len(name) > MAX_ROOM_NAME:
        raise HTTPException(422, "INVALID_ROOM")
    return (room_id, name) if room_id else ("", "")


FIELD_VALIDATORS = {"title": clean_title, "date": clean_date, "time": clean_time, "note": clean_note}


def merged_event(event, patch):
    """수정 요청에 들어 있는 칸만 검증해서 바꾼 새 일정을 돌려준다(보내지 않은 칸은 그대로 둔다)."""
    changed = dict(event)
    for field, validate in FIELD_VALIDATORS.items():
        if patch.get(field) is not None:
            changed[field] = validate(patch[field])
    return changed


def check_not_duplicate(rows, record, event, changed):
    """제목·날짜·시간이 바뀌어 다른 일정과 같아지면 막는다(같은 방·같은 사람 안에서만 비교한다)."""
    key = CalendarService.schedule_key(changed)
    if key != CalendarService.schedule_key(event) and any(
        row.sk != record.sk and CalendarService.schedule_key(row.value) == key for row in rows
    ):
        raise HTTPException(409, "DUPLICATE_EVENT")


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
    def schedule_key(event):
        return (event["title"].casefold(), event["date"], event.get("time") or "")

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

    def write_context(self, room_id, user):
        room = self.rooms.record(room_id)
        member = self.db.get("ROOM#" + room_id, "MEMBER#" + user)
        if not member:
            raise HTTPException(403, "not a member")
        partition = self.partition(room_id)
        revision = self.db.get(partition, "REVISION")
        # Every mutation advances the same version, so a concurrent insert cannot bypass the scan.
        advance = Change(partition, "REVISION", revision.version if revision else None, {})
        checks = [room.change(room.value, check=True), member.change(member.value, check=True)]
        return member.value, partition, advance, checks

    def create(self, room_id, user, data):
        event, _ = self.create_with_status(room_id, user, data)
        return event

    @retry_conflicts
    def create_with_status(self, room_id, user, data):
        member, partition, advance, checks = self.write_context(room_id, user)
        title, day = clean_title(data.get("title")), clean_date(data.get("date"))
        clock, note = clean_time(data.get("time")), clean_note(data.get("note"))
        events = self.db.query(partition, "EVENT#")
        for record in events:
            if self.schedule_key(record.value) == (title.casefold(), day, clock):
                self.db.commit([*checks, record.change(record.value, check=True)])
                return self.view(member, record.value, user), False
        if len(events) >= MAX_EVENTS:
            raise HTTPException(409, "TOO_MANY_EVENTS")
        now = int(time.time() * 1000)
        event_id = uuid.uuid4().hex
        event = {"id": event_id, "roomId": room_id, "title": title, "date": day, "time": clock, "note": note,
                 "createdBy": user, "createdAt": now, "updatedBy": user, "updatedAt": now}
        self.db.commit([*checks, advance, Change(partition, "EVENT#" + event_id, None, event)])
        return self.view(member, event, user), True

    @retry_conflicts
    def update(self, room_id, event_id, user, patch):
        member, partition, advance, checks = self.write_context(room_id, user)
        record = self.db.get(partition, "EVENT#" + event_id)
        if not record:
            raise HTTPException(404, "EVENT_NOT_FOUND")
        event = record.value
        if not self.allowed(member, event, user):
            raise HTTPException(403, "NOT_EVENT_OWNER")
        changed = merged_event(event, patch)
        check_not_duplicate(self.db.query(partition, "EVENT#"), record, event, changed)
        changed.update(updatedBy=user, updatedAt=int(time.time() * 1000))
        self.db.commit([*checks, advance, record.change(changed)])
        return self.view(member, changed, user)

    @retry_conflicts
    def delete(self, room_id, event_id, user):
        member, partition, advance, checks = self.write_context(room_id, user)
        record = self.db.get(partition, "EVENT#" + event_id)
        if not record:
            raise HTTPException(404, "EVENT_NOT_FOUND")
        if not self.allowed(member, record.value, user):
            raise HTTPException(403, "NOT_EVENT_OWNER")
        self.db.commit([*checks, advance, record.change(None)])
        return {"id": event_id, "deleted": True}


class PersonalCalendarService:
    """개인 일정: 사용자마다 자기 달력이 따로 있고, 본인이 직접 등록한 일정만 들어간다. 다른 사람은 볼 수 없다.
    저장은 공용 DB의 MYCAL#{닉네임} 파티션(sk=EVENT#{일정 번호})에 하며 검증 규칙은 방 일정과 같다."""

    def __init__(self, db):
        self.db = db

    @staticmethod
    def partition(user):
        return "MYCAL#" + user.casefold()

    @staticmethod
    def view(event):
        return {**event, "canEdit": True}  # 내 달력의 일정은 모두 내가 고칠 수 있다

    def list(self, user, month=None):
        if month is not None and not MONTH_PATTERN.match(month):
            raise HTTPException(422, "INVALID_MONTH")
        events = [record.value for record in self.db.query(self.partition(user), "EVENT#")]
        if month:
            events = [event for event in events if event["date"].startswith(month)]
        return [self.view(event) for event in sorted(events, key=CalendarService.sort_key)]

    def advance(self, partition):
        # 모든 변경이 같은 REVISION을 올리므로 동시에 추가해도 개수 제한을 피할 수 없다.
        revision = self.db.get(partition, "REVISION")
        return Change(partition, "REVISION", revision.version if revision else None, {})

    @retry_conflicts
    def create_with_status(self, user, data):
        partition = self.partition(user)
        advance = self.advance(partition)
        title, day = clean_title(data.get("title")), clean_date(data.get("date"))
        clock, note = clean_time(data.get("time")), clean_note(data.get("note"))
        room_id, room_name = clean_room_tag(data.get("roomId"), data.get("roomName"))
        events = self.db.query(partition, "EVENT#")
        for record in events:
            if CalendarService.schedule_key(record.value) == (title.casefold(), day, clock):
                self.db.commit([record.change(record.value, check=True)])
                return self.view(record.value), False
        if len(events) >= MAX_EVENTS:
            raise HTTPException(409, "TOO_MANY_EVENTS")
        now = int(time.time() * 1000)
        event_id = uuid.uuid4().hex
        event = {"id": event_id, "title": title, "date": day, "time": clock, "note": note, "roomId": room_id, "roomName": room_name,
                 "createdBy": user, "createdAt": now, "updatedBy": user, "updatedAt": now}
        self.db.commit([advance, Change(partition, "EVENT#" + event_id, None, event)])
        return self.view(event), True

    def create(self, user, data):
        return self.create_with_status(user, data)[0]

    @retry_conflicts
    def update(self, event_id, user, patch):
        partition = self.partition(user)
        advance = self.advance(partition)
        record = self.db.get(partition, "EVENT#" + event_id)
        if not record:
            raise HTTPException(404, "EVENT_NOT_FOUND")
        event = record.value
        changed = merged_event(event, patch)
        if patch.get("roomId") is not None:
            changed["roomId"], changed["roomName"] = clean_room_tag(patch["roomId"], patch.get("roomName"))
        check_not_duplicate(self.db.query(partition, "EVENT#"), record, event, changed)
        changed.update(updatedBy=user, updatedAt=int(time.time() * 1000))
        self.db.commit([advance, record.change(changed)])
        return self.view(changed)

    @retry_conflicts
    def delete(self, event_id, user):
        partition = self.partition(user)
        advance = self.advance(partition)
        record = self.db.get(partition, "EVENT#" + event_id)
        if not record:
            raise HTTPException(404, "EVENT_NOT_FOUND")
        self.db.commit([advance, record.change(None)])
        return {"id": event_id, "deleted": True}

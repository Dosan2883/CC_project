"""방·역할·읽음 순번. 조건부 트랜잭션으로 방 삭제와 쓰기의 경합을 막는다."""
import functools
import random
import time

from fastapi import HTTPException

from app.core.database import Change, Conflict


def retry_conflicts(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        for _ in range(40):
            try:
                return function(*args, **kwargs)
            except Conflict:
                time.sleep(random.uniform(0.001, 0.02))
        raise HTTPException(409, "concurrent update, retry request")
    return wrapped


class RoomRepository:
    def __init__(self, db):
        self.db = db

    def record(self, room_id):
        record = self.db.get("CATALOG", "ROOM#" + room_id)
        if not record or record.value.get("deletedAt"):
            raise HTTPException(404, "room not found")
        return record

    def get(self, room_id):
        return self.record(room_id).value

    def all(self):
        return [record.value for record in self.db.query("CATALOG", "ROOM#") if not record.value.get("deletedAt")]

    def find_by_name(self, name):
        record = self.db.get("ROOM_NAMES", name)
        return record.value["roomId"] if record else None

    def member(self, room_id, user):
        record = self.db.get("ROOM#" + room_id, "MEMBER#" + user)
        return record.value if record else None

    def members(self, room_id):
        return [record.value for record in self.db.query("ROOM#" + room_id, "MEMBER#")]

    def require_member(self, room_id, user):
        self.record(room_id)
        member = self.member(room_id, user)
        if not member:
            raise HTTPException(403, "not a member")
        return member

    def create(self, room):
        member = {"user": room["createdBy"], "role": "owner", "lastReadSeq": 0, "joinedAt": room["createdAt"]}
        room = {**room, "lastMessageSeq": 0}
        try:
            self.db.commit([
                Change("ROOM_NAMES", room["key"], None, {"roomId": room["roomId"]}),
                Change("CATALOG", "ROOM#" + room["roomId"], None, room),
                Change("ROOM#" + room["roomId"], "MEMBER#" + member["user"], None, member),
            ])
        except Conflict:
            raise HTTPException(409, "room name already exists")

    @retry_conflicts
    def join(self, room_id, user):
        room = self.record(room_id)
        if self.member(room_id, user):
            return False
        member = {"user": user, "role": "owner" if room.value["createdBy"] == user else "member", "lastReadSeq": 0, "joinedAt": int(time.time() * 1000)}
        self.db.commit([room.change(room.value, check=True), Change("ROOM#" + room_id, "MEMBER#" + user, None, member)])
        return True

    @retry_conflicts
    def leave(self, room_id, user):
        room = self.record(room_id)
        member = self.db.get("ROOM#" + room_id, "MEMBER#" + user)
        if not member:
            return False
        self.db.commit([room.change(room.value, check=True), member.change(None)])
        return True

    @retry_conflicts
    def delete(self, room_id, user):
        room = self.record(room_id)
        if room.value["createdBy"] != user:
            raise HTTPException(403, "only the creator can delete this room")
        name = self.db.get("ROOM_NAMES", room.value["key"])
        changes = [room.change({**room.value, "deletedAt": int(time.time() * 1000)})]
        if name and name.value["roomId"] == room_id:
            changes.append(name.change(None))
        self.db.commit(changes)

    @retry_conflicts
    def mark_read(self, room_id, user, seq):
        room = self.record(room_id)
        member = self.db.get("ROOM#" + room_id, "MEMBER#" + user)
        if not member:
            raise HTTPException(403, "not a member")
        if seq < 0 or seq > room.value["lastMessageSeq"]:
            raise HTTPException(422, "invalid read sequence")
        updated = {**member.value, "lastReadSeq": max(seq, member.value["lastReadSeq"])}
        self.db.commit([room.change(room.value, check=True), member.change(updated)])
        return updated

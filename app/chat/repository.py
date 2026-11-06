"""메시지 순번과 내용을 같은 트랜잭션으로 저장한다."""
import time

from fastapi import HTTPException

from app.core.database import Change
from app.rooms.repository import retry_conflicts


class MessageStore:
    def __init__(self, db, rooms):
        self.db, self.rooms = db, rooms

    @retry_conflicts
    def append(self, room, user, body, source_lang, media=None):
        record = self.rooms.record(room)
        member = self.db.get("ROOM#" + room, "MEMBER#" + user)
        if not member:
            raise HTTPException(403, "not a member")
        unique = self.db.get("ROOM#" + room, "MEDIA_MESSAGE#" + media["mediaId"]) if media else None
        if unique:
            return self.db.get("ROOM#" + room, f'MESSAGE#{unique.value["messageSeq"]:020d}').value
        seq = record.value["lastMessageSeq"] + 1
        message = {"type": "message:new", "roomId": room, "messageSeq": seq, "sender": user, "bodyOriginal": body,
                   "sourceLanguage": source_lang, "sentAt": int(time.time() * 1000)}
        if media:
            message["media"] = media
        changes = [record.change({**record.value, "lastMessageSeq": seq}), member.change(member.value, check=True),
                   Change("ROOM#" + room, f"MESSAGE#{seq:020d}", None, message)]
        if media:
            changes.append(Change("ROOM#" + room, "MEDIA_MESSAGE#" + media["mediaId"], None, {"messageSeq": seq}))
        self.db.commit(changes)
        return message

    def after(self, room, last_seq):
        return [record.value for record in self.db.query("ROOM#" + room, "MESSAGE#") if record.value["messageSeq"] > last_seq]

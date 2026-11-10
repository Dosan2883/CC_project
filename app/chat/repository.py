"""메시지 순번과 내용을 같은 트랜잭션으로 저장한다."""
import time
import re

from fastapi import HTTPException

from app.core.database import Change
from app.rooms.repository import retry_conflicts


class MessageStore:
    def __init__(self, db, rooms):
        self.db, self.rooms = db, rooms

    @retry_conflicts
    def append(self, room, user, body, source_lang, media=None, client_message_id=None):
        record = self.rooms.record(room)
        member = self.db.get("ROOM#" + room, "MEMBER#" + user)
        if not member:
            raise HTTPException(403, "not a member")
        if client_message_id is not None:
            if not isinstance(client_message_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", client_message_id):
                raise HTTPException(422, "invalid client message id")
            existing = self.db.get("ROOM#" + room, f"CLIENT#{user}#{client_message_id}")
            if existing:
                saved = self.db.get("ROOM#" + room, f'MESSAGE#{existing.value["messageSeq"]:020d}')
                if saved:
                    return saved.value
        unique = self.db.get("ROOM#" + room, "MEDIA_MESSAGE#" + media["mediaId"]) if media else None
        if unique:
            return self.db.get("ROOM#" + room, f'MESSAGE#{unique.value["messageSeq"]:020d}').value
        seq = record.value["lastMessageSeq"] + 1
        message = {"type": "message:new", "roomId": room, "messageSeq": seq, "sender": user, "bodyOriginal": body,
                   "sourceLanguage": source_lang, "sentAt": int(time.time() * 1000)}
        if client_message_id:
            message["clientMessageId"] = client_message_id
        if media:
            message["media"] = media
        changes = [record.change({**record.value, "lastMessageSeq": seq}), member.change(member.value, check=True),
                   Change("ROOM#" + room, f"MESSAGE#{seq:020d}", None, message)]
        if media:
            changes.append(Change("ROOM#" + room, "MEDIA_MESSAGE#" + media["mediaId"], None, {"messageSeq": seq}))
        if client_message_id:
            changes.append(Change("ROOM#" + room, f"CLIENT#{user}#{client_message_id}", None, {"messageSeq": seq}))
        self.db.commit(changes)
        return message

    def after(self, room, last_seq):
        lower = f"MESSAGE#{last_seq + 1:020d}"
        upper = "MESSAGE#" + chr(0x10ffff)
        return [record.value for record in self.db.query_range("ROOM#" + room, lower, upper)]

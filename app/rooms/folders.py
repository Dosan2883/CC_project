import json
import logging
from fastapi import HTTPException


class RoomFolders:
    def __init__(self, db, rooms, storage):
        self.db, self.rooms, self.storage = db, rooms, storage
        self.legacy_checked = False

    def ensure(self, room_id):
        try:
            room = self.rooms.get(room_id)
            self.storage.put(f"rooms/{room_id}/room.json", json.dumps({k:room[k] for k in ("roomId", "name", "createdAt")}, ensure_ascii=False).encode())
            self.db.update("ROOM_FOLDERS", room_id, lambda _: {"status":"COMPLETED"})
        except HTTPException as error:
            if error.status_code == 404:
                self.db.update("ROOM_FOLDERS", room_id, lambda _: {"status":"CANCELLED"})
        except Exception:
            logging.getLogger(__name__).exception("Room marker pending; recovery will retry")

    def recover(self):
        if not self.legacy_checked:
            for room in self.rooms.all():
                self.db.update("ROOM_FOLDERS", room["roomId"], lambda value:value or {"status":"PENDING"})
            self.legacy_checked = True
        for record in self.db.query("ROOM_FOLDERS"):
            if record.value["status"] == "PENDING":
                self.ensure(record.sk)

"""방 검색, 비밀번호·멤버 권한 확인과 응답 구성."""
import hmac
from app.auth.service import hash_password
from app.core.runtime import manager, room_repo

INTERNAL_FIELDS = {"key", "salt", "pwHash"}


def normalize(name):
    return " ".join(name.split())


def find_by_name(name):
    return room_repo.find_by_name(normalize(name).casefold())


def check_room_password(room, password):
    return hmac.compare_digest(hash_password(password, room["salt"]), room["pwHash"])


def room_view(room_id, user=None):
    room = room_repo.get(room_id)
    people = room_repo.members(room_id)
    member = next((item for item in people if item["user"] == user), None)
    return {**{key: value for key, value in room.items() if key not in INTERNAL_FIELDS},
            "memberCount": len(people), "online": len(manager.members(room_id)), "joined": bool(member),
            "role": member["role"] if member else None, "lastReadSeq": member["lastReadSeq"] if member else 0,
            "unreadCount": max(0, room["lastMessageSeq"] - member["lastReadSeq"]) if member else 0}


def get_room(room_id):
    return room_repo.get(room_id)


def newest_first(items):
    return sorted(items, key=lambda room: room["createdAt"], reverse=True)


def require_member(room_id, user):
    return room_repo.require_member(room_id, user)

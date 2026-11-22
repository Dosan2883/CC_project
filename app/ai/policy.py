"""Account consent and a shared daily cap for outbound AI calls."""
from datetime import datetime, timezone

from fastapi import HTTPException


class ExternalAIPolicy:
    def __init__(self, db, rooms, daily_limit, allow_private=False):
        self.db = db
        self.rooms = rooms
        self.daily_limit = daily_limit
        self.allow_private = allow_private

    def consent(self, user):
        row = self.db.get("USER#" + user.casefold(), "AI_CONSENT")
        return row.value if row else {"allowed": False, "updatedAt": None}

    def set_consent(self, user, allowed):
        now = datetime.now(timezone.utc)
        return self.db.update("USER#" + user.casefold(), "AI_CONSENT",
                              lambda _: {"allowed": allowed, "updatedAt": int(now.timestamp() * 1000)})

    def authorize(self, user, room):
        self.rooms.require_member(room, user)
        if not self.allow_private and self.rooms.record(room).value.get("visibility") != "public":
            raise HTTPException(403, "AI_PRIVATE_ROOM_DISABLED")
        if not self.consent(user)["allowed"]:
            raise HTTPException(403, "AI_CONSENT_REQUIRED")

    def reserve_call(self):
        day = datetime.now(timezone.utc).date().isoformat()

        def consume(value):
            calls = (value or {}).get("calls", 0)
            if calls >= self.daily_limit:
                raise HTTPException(429, "AI_DAILY_CALL_LIMIT")
            return {"calls": calls + 1}

        self.db.update("AI_SERVICE_BUDGET", day, consume)

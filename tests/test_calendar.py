from concurrent.futures import ThreadPoolExecutor
import tempfile
from threading import Barrier
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401  테스트용 임시 저장소 설정(실제 데이터를 건드리지 않는다)
from fastapi import HTTPException

from app.calendar import service as calendar_service
from app.calendar.service import CalendarService, PersonalCalendarService
from app.core.database import SQLiteDatabase
from app.rooms.repository import RoomRepository


class CalendarChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = SQLiteDatabase(Path(self.temp.name) / "calendar.db")
        self.rooms = RoomRepository(self.db)
        for room_id, owner in (("r", "owner"), ("s", "owner")):
            self.rooms.create({"roomId": room_id, "createdBy": owner, "createdAt": 0, "name": room_id, "key": room_id, "visibility": "public"})
        self.rooms.join("r", "guest")
        self.rooms.join("r", "other")
        self.calendar = CalendarService(self.db, self.rooms)

    def add(self, user="owner", **fields):
        body = {"title": "회의", "date": "2026-11-20", "time": "", "note": ""}
        body.update(fields)
        return self.calendar.create("r", user, body)

    def code(self, call, *args):
        with self.assertRaises(HTTPException) as error:
            call(*args)
        return error.exception.detail

    def test_create_and_list_sorted_by_date_then_time(self):
        self.add(title="나중", date="2026-11-21", time="10:00")
        self.add(title="종일", date="2026-11-20")
        self.add(title="오전", date="2026-11-20", time="09:30")
        titles = [event["title"] for event in self.calendar.list("r", "guest")]
        self.assertEqual(titles, ["종일", "오전", "나중"])  # 같은 날은 시간이 없는 일정(종일)이 먼저, 그다음 시각 순
        created = self.add(title="  앞뒤   공백  제거 ", date="2026-12-01", note=" 메모 ")
        self.assertEqual((created["title"], created["note"], created["createdBy"]), ("앞뒤 공백 제거", "메모", "owner"))

    def test_month_filter_and_isolation_between_rooms(self):
        self.add(date="2026-11-30")
        self.add(date="2026-12-01")
        self.calendar.create("s", "owner", {"title": "다른 방", "date": "2026-11-30"})
        self.assertEqual([event["date"] for event in self.calendar.list("r", "owner", "2026-11")], ["2026-11-30"])
        self.assertEqual(len(self.calendar.list("r", "owner")), 2)
        self.assertEqual([event["title"] for event in self.calendar.list("s", "owner")], ["다른 방"])  # 방마다 따로 보인다
        self.assertEqual(self.code(self.calendar.list, "r", "owner", "2026-13"), "INVALID_MONTH")

    def test_validation_rejects_bad_input(self):
        cases = {"INVALID_DATE": [{"date": "2026-02-30"}, {"date": "20261120"}, {"date": "2026-1-5"}, {"date": "1999-12-31"}, {"date": "2101-01-01"}, {"date": ""}],
                 "INVALID_TIME": [{"time": "25:00"}, {"time": "9:30"}, {"time": "10:60"}],
                 "TITLE_REQUIRED": [{"title": "   "}, {"title": ""}],
                 "TITLE_TOO_LONG": [{"title": "가" * 101}],
                 "NOTE_TOO_LONG": [{"note": "가" * 501}]}
        for expected, bodies in cases.items():
            for fields in bodies:
                with self.subTest(fields=fields):
                    with self.assertRaises(HTTPException) as error:
                        self.add(**fields)
                    self.assertEqual((error.exception.status_code, error.exception.detail), (422, expected))
        self.assertEqual(self.calendar.list("r", "owner"), [])  # 거절된 입력은 저장되지 않는다
        self.add(title="가" * 100, note="가" * 500, time="23:59", date="2100-12-31")  # 경계값은 허용

    def test_permissions(self):
        event = self.add("guest", title="내 일정")
        listed = {user: self.calendar.list("r", user)[0]["canEdit"] for user in ("guest", "owner", "other")}
        self.assertEqual(listed, {"guest": True, "owner": True, "other": False})  # 만든 사람과 방 주인만 고칠 수 있다
        self.assertEqual(self.code(self.calendar.update, "r", event["id"], "other", {"title": "가로채기"}), "NOT_EVENT_OWNER")
        self.assertEqual(self.code(self.calendar.delete, "r", event["id"], "other"), "NOT_EVENT_OWNER")
        self.assertEqual(self.calendar.update("r", event["id"], "owner", {"title": "방장이 수정"})["title"], "방장이 수정")
        with self.assertRaises(HTTPException) as error:
            self.calendar.list("r", "stranger")  # 방 구성원이 아니면 볼 수 없다
        self.assertEqual(error.exception.status_code, 403)
        with self.assertRaises(HTTPException):
            self.calendar.create("r", "stranger", {"title": "x", "date": "2026-11-20"})

    def test_partial_update_and_delete(self):
        event = self.add(title="원래", date="2026-11-20", time="09:00", note="메모")
        updated = self.calendar.update("r", event["id"], "owner", {"time": ""})
        self.assertEqual((updated["title"], updated["date"], updated["time"], updated["note"]), ("원래", "2026-11-20", "", "메모"))  # 보낸 칸만 바뀐다
        moved = self.calendar.update("r", event["id"], "owner", {"date": "2026-12-25"})
        self.assertEqual(moved["date"], "2026-12-25")
        self.assertEqual(self.code(self.calendar.update, "r", event["id"], "owner", {"date": "2026-02-31"}), "INVALID_DATE")
        self.assertEqual(self.calendar.list("r", "owner")[0]["date"], "2026-12-25")  # 잘못된 수정은 반영되지 않는다
        self.assertEqual(self.calendar.delete("r", event["id"], "owner"), {"id": event["id"], "deleted": True})
        self.assertEqual(self.calendar.list("r", "owner"), [])
        self.assertEqual(self.code(self.calendar.delete, "r", event["id"], "owner"), "EVENT_NOT_FOUND")
        self.assertEqual(self.code(self.calendar.update, "r", "missing", "owner", {"title": "x"}), "EVENT_NOT_FOUND")

    def test_event_belongs_to_its_room(self):
        event = self.add()
        self.assertEqual(self.code(self.calendar.delete, "s", event["id"], "owner"), "EVENT_NOT_FOUND")  # 다른 방 주소로는 지울 수 없다
        self.assertEqual(len(self.calendar.list("r", "owner")), 1)

    def test_event_limit_per_room(self):
        with patch.object(calendar_service, "MAX_EVENTS", 2):
            self.add(title="회의 1")
            self.add(title="회의 2")
            self.assertEqual(self.code(self.calendar.create, "r", "owner", {
                "title": "회의 3", "date": "2026-11-20",
            }), "TOO_MANY_EVENTS")

    def test_concurrent_duplicate_creation_reuses_one_event(self):
        barrier = Barrier(2)

        def create(user, title):
            barrier.wait(timeout=5)
            return self.add(user, title=title)

        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(create, "owner", "Sprint   review")
            second = pool.submit(create, "guest", " sprint review ")
            self.assertEqual(first.result(timeout=10)["id"], second.result(timeout=10)["id"])
        self.assertEqual(len(self.calendar.list("r", "owner")), 1)

    def test_concurrent_creates_compete_for_last_slot(self):
        barrier = Barrier(2)

        def add(title):
            barrier.wait(timeout=5)
            try:
                self.add(title=title)
                return "created"
            except HTTPException as error:
                return error.detail

        with patch.object(calendar_service, "MAX_EVENTS", 1), ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(add, ["회의 A", "회의 B"])), ["TOO_MANY_EVENTS", "created"])
        self.assertEqual(len(self.calendar.list("r", "owner")), 1)

    def test_deleted_room_cannot_gain_an_event_during_commit(self):
        commit = self.db.commit
        deleted = False

        def delete_before_commit(changes):
            nonlocal deleted
            if not deleted and any(change.sk.startswith("EVENT#") for change in changes):
                deleted = True
                self.rooms.delete("r", "owner")
            return commit(changes)

        with patch.object(self.db, "commit", side_effect=delete_before_commit):
            with self.assertRaises(HTTPException) as error:
                self.add()
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(self.db.query("CALENDAR#r", "EVENT#"), [])

    def test_departed_member_cannot_create_edit_or_delete_during_commit(self):
        event = self.add("guest")
        commit = self.db.commit
        operations = [
            lambda: self.add("guest", title="새 회의"),
            lambda: self.calendar.update("r", event["id"], "guest", {"title": "변경"}),
            lambda: self.calendar.delete("r", event["id"], "guest"),
        ]
        for operation in operations:
            self.rooms.join("r", "guest")
            left = False

            def leave_before_commit(changes):
                nonlocal left
                if not left and any(change.sk.startswith("EVENT#") for change in changes):
                    left = True
                    self.rooms.leave("r", "guest")
                return commit(changes)

            with patch.object(self.db, "commit", side_effect=leave_before_commit):
                with self.assertRaises(HTTPException) as error:
                    operation()
            self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.calendar.list("r", "owner")[0]["title"], event["title"])
        self.assertEqual(len(self.calendar.list("r", "owner")), 1)


class PersonalCalendarChecks(unittest.TestCase):
    """개인 캘린더: 사용자마다 따로 저장되고 남에게 보이지 않는다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = SQLiteDatabase(Path(self.temp.name) / "personal.db")
        self.calendar = PersonalCalendarService(self.db)

    def body(self, **fields):
        return {"title": "병원", "date": "2026-11-20", "time": "", "note": "", **fields}

    def code(self, call, *args):
        with self.assertRaises(HTTPException) as error:
            call(*args)
        return error.exception.detail

    def test_each_user_sees_only_own_events(self):
        self.calendar.create("alice", self.body(title="앨리스 일정"))
        self.calendar.create("bob", self.body(title="밥 일정"))
        self.assertEqual([e["title"] for e in self.calendar.list("alice")], ["앨리스 일정"])
        self.assertEqual([e["title"] for e in self.calendar.list("bob")], ["밥 일정"])
        self.assertEqual(self.calendar.list("carol"), [])

    def test_nickname_case_does_not_split_calendar(self):
        self.calendar.create("Alice", self.body())
        self.assertEqual(len(self.calendar.list("alice")), 1)

    def test_cannot_edit_or_delete_another_users_event(self):
        event = self.calendar.create("alice", self.body())
        self.assertEqual(self.code(self.calendar.update, event["id"], "bob", {"title": "해킹"}), "EVENT_NOT_FOUND")
        self.assertEqual(self.code(self.calendar.delete, event["id"], "bob"), "EVENT_NOT_FOUND")
        self.assertEqual(self.calendar.list("alice")[0]["title"], "병원")

    def test_update_delete_and_validation(self):
        event = self.calendar.create("alice", self.body())
        changed = self.calendar.update(event["id"], "alice", {"title": "치과", "time": "09:30"})
        self.assertEqual((changed["title"], changed["time"], changed["canEdit"]), ("치과", "09:30", True))
        self.assertEqual(self.code(self.calendar.update, event["id"], "alice", {"date": "2026-13-40"}), "INVALID_DATE")
        self.assertEqual(self.code(self.calendar.create, "alice", self.body(title="  ")), "TITLE_REQUIRED")
        self.calendar.delete(event["id"], "alice")
        self.assertEqual(self.calendar.list("alice"), [])

    def test_duplicate_returns_existing_and_sorted_by_date(self):
        first = self.calendar.create("alice", self.body(date="2026-11-22"))
        _, created = self.calendar.create_with_status("alice", self.body(date="2026-11-22"))
        self.assertFalse(created)
        self.calendar.create("alice", self.body(title="먼저", date="2026-11-21"))
        self.assertEqual([e["date"] for e in self.calendar.list("alice")], ["2026-11-21", "2026-11-22"])
        self.assertEqual(self.calendar.list("alice", "2026-12"), [])
        self.assertEqual(first["canEdit"], True)

    def test_room_tag_is_stored_cleared_and_validated(self):
        event = self.calendar.create("alice", self.body(roomId="r1", roomName="  버그   제보방 "))
        self.assertEqual((event["roomId"], event["roomName"]), ("r1", "버그 제보방"))
        moved = self.calendar.update(event["id"], "alice", {"roomId": "r2", "roomName": "디자인방"})
        self.assertEqual((moved["roomId"], moved["roomName"]), ("r2", "디자인방"))
        untouched = self.calendar.update(event["id"], "alice", {"title": "이름만 변경"})
        self.assertEqual(untouched["roomId"], "r2")  # 방 표시를 보내지 않으면 그대로 둔다
        cleared = self.calendar.update(event["id"], "alice", {"roomId": ""})
        self.assertEqual((cleared["roomId"], cleared["roomName"]), ("", ""))
        self.assertEqual(self.calendar.create("alice", self.body(title="태그 없음"))["roomId"], "")
        self.assertEqual(self.code(self.calendar.create, "alice", self.body(title="긴 방", roomId="r", roomName="가" * 41)), "INVALID_ROOM")


if __name__ == "__main__":
    unittest.main()

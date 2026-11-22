import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import support  # noqa: F401  테스트용 임시 저장소 설정(실제 데이터를 건드리지 않는다)
from fastapi import HTTPException

from app.ai.client import TranslationUnavailable
from app.calendar.suggest import DeadlineFinder, add_months, local_date, parse_items, resolve_when
from app.core.database import SQLiteDatabase
from app.media.service import MediaError

BASE = date(2026, 11, 18)  # 수요일


class ResolveChecks(unittest.TestCase):
    def check(self, spec, expected, base=BASE):
        self.assertEqual(resolve_when(spec, base), expected, spec)

    def test_weekday_this_next_upcoming(self):
        friday = lambda week: {"type": "weekday", "weekday": 4, "week": week}  # noqa: E731
        self.check(friday("this"), date(2026, 11, 20))
        self.check(friday("next"), date(2026, 11, 27))  # "다음 주 금요일"
        self.check(friday("upcoming"), date(2026, 11, 20))
        self.check({"type": "weekday", "weekday": 2, "week": "upcoming"}, date(2026, 11, 18))  # 같은 요일이면 오늘
        self.check({"type": "weekday", "weekday": 0, "week": "upcoming"}, date(2026, 11, 23))  # 이미 지난 요일이면 다음 주
        self.check({"type": "weekday", "weekday": 0, "week": "next"}, date(2026, 11, 23))
        self.check({"type": "weekday", "weekday": 6, "week": "this"}, date(2026, 11, 22))
        self.check({"type": "weekday", "weekday": 6, "week": "next"}, date(2026, 11, 29))
        self.check({"type": "weekday", "weekday": 4}, date(2026, 11, 20))  # week을 빼면 upcoming
        self.check({"type": "weekday", "weekday": 7, "week": "next"}, None)
        self.check({"type": "weekday", "weekday": 4, "week": "last"}, None)

    def test_relative_day_and_in(self):
        for offset, expected in ((0, date(2026, 11, 18)), (1, date(2026, 11, 19)), (2, date(2026, 11, 20)), (-1, date(2026, 11, 17))):
            self.check({"type": "relative_day", "offset": offset}, expected)
        self.check({"type": "relative_day", "offset": 400}, None)
        self.check({"type": "relative_day", "offset": -31}, None)
        self.check({"type": "in", "unit": "day", "n": 3}, date(2026, 11, 21))
        self.check({"type": "in", "unit": "week", "n": 2}, date(2026, 12, 2))
        self.check({"type": "in", "unit": "month", "n": 1}, date(2026, 12, 18))
        self.check({"type": "in", "unit": "month", "n": 3}, date(2027, 2, 18))
        self.check({"type": "in", "unit": "day", "n": -1}, None)
        self.check({"type": "in", "unit": "year", "n": 1}, None)

    def test_month_arithmetic_clamps_to_month_end(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2028, 1, 31), 1), date(2028, 2, 29))  # 윤년
        self.assertEqual(add_months(date(2026, 12, 31), 2), date(2027, 2, 28))
        self.assertEqual(add_months(date(2026, 11, 18), 0), date(2026, 11, 18))

    def test_absolute_dates_and_year_guess(self):
        self.check({"type": "date", "month": 11, "day": 27, "year": None}, date(2026, 11, 27))
        self.check({"type": "date", "month": 11, "day": 10, "year": None}, date(2026, 11, 10))  # 며칠 지난 날짜는 올해로 둔다
        self.check({"type": "date", "month": 1, "day": 5, "year": None}, date(2027, 1, 5))  # 한참 지난 달이면 내년
        self.check({"type": "date", "month": 10, "day": 10}, date(2027, 10, 10))
        self.check({"type": "date", "month": 3, "day": 1, "year": 2027}, date(2027, 3, 1))
        self.check({"type": "date", "month": 2, "day": 30, "year": None}, None)
        self.check({"type": "date", "month": 13, "day": 1}, None)
        self.check({"type": "date", "month": "11", "day": 5}, None)  # 숫자가 아니면 버린다
        self.check({"type": "date", "month": True, "day": 5}, None)
        self.check({"type": "date", "month": 11}, None)

    def test_month_end(self):
        self.check({"type": "month_end", "month_offset": 0}, date(2026, 11, 30))
        self.check({"type": "month_end", "month_offset": 1}, date(2026, 12, 31))
        self.check({"type": "month_end", "month_offset": 1}, date(2027, 1, 31), date(2026, 12, 15))
        self.check({"type": "month_end", "month_offset": 1}, date(2028, 2, 29), date(2028, 1, 31))
        self.check({"type": "month_end", "month_offset": 2}, None)

    def test_garbage_specs(self):
        for spec in (None, "금요일", 5, [], {}, {"type": "nope"}, {"type": "in"}, {"type": "weekday"}):
            self.check(spec, None)


class ParseChecks(unittest.TestCase):
    ITEM = {"source": 1, "title": "발표 자료 제출", "when": {"type": "weekday", "weekday": 4, "week": "next"}, "time": "15:00"}

    def test_reads_plain_fenced_and_chatty_replies(self):
        body = json.dumps([self.ITEM], ensure_ascii=False)
        for raw in (body, f"```json\n{body}\n```", f"```\n{body}\n```", f"알겠습니다.\n{body}\n도움이 되었길 바랍니다."):
            with self.subTest(raw=raw[:20]):
                self.assertEqual(len(parse_items(raw)), 1)

    def test_none_means_unreadable_and_empty_list_means_no_events(self):
        for raw in ("", None, "죄송합니다. 찾지 못했습니다.", "[not json]", '{"a": 1}', "]["):
            self.assertIsNone(parse_items(raw), raw)
        self.assertEqual(parse_items("[]"), [])
        self.assertEqual(parse_items('{"a":[1,2]}'), [])  # 배열은 있지만 일정 항목이 아니다

    def test_drops_bad_items_and_cleans_fields(self):
        items = [
            {**self.ITEM, "source": "1"}, {**self.ITEM, "source": True}, {**self.ITEM, "title": "   "}, "문자열", 3,
            {**self.ITEM, "title": "  줄   바꿈\n제목 ", "time": "25:00"},
            {**self.ITEM, "title": "가" * 300, "time": "9:30"},
        ]
        parsed = parse_items(json.dumps(items, ensure_ascii=False))
        self.assertEqual([(item["title"], item["time"]) for item in parsed], [("줄 바꿈 제목", ""), ("가" * 100, "")])
        many = parse_items(json.dumps([self.ITEM] * 50))
        self.assertEqual(len(many), 20)  # 한 번에 20개까지만 읽는다


class FakeJobs:
    def __init__(self, evidence, error=None):
        self.evidence, self.error, self.calls = evidence, error, []

    def select(self, room, sources, user):
        self.calls.append((room, sources, user))
        if self.error:
            raise self.error
        return self.evidence


class FakeQuota:
    def authorize_external(self, user, room):
        pass

    def __init__(self, error=None):
        self.error, self.reserved, self.attempted = error, 0, 0

    def reserve(self, user, cost=1):
        if self.error:
            raise self.error
        self.reserved += 1
        return ["r"]

    def mark_attempted(self, user, ids):
        self.attempted += 1


class FakeTranslator:
    def __init__(self, reply="[]", configured=True, fail=False):
        self.reply, self.configured, self.fail, self.calls = reply, configured, fail, []

    def invoke(self, text, target, action, source=None):
        self.calls.append((text, target, action))
        if self.fail:
            raise TranslationUnavailable("down")
        return self.reply


def ms(year, month, day, hour=12, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


class FinderChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = SQLiteDatabase(Path(self.temp.name) / "suggest.db")
        self.quota, self.sources = FakeQuota(), [{"kind": "message", "messageSeq": 1}]
        self.reply = json.dumps([{"source": 1, "title": "발표 자료 제출", "when": {"type": "weekday", "weekday": 4, "week": "next"}, "time": "15:00"}], ensure_ascii=False)

    def finder(self, evidence=None, reply=None, **kwargs):
        evidence = evidence if evidence is not None else [{"id": 1, "text": "다음 주 금요일 3시까지 발표 자료 제출", "sentAt": ms(2026, 11, 18)}]
        self.jobs = FakeJobs(evidence, kwargs.pop("jobs_error", None))
        self.translator = FakeTranslator(reply if reply is not None else self.reply, **kwargs)
        return DeadlineFinder(self.db, self.jobs, self.quota, self.translator)

    def code(self, call, *args, **kwargs):
        with self.assertRaises(HTTPException) as error:
            call(*args, **kwargs)
        return (error.exception.status_code, error.exception.detail)

    def test_dates_are_computed_from_each_messages_own_date(self):
        evidence = [{"id": 1, "text": "다음 주 금요일까지 제출", "sentAt": ms(2026, 11, 18)}, {"id": 2, "text": "다음 주 금요일까지 제출", "sentAt": ms(2026, 11, 25)}]
        reply = json.dumps([{"source": n, "title": f"제출 {n}", "when": {"type": "weekday", "weekday": 4, "week": "next"}, "time": ""} for n in (1, 2)], ensure_ascii=False)
        result = self.finder(evidence, reply).suggest("r", "owner", self.sources, 0, True)
        self.assertEqual([(item["title"], item["date"]) for item in result["suggestions"]], [("제출 1", "2026-11-27"), ("제출 2", "2026-12-04")])
        self.assertEqual(result["suggestions"][0]["evidence"], "다음 주 금요일까지 제출")
        self.assertEqual(self.translator.calls[0][1:], ("Original", "deadlines"))
        self.assertIn("[1] (2026-11-18) 다음 주 금요일까지 제출", self.translator.calls[0][0])  # AI에게 메시지 날짜를 알려 준다
        self.assertIn("[2] (2026-11-25)", self.translator.calls[0][0])

    def test_user_timezone_decides_the_message_date(self):
        evidence = [{"id": 1, "text": "내일까지", "sentAt": ms(2026, 11, 17, 23, 30)}]  # UTC로는 17일 밤, 한국에서는 18일 아침
        self.finder(evidence).suggest("r", "owner", self.sources, 540, True)
        self.assertIn("(2026-11-18)", self.translator.calls[0][0])
        self.finder(evidence).suggest("r", "owner", self.sources, 0, True)
        self.assertIn("(2026-11-17)", self.translator.calls[0][0])
        self.assertEqual(local_date(ms(2026, 11, 17, 23, 30), 540, datetime.now(timezone.utc)), date(2026, 11, 18))
        self.assertEqual(local_date(None, 0, datetime(2026, 5, 1, tzinfo=timezone.utc)), date(2026, 5, 1))  # 시각을 모르면 지금

    def test_same_selection_is_not_sent_to_the_ai_twice(self):
        finder = self.finder()
        first = finder.suggest("r", "owner", self.sources, 0, True)
        second = finder.suggest("r", "guest", self.sources, 0, True)
        self.assertEqual((first["cached"], second["cached"]), (False, True))
        self.assertEqual(first["suggestions"], second["suggestions"])
        self.assertEqual((len(self.translator.calls), self.quota.reserved), (1, 1))  # 호출 한도도 한 번만 쓴다
        finder.suggest("r", "owner", self.sources, 720, True)  # 시간대가 달라 메시지 날짜가 바뀌면 새로 묻는다
        self.assertEqual(len(self.translator.calls), 2)

    def test_requests_are_rejected_before_calling_the_ai(self):
        self.assertEqual(self.code(self.finder().suggest, "r", "owner", self.sources, 0, False), (422, "CONSENT_REQUIRED"))
        self.assertEqual(self.jobs.calls, [])  # 동의가 없으면 자료를 읽지도 않는다
        long_text = [{"id": 1, "text": "가" * 4000, "sentAt": ms(2026, 11, 18)}]
        self.assertEqual(self.code(self.finder(long_text).suggest, "r", "owner", self.sources, 0, True), (422, "SELECTION_TOO_LONG"))
        self.assertEqual(self.code(self.finder(configured=False).suggest, "r", "owner", self.sources, 0, True), (503, "TRANSLATION_NOT_CONFIGURED"))
        self.assertEqual(self.code(self.finder(jobs_error=MediaError(404, "MEDIA_NOT_FOUND")).suggest, "r", "owner", self.sources, 0, True), (404, "MEDIA_NOT_FOUND"))
        self.assertEqual(self.translator.calls, [])
        self.quota.error = HTTPException(429, "TRANSLATE_LIMIT")
        self.assertEqual(self.code(self.finder().suggest, "r", "owner", self.sources, 0, True), (429, "TRANSLATE_LIMIT"))
        self.assertEqual(self.translator.calls, [])  # 한도를 넘으면 AI를 부르지 않는다

    def test_failures_are_not_cached(self):
        self.assertEqual(self.code(self.finder(fail=True).suggest, "r", "owner", self.sources, 0, True), (502, "SUGGEST_FAILED"))
        self.assertEqual(self.code(self.finder(reply="죄송합니다, 찾지 못했습니다.").suggest, "r", "owner", self.sources, 0, True), (502, "SUGGEST_FAILED"))
        self.assertEqual(self.db.query("CALENDAR_SUGGEST"), [])  # 실패한 응답은 저장되지 않아 다시 누르면 새로 시도한다
        self.assertEqual(self.finder().suggest("r", "owner", self.sources, 0, True)["cached"], False)

    def test_empty_result_is_cached_as_no_events(self):
        finder = self.finder(reply="[]")
        self.assertEqual(finder.suggest("r", "owner", self.sources, 0, True), {"suggestions": [], "cached": False})
        self.assertEqual(finder.suggest("r", "owner", self.sources, 0, True), {"suggestions": [], "cached": True})
        self.assertEqual(len(self.translator.calls), 1)

    def test_bad_dates_unknown_sources_and_duplicates_are_dropped(self):
        spec = {"type": "date", "month": 11, "day": 30, "year": None}
        reply = json.dumps([
            {"source": 1, "title": "보고서", "when": spec, "time": ""},
            {"source": 1, "title": "보고서", "when": spec, "time": ""},             # 완전히 같은 후보
            {"source": 1, "title": "보고서", "when": spec, "time": "09:00"},        # 시간이 다르면 다른 후보
            {"source": 1, "title": "없는 날", "when": {"type": "date", "month": 2, "day": 30, "year": None}, "time": ""},
            {"source": 9, "title": "없는 자료", "when": spec, "time": ""},
            {"source": 1, "title": "범위 밖", "when": {"type": "date", "month": 1, "day": 1, "year": 2300}, "time": ""},
            {"source": 1, "title": "이해 못 함", "when": "곧", "time": ""},
        ], ensure_ascii=False)
        result = self.finder(reply=reply).suggest("r", "owner", self.sources, 0, True)
        self.assertEqual([(item["title"], item["date"], item["time"]) for item in result["suggestions"]],
                         [("보고서", "2026-11-30", ""), ("보고서", "2026-11-30", "09:00")])


class LambdaChecks(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "lambda/translation/handler.py"
        spec = importlib.util.spec_from_file_location("translation_lambda_deadlines", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module._secret = "s"
        self.module._bedrock = Mock()
        self.module._bedrock.converse.return_value = {"stopReason": "end_turn", "output": {"message": {"content": [{"text": "[]"}]}}}

    def call(self, payload):
        event = {"requestContext": {"http": {"method": "POST"}}, "headers": {"x-api-secret": "s"}, "body": json.dumps(payload)}
        return self.module.handler(event, None)

    def test_deadlines_action_is_accepted_and_uses_its_own_prompt(self):
        response = self.call({"text": "[1] (2026-11-18) 내일까지 제출", "target": "Original", "action": "deadlines"})
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual((body["translated"], body["finishReason"], body["truncated"], body["modelVersion"]), ("[]", "end_turn", False, "v1"))
        args = self.module._bedrock.converse.call_args.kwargs
        self.assertIn("Return ONLY a JSON array", args["system"][0]["text"])
        self.assertIn("do NOT compute the calendar date", args["system"][0]["text"])  # 날짜 계산은 AI에게 시키지 않는다
        user_text = args["messages"][0]["content"][0]["text"]
        self.assertTrue(user_text.startswith("Messages:\n[1] (2026-11-18)"))
        self.assertNotIn("Target language", user_text)

    def test_other_actions_keep_their_prompts_and_unknown_actions_are_rejected(self):
        self.call({"text": "hello", "target": "Korean"})
        self.assertIn("Translate the user's text", self.module._bedrock.converse.call_args.kwargs["system"][0]["text"])
        self.assertTrue(self.module._bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"].startswith("Target language: Korean"))
        self.call({"text": "[1] hello", "target": "Korean", "action": "summarize"})
        self.assertIn("Summarize only", self.module._bedrock.converse.call_args.kwargs["system"][0]["text"])
        self.assertEqual(self.call({"text": "x", "target": "Korean", "action": "delete-everything"})["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()

"""AI가 찾은 마감·일정 후보.

AI에게 날짜 계산을 맡기면 "다음 주 금요일" 같은 표현에서 틀리기 쉽다. 그래서 AI는 날짜 표현의 구조(예: 다음 주 + 금요일)만
JSON으로 돌려주고, 실제 날짜는 이 파일의 코드가 **그 글을 보낸 날짜를 기준으로** 계산한다. 같은 입력은 항상 같은 날짜가 나온다.
후보는 제안일 뿐이고, 일정으로 저장하는 것은 사용자가 확인한 뒤 일정 추가 API로 한다.
"""
import hashlib
import json
import re
import time
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

from app.ai.client import TranslationUnavailable
from app.calendar.service import TIME_PATTERN, clean_date
from app.media.service import MediaError

CACHE_VERSION = "deadlines-v1"
MAX_PROMPT_CHARS = 3900        # Lambda가 받는 글자 수 상한(4000)보다 조금 작게
MAX_ITEMS = 20
FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def add_months(day, months):
    """달을 더한다. 도착한 달에 그 날짜가 없으면 그 달의 마지막 날로 맞춘다(1월 31일 + 1달 = 2월 28일)."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def integer(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("integer expected")
    return value


def resolve_when(spec, base):
    """날짜 표현의 구조(spec)를 기준일(base)로 실제 날짜로 바꾼다. 계산할 수 없으면 None."""
    if not isinstance(spec, dict):
        return None
    kind = spec.get("type")
    try:
        if kind == "date":
            month, day = integer(spec["month"]), integer(spec["day"])
            year = spec.get("year")
            if year is not None:
                return date(integer(year), month, day)
            found = date(base.year, month, day)
            if found < base - timedelta(days=31):  # 한 달 넘게 지난 날짜로 읽히면 다음 해의 날짜로 본다
                found = date(base.year + 1, month, day)
            return found
        if kind == "relative_day":
            offset = integer(spec["offset"])
            return base + timedelta(days=offset) if -30 <= offset <= 366 else None
        if kind == "in":
            unit, count = spec["unit"], integer(spec["n"])
            if not 0 <= count <= 120:
                return None
            if unit == "day":
                return base + timedelta(days=count)
            if unit == "week":
                return base + timedelta(weeks=count)
            if unit == "month":
                return add_months(base, count)
            return None
        if kind == "weekday":
            weekday = integer(spec["weekday"])
            if not 0 <= weekday <= 6:
                return None
            week = spec.get("week", "upcoming")
            if week in ("this", "next"):
                monday = base - timedelta(days=base.weekday())  # 월요일에 시작하는 주
                return monday + timedelta(days=weekday + (7 if week == "next" else 0))
            if week == "upcoming":
                return base + timedelta(days=(weekday - base.weekday()) % 7)  # 오늘을 포함해 가장 가까운 그 요일
            return None
        if kind == "month_end":
            offset = integer(spec["month_offset"])
            if offset not in (0, 1):
                return None
            first = add_months(base.replace(day=1), offset)
            return first.replace(day=monthrange(first.year, first.month)[1])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return None


def parse_items(raw):
    """AI 응답에서 JSON 배열을 꺼낸다. 코드 블록이나 앞뒤 설명이 붙어 와도 읽고, 형식이 틀린 항목은 버린다.
    배열을 아예 읽을 수 없으면 None(= 실패)을 돌려준다. 읽은 결과가 비어 있으면 빈 목록([] = 일정 없음)이다."""
    text = FENCE.sub("", str(raw or "").strip())
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    items = []
    for item in data[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        if isinstance(source, bool) or not isinstance(source, int):
            continue
        title = " ".join(str(item.get("title") or "").split())[:100]
        if not title:
            continue
        clock = item.get("time")
        clock = clock if isinstance(clock, str) and TIME_PATTERN.match(clock) else ""
        items.append({"source": source, "title": title, "when": item.get("when"), "time": clock})
    return items


def local_date(sent_at_ms, tz_minutes, now):
    """보낸 시각(epoch 밀리초)을 사용자의 시간대 날짜로 바꾼다. 시각을 모르면 지금을 쓴다."""
    moment = datetime.fromtimestamp(sent_at_ms / 1000, timezone.utc) if isinstance(sent_at_ms, (int, float)) else now
    return (moment + timedelta(minutes=tz_minutes)).date()


class DeadlineFinder:
    def __init__(self, db, jobs, translation, translator):
        self.db, self.jobs, self.translation, self.translator = db, jobs, translation, translator

    def suggest(self, room_id, user, sources, tz_minutes=0, consent=False):
        if not consent:
            raise HTTPException(422, "CONSENT_REQUIRED")
        try:
            evidence = self.jobs.select(room_id, sources, user)  # 방 구성원 확인과 자료 읽기는 요약과 같은 규칙
        except MediaError as error:
            raise HTTPException(error.status, error.code) from None
        now = datetime.now(timezone.utc)
        bases, lines = {}, []
        for item in evidence:
            bases[item["id"]] = local_date(item.get("sentAt"), tz_minutes, now)
            lines.append(f"[{item['id']}] ({bases[item['id']].isoformat()}) {' '.join(item['text'].split())}")
        prompt = "\n".join(lines)
        if len(prompt) > MAX_PROMPT_CHARS:
            raise HTTPException(422, "SELECTION_TOO_LONG")
        key = hashlib.sha256((CACHE_VERSION + "\n" + prompt).encode()).hexdigest()
        cached = self.db.get("CALENDAR_SUGGEST", key)
        if cached:  # 같은 자료는 AI를 다시 부르지 않는다
            return {"suggestions": cached.value["items"], "cached": True}
        if not self.translator.configured:
            raise HTTPException(503, "TRANSLATION_NOT_CONFIGURED")
        reservation = self.translation.reserve(user)  # 번역·요약과 같은 사용자별 호출 한도를 쓴다
        try:
            self.translation.mark_attempted(user, reservation)
            raw = self.translator.invoke(prompt, "Original", "deadlines")
        except TranslationUnavailable:
            raise HTTPException(502, "SUGGEST_FAILED") from None
        parsed = parse_items(raw)
        if parsed is None:  # 읽을 수 없는 응답은 저장하지 않아서 다시 누르면 새로 시도한다
            raise HTTPException(502, "SUGGEST_FAILED")
        items = self.resolve(parsed, bases, evidence)
        self.db.update("CALENDAR_SUGGEST", key, lambda _: {"items": items, "createdAt": int(time.time() * 1000)})
        return {"suggestions": items, "cached": False}

    @staticmethod
    def resolve(items, bases, evidence):
        texts = {item["id"]: " ".join(item["text"].split()) for item in evidence}
        seen, resolved = set(), []
        for item in items:
            base = bases.get(item["source"])
            day = resolve_when(item["when"], base) if base else None
            if day is None:
                continue
            try:
                clean_date(day.isoformat())  # 2000~2100년 범위 밖이면 버린다
            except HTTPException:
                continue
            marker = (item["title"].casefold(), day.isoformat(), item["time"])
            if marker in seen:
                continue
            seen.add(marker)
            resolved.append({"title": item["title"], "date": day.isoformat(), "time": item["time"],
                             "source": item["source"], "evidence": texts[item["source"]][:120]})
        return resolved

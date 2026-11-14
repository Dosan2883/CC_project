"""채팅 메시지 번역: 권한 확인, 캐시, 사용자별 호출 한도.

서버가 메시지를 저장소에서 직접 읽어 번역한다. 클라이언트가 보낸 글자는 번역하지 않으므로
방 멤버가 아니면 쓸 수 없고, 이 API를 아무 글이나 번역하는 용도로 쓸 수도 없다.
"""
import hashlib
import re
import time
import uuid

from fastapi import HTTPException

from app.ai.contract import MODEL_VERSION
from app.media.service import MediaError

LANGUAGE_NAMES = {"ko": "Korean", "en": "English", "ja": "Japanese", "zh-CN": "Simplified Chinese", "es": "Spanish", "fr": "French"}
MAX_CHARS = 4000      # Lambda 쪽 입력 제한과 같은 값. 넘으면 잘린 번역이 나오므로 미리 거절한다
CHUNK_CHARS = 3500    # 문서 페이지를 나눌 때 한 번에 보내는 최대 길이 (MAX_CHARS보다 작게)
MAX_PAGE_CHARS = 12000  # 한 페이지 번역의 상한. 호출 횟수(=시간과 비용)를 묶어 둔다
QUOTA_WINDOW = 3600
CLAIM_LEASE = 180
CLAIM_WAIT = 30
RESERVATION_LEASE = 180
MAX_SEGMENTS = 300     # 한 음성에서 한 번에 번역하는 구간 수의 상한
NUMBERED_LINE = re.compile(r"^\s*\[(\d+)\]\s*(.*)$")
SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")


SCRIPTS = {"kana": re.compile("[぀-ヿ]"), "hangul": re.compile("[가-힣]"),
           "han": re.compile("[一-鿿]"), "latin": re.compile("[A-Za-zÀ-ÿ]")}


def text_matches_language(text, code):
    """글자 종류로 본 글이 설정한 원문 언어와 맞는지. 맞지 않으면(언어 설정이 실제 글과 다르면) 번역하지 않고 그대로 보여 준다.

    한국어·일본어·중국어는 글자로 구별하고, 영어·스페인어·프랑스어는 라틴 글자 비중으로만 본다(서로는 구별하지 못함)."""
    kana, hangul, han, latin = (len(SCRIPTS[name].findall(text)) for name in ("kana", "hangul", "han", "latin"))
    if code == "ko":
        return hangul > 0
    if code == "ja":
        return kana > 0 or (han > 0 and hangul == 0)
    if code == "zh-CN":
        return han > 0 and kana == 0 and hangul == 0
    if code in ("en", "es", "fr"):
        return latin > kana + hangul + han
    return True


def parse_numbered(text):
    """'[1] 문장' 줄들을 {번호: 문장}으로 바꾼다. 번호 없는 줄은 앞 항목의 이어진 줄로 본다."""
    parsed, current = {}, None
    for line in text.splitlines():
        match = NUMBERED_LINE.match(line)
        if match:
            current = int(match.group(1))
            parsed[current] = match.group(2).strip()
        elif current is not None and line.strip():
            parsed[current] = (parsed[current] + " " + line.strip()).strip()
    return parsed


def split_text(text, limit=CHUNK_CHARS):
    """번역 호출 한 번에 보낼 수 있는 길이로 나눈다.

    문단 경계를 우선하고, 한 문단이 너무 길면 문장 단위로, 그래도 길면 글자 수로 자른다.
    같은 문단에서 나온 문장끼리는 공백으로, 문단끼리는 빈 줄로 다시 붙여 모양을 최대한 지킨다.
    """
    pieces = []  # (글자, 앞에 붙일 구분자)
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        sentences = [paragraph] if len(paragraph) <= limit else SENTENCE_END.split(paragraph)
        for index, sentence in enumerate(sentences):
            separator = "\n\n" if index == 0 else " "
            while len(sentence) > limit:
                pieces.append((sentence[:limit], separator))
                sentence, separator = sentence[limit:], " "
            if sentence:
                pieces.append((sentence, separator))
    chunks, current = [], ""
    for piece, separator in pieces:
        if current and len(current) + len(separator) + len(piece) > limit:
            chunks.append(current)
            current = piece
        else:
            current = current + separator + piece if current else piece
    if current:
        chunks.append(current)
    return chunks


class TranslationService:
    def __init__(self, db, rooms, translator, hourly_limit=60, media=None):
        self.db, self.rooms, self.translator, self.hourly_limit, self.media = db, rooms, translator, hourly_limit, media

    def translate_message(self, room_id, seq, target, user):
        if target not in LANGUAGE_NAMES:
            raise HTTPException(422, "INVALID_LANGUAGE")
        self.rooms.require_member(room_id, user)
        record = self.db.get("ROOM#" + room_id, f"MESSAGE#{seq:020d}")
        if not record:
            raise HTTPException(404, "MESSAGE_NOT_FOUND")
        message = record.value
        def verify():
            self.rooms.require_member(room_id, user)
            current = self.db.get("ROOM#" + room_id, f"MESSAGE#{seq:020d}")
            if not current or current.version != record.version:
                raise HTTPException(409, "SOURCE_VERSION_CHANGED")
        if message.get("media"):
            raise HTTPException(422, "NOT_TEXT_MESSAGE")
        text = message["bodyOriginal"]
        if len(text) > MAX_CHARS:
            raise HTTPException(422, "TEXT_TOO_LONG")
        if message.get("sourceLanguage") == target:  # 이미 같은 언어면 호출하지 않는다
            verify()
            return {"translated": text, "target": target, "cached": False, "sameLanguage": True}

        source = self.source_language(message.get("sourceLanguage"))
        if source and not text_matches_language(text, source):  # 설정한 원문 언어와 실제 글이 다르면 그대로 둔다
            verify()
            return {"translated": text, "target": target, "cached": False, "sameLanguage": True, "languageMismatch": True}
        translated, cached = self.translate_text(text, target, user, verify, source)
        verify()
        return {"translated": translated, "target": target, "cached": cached, "sameLanguage": False}

    def translate_segments(self, media_id, target, user):
        """음성 녹취의 모든 구간을 번역한다. 구간 번호를 붙여 묶음으로 보내고(호출 수를 줄임), 번호가 어긋난 묶음은 구간마다 따로 다시 번역한다."""
        if target not in LANGUAGE_NAMES:
            raise HTTPException(422, "INVALID_LANGUAGE")
        try:
            asset = self.media.live_asset(media_id)
            self.rooms.require_member(asset["roomId"], user)
        except (MediaError, HTTPException):
            raise HTTPException(404, "MEDIA_NOT_FOUND")
        try:
            transcript = self.media.transcript(media_id)
        except MediaError as error:
            raise HTTPException(error.status, error.code)
        segments = transcript.get("segments") or []
        if not segments:
            raise HTTPException(422, "NO_SEGMENTS")
        if len(segments) > MAX_SEGMENTS or sum(len(s["text"]) for s in segments) > MAX_PAGE_CHARS:
            raise HTTPException(422, "TRANSCRIPT_TOO_LONG")
        source = self.source_language(transcript.get("language"))

        def verify():
            try:
                self.rooms.require_member(asset["roomId"], user)
                self.media.live_asset(media_id)
            except (MediaError, HTTPException):
                raise HTTPException(404, "MEDIA_NOT_FOUND")

        def result(values):
            return {"target": target, "segments": [{"start": s["start"], "end": s["end"], "text": s["text"], "translated": value}
                                                   for s, value in zip(segments, values)]}

        if source == target:  # 이미 같은 언어면 호출하지 않는다
            return result([s["text"] for s in segments])
        batches, current, size = [], [], 0
        for index, segment in enumerate(segments):
            line = f"[{index + 1}] {segment['text']}"
            if current and size + len(line) + 1 > CHUNK_CHARS:
                batches.append(current)
                current, size = [], 0
            current.append((index, line))
            size += len(line) + 1
        batches.append(current)
        values = [None] * len(segments)
        for batch in batches:
            output, _ = self.translate_text("\n".join(line for _, line in batch), target, user, verify, source)
            parsed = parse_numbered(output)
            if all(parsed.get(index + 1) for index, _ in batch):
                for index, _ in batch:
                    values[index] = parsed[index + 1]
                continue
            # 번호가 하나라도 빠지면 줄이 이웃 구간에 붙어 어긋났을 수 있으므로, 그 묶음은 구간마다 따로 번역한다
            for index, _ in batch:
                values[index], _ = self.translate_text(segments[index]["text"], target, user, verify, source)
        verify()
        return result(values)

    def translate_page(self, media_id, page, target, user):
        """업로드한 문서의 한 페이지를 번역한다. 서버가 저장된 추출 글자를 직접 읽는다."""
        if target not in LANGUAGE_NAMES:
            raise HTTPException(422, "INVALID_LANGUAGE")
        try:
            asset = self.media.live_asset(media_id)
            self.rooms.require_member(asset["roomId"], user)
        except (MediaError, HTTPException):
            raise HTTPException(404, "MEDIA_NOT_FOUND")  # 방 밖 사용자에게는 파일의 존재도 알리지 않는다
        try:
            transcript = self.media.transcript(media_id)
            pages = transcript["pages"]
        except MediaError as error:
            raise HTTPException(error.status, error.code)
        if not 1 <= page <= len(pages):
            raise HTTPException(422, "INVALID_PAGE")
        text = pages[page - 1].strip()
        if not text:
            raise HTTPException(422, "EMPTY_PAGE")
        if len(text) > MAX_PAGE_CHARS:
            raise HTTPException(422, "PAGE_TOO_LONG")
        detail = transcript.get("pageDetails") or []
        detail = detail[page - 1] if len(detail) >= page and isinstance(detail[page - 1], dict) else {}
        source = self.source_language((transcript.get("corrections") or {}).get(str(page), {}).get("language") or detail.get("language"))
        def checked(result):
            verify()
            return result
        def verify():
            try:
                current_asset = self.media.live_asset(media_id)
                self.rooms.require_member(current_asset["roomId"], user)
                current = self.media.transcript(media_id)
            except MediaError as error:
                raise HTTPException(error.status, error.code)
            if (current_asset.get("transcriptKey") != asset.get("transcriptKey") or
                current.get("version", 1) != transcript.get("version", 1) or current.get("pages") != pages):
                raise HTTPException(409, "SOURCE_VERSION_CHANGED")

        if source and not text_matches_language(text, source):  # 설정한 원문 언어와 실제 글이 다르면 번역하지 않는다
            return checked({"translated": text, "target": target, "page": page, "cached": False, "languageMismatch": True})
        key, cached = self.cached(target, text, source)
        if cached:
            return checked({"translated": cached, "target": target, "page": page, "cached": True})
        chunks = split_text(text)
        if len(chunks) == 1:
            translated, was_cached = self.translate_text(text, target, user, verify, source)
            return checked({"translated": translated, "target": target, "page": page, "cached": was_cached})
        page_key = "TRANSLATION#" + key[:2]
        lease, saved = self.acquire(page_key, key)
        if saved is not None:
            return checked({"translated": saved, "target": target, "page": page, "cached": True})
        reservation_ids = []
        try:
            chunk_records = [self.cached(target, chunk, source) for chunk in chunks]
            missing = [index for index, (_, value) in enumerate(chunk_records) if value is None]
            reservation_ids = self.reserve(user, len(missing)) if missing else []
            reservation_by_index = dict(zip(missing, reservation_ids))
            output = []
            for index, (chunk_key, value) in enumerate(chunk_records):
                verify()
                if value is None:
                    reservation_id = reservation_by_index[index]
                    chunk_pk = "TRANSLATION#" + chunk_key[:2]
                    chunk_lease, value = self.acquire(chunk_pk, chunk_key)
                    if value is not None:
                        self.release(user, [reservation_id])
                    else:
                        self.mark_attempted(user, [reservation_id])
                        later = [reservation_by_index[i] for i in missing if i > index]
                        try:
                            verify()
                            value = self.call_translator(chunks[index], target, source)
                            value = self.complete(chunk_pk, chunk_key, target, value, chunk_lease)
                        except Exception:
                            self.release(user, later)
                            self.release_claim(chunk_pk, chunk_key, chunk_lease)
                            raise
                output.append(value)
            translated = "\n\n".join(output)
            verify()
            return checked({"translated": self.complete(page_key, key, target, translated, lease), "target": target, "page": page, "cached": False})
        except Exception:
            self.release(user, reservation_ids)
            self.release_claim(page_key, key, lease)
            raise

    @staticmethod
    def source_language(code):
        """원문 언어 코드. 모르거나 지원하지 않는 값이면 None(= 전체 번역)."""
        return code if code in LANGUAGE_NAMES else None

    def call_translator(self, text, target, source=None):
        # 원문 언어를 아는 경우에만 Lambda에 알려, 그 언어 부분만 번역하게 한다.
        if source:
            return self.translator.translate(text, LANGUAGE_NAMES[target], LANGUAGE_NAMES[source])
        return self.translator.translate(text, LANGUAGE_NAMES[target])

    @staticmethod
    def cache_key(target, text, source=None):
        # source가 없으면 예전 캐시 키와 같다.
        suffix = f"\nsource:{source}" if source else ""
        return hashlib.sha256(f"{MODEL_VERSION}\n{target}\n{text}{suffix}".encode()).hexdigest()

    def cached(self, target, text, source=None):
        key = self.cache_key(target, text, source)
        record = self.db.get("TRANSLATION#" + key[:2], key)
        value = record.value if record else None
        # 이전 번역 캐시에는 state 필드가 없었다. translated가 있으면 완료로 간주한다.
        return key, (value["translated"] if value and isinstance(value.get("translated"), str) and value["translated"].strip() else None)

    def remember(self, key, target, translated):
        saved = self.db.update("TRANSLATION#" + key[:2], key, lambda value: value if value and value.get("translated") else {"state": "COMPLETED", "translated": translated, "target": target, "modelVersion": MODEL_VERSION, "createdAt": int(time.time() * 1000)})
        return saved["translated"]

    def acquire(self, pk, key):
        """캐시 키를 DB 조건부 업데이트로 선점해 다중 프로세스의 중복 호출을 막는다."""
        token = uuid.uuid4().hex
        now = time.time()
        value = self.db.update(pk, key, lambda current: (
            current if current and current.get("translated") else
            current if current and current.get("state") == "IN_PROGRESS" and current.get("leaseUntil", 0) > now else
            {"state": "IN_PROGRESS", "leaseToken": token, "leaseUntil": now + CLAIM_LEASE, "modelVersion": MODEL_VERSION}
        ))
        if value.get("translated"):
            return None, value["translated"]
        if value.get("leaseToken") == token:
            return token, None

        deadline = time.monotonic() + CLAIM_WAIT
        while time.monotonic() < deadline:
            time.sleep(0.05)
            record = self.db.get(pk, key)
            current = record.value if record else None
            if current and current.get("translated"):
                return None, current["translated"]
            if not current or current.get("leaseUntil", 0) <= time.time():
                return self.acquire(pk, key)
        raise HTTPException(503, "TRANSLATION_IN_PROGRESS")

    def complete(self, pk, key, target, translated, token):
        def finish(current):
            if current and current.get("translated"):
                return current
            if not current or current.get("leaseToken") != token:
                raise HTTPException(503, "TRANSLATION_LEASE_LOST")
            return {"state": "COMPLETED", "translated": translated, "target": target,
                    "modelVersion": MODEL_VERSION, "createdAt": int(time.time() * 1000)}
        return self.db.update(pk, key, finish)["translated"]

    def release_claim(self, pk, key, token):
        def release(value):
            if value and value.get("state") == "IN_PROGRESS" and value.get("leaseToken") == token:
                return {"state": "FAILED", "leaseUntil": 0, "modelVersion": MODEL_VERSION}
            return value
        self.db.update(pk, key, release)

    def translate_text(self, text, target, user, verify=None, source=None):
        verify = verify or (lambda: None)
        key, cached = self.cached(target, text, source)
        if cached:
            verify()
            return cached, True
        pk = "TRANSLATION#" + key[:2]
        lease, saved = self.acquire(pk, key)
        if saved is not None:
            verify()
            return saved, True
        reservation = None
        try:
            verify()
            reservation = self.reserve(user, 1)[0]
            self.mark_attempted(user, [reservation])
            translated = self.call_translator(text, target, source)
            verify()
            return self.complete(pk, key, target, translated, lease), False
        except Exception:
            if reservation:
                self.release(user, [reservation])
            self.release_claim(pk, key, lease)
            raise

    def reserve(self, user, cost=1):
        """전체 작업의 한도를 예약한다. 호출을 시작한 조각만 실제 사용량으로 확정한다."""
        if cost <= 0:
            return []
        now = time.time()
        ids = [uuid.uuid4().hex for _ in range(cost)]

        def transform(value):
            value = value or {}
            legacy = [stamp for stamp in value.get("calls", []) if stamp > now - QUOTA_WINDOW]
            entries = [entry for entry in value.get("entries", []) if
                       (entry.get("state") == "reserved" and entry.get("expiresAt", 0) > now) or
                       (entry.get("state") == "attempted" and entry.get("createdAt", 0) > now - QUOTA_WINDOW)]
            if len(legacy) + len(entries) + cost > self.hourly_limit:
                raise HTTPException(429, "TRANSLATE_LIMIT")
            entries.extend({"id": item, "state": "reserved", "createdAt": now, "expiresAt": now + RESERVATION_LEASE} for item in ids)
            return {"calls": legacy, "entries": entries}

        self.db.update("TRANSLATE_QUOTA", user.casefold(), transform)
        return ids

    def mark_attempted(self, user, reservation_ids):
        if not reservation_ids:
            return
        wanted, now = set(reservation_ids), time.time()
        def transform(value):
            value = value or {"calls": [], "entries": []}
            entries = []
            for entry in value.get("entries", []):
                if entry.get("id") in wanted and entry.get("state") == "reserved":
                    entry = {**entry, "state": "attempted", "createdAt": now}
                entries.append(entry)
            return {**value, "entries": entries}
        self.db.update("TRANSLATE_QUOTA", user.casefold(), transform)

    def release(self, user, reservation_ids):
        """시작하지 않은 호출의 예약만 반환한다. 시작한 호출은 환급하지 않는다."""
        if not reservation_ids:
            return
        wanted = set(reservation_ids)
        def transform(value):
            value = value or {"calls": [], "entries": []}
            return {**value, "entries": [entry for entry in value.get("entries", [])
                                           if not (entry.get("id") in wanted and entry.get("state") == "reserved")]}
        self.db.update("TRANSLATE_QUOTA", user.casefold(), transform)

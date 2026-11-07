"""채팅 메시지 번역: 권한 확인, 캐시, 사용자별 호출 한도.

서버가 메시지를 저장소에서 직접 읽어 번역한다. 클라이언트가 보낸 글자는 번역하지 않으므로
방 멤버가 아니면 쓸 수 없고, 이 API를 아무 글이나 번역하는 용도로 쓸 수도 없다.
"""
import hashlib
import re
import time

from fastapi import HTTPException

from app.media.service import MediaError

LANGUAGE_NAMES = {"ko": "Korean", "en": "English", "ja": "Japanese", "zh-CN": "Simplified Chinese", "es": "Spanish", "fr": "French"}
MODEL_VERSION = "v1"  # 번역 모델이나 프롬프트를 바꾸면 올려서 이전 캐시를 쓰지 않게 한다
MAX_CHARS = 4000      # Lambda 쪽 입력 제한과 같은 값. 넘으면 잘린 번역이 나오므로 미리 거절한다
CHUNK_CHARS = 3500    # 문서 페이지를 나눌 때 한 번에 보내는 최대 길이 (MAX_CHARS보다 작게)
MAX_PAGE_CHARS = 12000  # 한 페이지 번역의 상한. 호출 횟수(=시간과 비용)를 묶어 둔다
QUOTA_WINDOW = 3600
SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")


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
        if message.get("media"):
            raise HTTPException(422, "NOT_TEXT_MESSAGE")
        text = message["bodyOriginal"]
        if len(text) > MAX_CHARS:
            raise HTTPException(422, "TEXT_TOO_LONG")
        if message.get("sourceLanguage") == target:  # 이미 같은 언어면 호출하지 않는다
            return {"translated": text, "target": target, "cached": False, "sameLanguage": True}

        key, cached = self.cached(target, text)
        if cached:  # 캐시를 쓰면 외부 호출도 한도 소모도 없다
            return {"translated": cached, "target": target, "cached": True, "sameLanguage": False}

        self.reserve(user)
        translated = self.translator.translate(text, LANGUAGE_NAMES[target])
        saved = self.remember(key, target, translated)
        return {"translated": saved, "target": target, "cached": False, "sameLanguage": False}

    def translate_page(self, media_id, page, target, user):
        """업로드한 문서의 한 페이지를 번역한다. 서버가 저장된 추출 글자를 직접 읽는다."""
        if target not in LANGUAGE_NAMES:
            raise HTTPException(422, "INVALID_LANGUAGE")
        try:
            asset = self.media.asset(media_id)
            self.rooms.require_member(asset["roomId"], user)
        except (MediaError, HTTPException):
            raise HTTPException(404, "MEDIA_NOT_FOUND")  # 방 밖 사용자에게는 파일의 존재도 알리지 않는다
        try:
            pages = self.media.transcript(media_id)["pages"]
        except MediaError as error:
            raise HTTPException(error.status, error.code)
        if not 1 <= page <= len(pages):
            raise HTTPException(422, "INVALID_PAGE")
        text = pages[page - 1].strip()
        if not text:
            raise HTTPException(422, "EMPTY_PAGE")
        if len(text) > MAX_PAGE_CHARS:
            raise HTTPException(422, "PAGE_TOO_LONG")

        key, cached = self.cached(target, text)
        if cached:
            return {"translated": cached, "target": target, "page": page, "cached": True}
        chunks = split_text(text)
        self.reserve(user, len(chunks))  # 나뉜 조각 수만큼 호출 한도를 쓴다
        translated = "\n\n".join(self.translator.translate(chunk, LANGUAGE_NAMES[target]) for chunk in chunks)
        return {"translated": self.remember(key, target, translated), "target": target, "page": page, "cached": False}

    @staticmethod
    def cache_key(target, text):
        return hashlib.sha256(f"{MODEL_VERSION}\n{target}\n{text}".encode()).hexdigest()

    def cached(self, target, text):
        key = self.cache_key(target, text)
        record = self.db.get("TRANSLATION#" + key[:2], key)
        return key, (record.value["translated"] if record else None)

    def remember(self, key, target, translated):
        saved = self.db.update("TRANSLATION#" + key[:2], key, lambda value: value or {"translated": translated, "target": target, "createdAt": int(time.time() * 1000)})
        return saved["translated"]

    def reserve(self, user, cost=1):
        """호출 전에 한도를 먼저 차감한다. 한도를 넘으면 외부를 부르지 않는다."""
        now = time.time()

        def transform(value):
            calls = [stamp for stamp in (value or {}).get("calls", []) if stamp > now - QUOTA_WINDOW]
            if len(calls) + cost > self.hourly_limit:
                raise HTTPException(429, "TRANSLATE_LIMIT")
            return {"calls": calls + [now] * cost}

        self.db.update("TRANSLATE_QUOTA", user.casefold(), transform)

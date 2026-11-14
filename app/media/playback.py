"""음성 재생 주소(임시 토큰)와 부분 전송(Range) 처리.

<audio> 태그는 요청에 로그인 토큰을 실을 수 없어서, 짧은 시간만 쓸 수 있고 한 파일에만 유효한 서명 토큰을 주소에 붙인다.
토큰에는 사용자 이름이 들어 있어 재생 요청 때 방 멤버인지 다시 확인한다. 서버의 서명 키를 쓰므로 토큰은 위조할 수 없다.
"""
import base64
import hashlib
import hmac
import json
import re
import time

PLAY_TTL = 3600
CONTENT_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "ogg": "audio/ogg", "flac": "audio/flac",
                 "mp4": "video/mp4", "webm": "video/webm"}
RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _sign(secret, payload):
    return hmac.new(secret.encode(), b"play:" + payload, hashlib.sha256).hexdigest()


def make_token(secret, media_id, user, ttl=PLAY_TTL, now=None):
    payload = base64.urlsafe_b64encode(json.dumps({"m": media_id, "u": user, "e": int((now or time.time()) + ttl)}).encode())
    return payload.decode() + "." + _sign(secret, payload)


def read_token(secret, token, media_id, now=None):
    """토큰이 유효하면 사용자 이름을, 아니면 None을 돌려준다. 다른 파일용이거나 만료된 토큰도 거부한다."""
    try:
        payload, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, _sign(secret, payload.encode())):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        if data["m"] != media_id or data["e"] < (now or time.time()):
            return None
        return data["u"]
    except (ValueError, KeyError, TypeError):
        return None


def parse_range(header, size):
    """Range 헤더를 (시작, 끝)으로 바꾼다. 헤더가 없으면 전체, 범위가 잘못되면 None."""
    if not header:
        return 0, size - 1
    match = RANGE.match(header.strip())
    if not match or size <= 0:
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":  # 끝에서 N바이트
        length = int(last)
        if length == 0:
            return None
        return max(0, size - length), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)

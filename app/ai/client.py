"""번역 호출부. 지금은 AWS Lambda 함수 URL을 부른다.

다른 서비스(Bedrock 직접 호출, Gemini 등)로 바꾸려면 translate()를 같은 모양으로 구현한
클래스로 교체하면 된다. 작업 등록·캐시·호출 한도는 service.py에 있어서 바뀌지 않는다.

성공 응답은 translated, modelVersion="v1", finishReason(정상 종료), truncated=false를
포함해야 한다. 부분 응답이나 계약이 맞지 않는 응답은 캐시 전에 거부한다.
"""
import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

from app.ai.contract import MODEL_VERSION

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_RESPONSE_BYTES = 64 * 1024


class TranslationUnavailable(Exception):
    """번역 서비스를 부르지 못했거나, 쓸 수 없는 응답이 왔다."""


class LambdaTranslator:
    def __init__(self, url="", secret="", timeout=25):
        self.url, self.secret, self.timeout = url.strip(), secret, timeout
        if self.url:
            parsed = urlparse(self.url)
            # https만 허용한다. 비밀 헤더가 암호화 없이 나가거나, file:// 같은 주소를 여는 일을 막는다.
            # 테스트용으로 로컬 주소의 http만 예외로 둔다.
            if not (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS)):
                raise RuntimeError("TRANSLATE_URL must be an https URL")

    @property
    def configured(self):
        return bool(self.url and self.secret)

    def translate(self, text, target_name):
        if not self.configured:
            raise TranslationUnavailable("not configured")
        request = urllib.request.Request(
            self.url,
            data=json.dumps({"text": text, "target": target_name}).encode(),
            headers={"Content-Type": "application/json", "x-api-secret": self.secret},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise TranslationUnavailable("response too large")
                data = json.loads(raw)
        except urllib.error.HTTPError as error:
            error.close()  # 오류 응답도 연결을 닫아야 누수되지 않는다
            raise TranslationUnavailable(f"HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            # 비밀 값이 들어 있을 수 있는 요청 내용은 로그나 예외 문구에 남기지 않는다.
            raise TranslationUnavailable(type(error).__name__) from error
        translated = data.get("translated") if isinstance(data, dict) else None
        if not isinstance(translated, str) or not translated.strip():
            raise TranslationUnavailable("empty response")
        truncated = data.get("truncated")
        finish_reason = data.get("finishReason", data.get("finish_reason", data.get("stopReason")))
        if truncated is not False or not isinstance(finish_reason, str):
            raise TranslationUnavailable("response is missing completion metadata")
        if finish_reason.casefold() not in {
            "stop", "stop_sequence", "end_turn", "completed", "complete", "finished",
        }:
            raise TranslationUnavailable("incomplete translation")
        model_version = data.get("modelVersion")
        if not isinstance(model_version, str) or model_version != MODEL_VERSION:
            raise TranslationUnavailable("response model version does not match the cache contract")
        return translated.strip()

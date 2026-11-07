"""번역 호출부. 지금은 AWS Lambda 함수 URL을 부른다.

다른 서비스(Bedrock 직접 호출, Gemini 등)로 바꾸려면 translate()를 같은 모양으로 구현한
클래스로 교체하면 된다. 작업 등록·캐시·호출 한도는 service.py에 있어서 바뀌지 않는다.
"""
import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

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
                data = json.loads(response.read(MAX_RESPONSE_BYTES))
        except urllib.error.HTTPError as error:
            error.close()  # 오류 응답도 연결을 닫아야 누수되지 않는다
            raise TranslationUnavailable(f"HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            # 비밀 값이 들어 있을 수 있는 요청 내용은 로그나 예외 문구에 남기지 않는다.
            raise TranslationUnavailable(type(error).__name__) from error
        translated = data.get("translated") if isinstance(data, dict) else None
        if not isinstance(translated, str) or not translated.strip():
            raise TranslationUnavailable("empty response")
        return translated.strip()

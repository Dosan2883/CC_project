"""음성·영상의 말을 글자로 바꾼다(Whisper 모델을 faster-whisper로 서버 안에서 직접 실행).

AWS 서비스나 외부 API를 부르지 않는다. 모델 파일은 처음 한 번 내려받아 서버에 두고 쓴다(scripts/prepare_stt.py).
결과는 앱의 녹취 형식 segments [{start, end, text}]로 돌려주고, 이후 번역·요약은 기존 흐름을 그대로 쓴다.

STT_BACKEND
  whisper : 실제 인식(faster-whisper 필요: requirements-stt.txt)
  fake    : 모델 없이 정해진 결과를 돌려준다(로컬 개발·화면 확인용)
  (비움)  : 음성·영상 처리를 하지 않는다(STT_NOT_CONFIGURED로 실패)
"""
import logging
import os
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# 앱 언어 → Whisper 언어 코드
LANGUAGE_CODES = {"ko": "ko", "en": "en", "ja": "ja", "zh-CN": "zh", "es": "es", "fr": "fr"}
AUDIO_EXTENSIONS = {"mp3", "m4a", "wav", "ogg", "flac"}
VIDEO_EXTENSIONS = {"mp4", "webm"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

# 한 번에 하나만 인식한다. 서버 CPU와 메모리가 작아서 동시에 돌리면 채팅까지 느려진다.
GATE = threading.Semaphore(int(os.getenv("STT_CONCURRENCY", "1")))


class SpeechError(Exception):
    """인식을 할 수 없다. code는 화면에 보여 줄 수 있는 짧은 이름이다(다시 시도해도 같은 결과)."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def language_from_whisper(code):
    for language, value in LANGUAGE_CODES.items():
        if value == code:
            return language
    return code or None


def segments_from_whisper(raw_segments, max_no_speech=0.8, min_logprob=-1.0):
    """faster-whisper 구간(start, end, text …)을 앱 녹취 형식으로 바꾼다. 소리가 없는데 지어낸 말로 보이는 구간은 버린다."""
    segments = []
    for raw in raw_segments:
        text = (getattr(raw, "text", "") or "").strip()
        if not text:
            continue
        if getattr(raw, "no_speech_prob", 0) > max_no_speech and getattr(raw, "avg_logprob", 0) < min_logprob:
            continue  # 무음·배경음에서 나오는 환각
        start, end = float(raw.start), float(raw.end)
        segments.append({"start": round(start, 2), "end": round(max(end, start), 2), "text": text})
    return segments


class WhisperTranscriber:
    """faster-whisper 모델을 처음 쓸 때 한 번만 올려 두고 재사용한다. model을 넘기면 그것을 쓴다(테스트용)."""

    def __init__(self, model_size="base", model_dir=None, compute_type="int8", threads=0, max_seconds=600, beam_size=1, model=None):
        self.model_size, self.model_dir, self.compute_type = model_size, model_dir, compute_type
        self.threads, self.max_seconds, self.beam_size = threads, max_seconds, beam_size
        self._model, self._lock = model, threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as error:
                    raise SpeechError("STT_NOT_INSTALLED") from error
                try:
                    self._model = WhisperModel(self.model_size, device="cpu", compute_type=self.compute_type,
                                               cpu_threads=self.threads, download_root=self.model_dir)
                except Exception as error:  # 모델을 받지 못했거나 메모리가 부족한 경우 등
                    raise SpeechError("STT_MODEL_UNAVAILABLE", type(error).__name__) from error
            return self._model

    def transcribe_file(self, path, language=None, progress=None):
        """path의 음성·영상을 인식한다. progress(처리한 초, 전체 초)는 워커의 진행 표시에 쓴다."""
        if language not in (None, "auto") and language not in LANGUAGE_CODES:
            raise SpeechError("UNSUPPORTED_LANGUAGE", str(language))
        code = None if language in (None, "auto") else LANGUAGE_CODES[language]
        model = self._load()
        with GATE:
            try:
                raw, info = model.transcribe(str(path), language=code, vad_filter=True, beam_size=self.beam_size,
                                             condition_on_previous_text=False)
                duration = float(getattr(info, "duration", 0) or 0)
                if self.max_seconds and duration > self.max_seconds:
                    raise SpeechError("AUDIO_TOO_LONG", f"{int(duration)}s")
                collected = []
                for segment in raw:  # 여기서 실제 인식이 진행된다
                    collected.append(segment)
                    if progress and duration:
                        progress(min(float(segment.end), duration), duration)
            except SpeechError:
                raise
            except Exception as error:  # 읽을 수 없는 파일, 디코딩 실패 등. 사용자에게는 상세를 보이지 않고 서버 로그에만 남긴다
                log.exception("Whisper transcription failed")
                raise SpeechError("INVALID_AUDIO", type(error).__name__) from error
        return {"segments": segments_from_whisper(collected), "language": language_from_whisper(getattr(info, "language", None)),
                "durationSeconds": round(duration, 2), "model": f"whisper-{self.model_size}"}


class FakeTranscriber:
    """모델 없이 정해진 결과를 돌려준다. 호출 흐름과 화면을 만들 때 쓴다."""

    SAMPLE = [{"start": 0.0, "end": 3.2, "text": "안녕하세요, 회의를 시작하겠습니다."},
              {"start": 3.2, "end": 7.8, "text": "다음 주 금요일에 출시 일정을 확정합니다."},
              {"start": 7.8, "end": 12.0, "text": "영희 님이 성능 시험 결과를 발표해 주세요."}]

    def __init__(self):
        self.calls = []

    def transcribe_file(self, path, language=None, progress=None):
        self.calls.append((str(path), language))
        if progress:
            progress(12.0, 12.0)
        return {"segments": [dict(segment) for segment in self.SAMPLE], "language": language if language not in (None, "auto") else "ko",
                "durationSeconds": 12.0, "model": "fake"}


_whisper = None


def create_transcriber():
    """STT_BACKEND에 맞는 인식기를 돌려준다. whisper는 모델을 한 번만 올리도록 하나를 계속 쓴다."""
    global _whisper
    backend = os.getenv("STT_BACKEND", "").strip()
    if backend == "fake":
        return FakeTranscriber()
    if backend == "whisper":
        if _whisper is None:
            _whisper = WhisperTranscriber(
                model_size=os.getenv("STT_MODEL", "base"), model_dir=os.getenv("STT_MODEL_DIR") or None,
                compute_type=os.getenv("STT_COMPUTE_TYPE", "int8"), threads=int(os.getenv("STT_THREADS", "0")),
                max_seconds=int(os.getenv("STT_MAX_SECONDS", "600")), beam_size=int(os.getenv("STT_BEAM_SIZE", "1")))
        return _whisper
    raise SpeechError("STT_NOT_CONFIGURED")


def transcribe_bytes(data, ext, language=None, progress=None):
    """메모리의 파일 내용을 임시 파일로 내려 인식한다. 영상도 같다(faster-whisper가 소리를 직접 읽는다)."""
    if ext.lower().lstrip(".") not in MEDIA_EXTENSIONS:
        raise SpeechError("UNSUPPORTED_AUDIO_FORMAT", ext)
    transcriber = create_transcriber()
    handle = tempfile.NamedTemporaryFile(suffix="." + ext.lower().lstrip("."), delete=False)
    try:
        with handle:
            handle.write(data)
        return transcriber.transcribe_file(handle.name, language, progress)
    finally:
        Path(handle.name).unlink(missing_ok=True)

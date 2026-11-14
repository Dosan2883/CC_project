"""Whisper 모델 파일을 미리 내려받아 서버에 둔다. 처음 한 번만 실행하면 이후에는 인터넷 없이 인식한다.

사용: python -m scripts.prepare_stt            (STT_MODEL, STT_MODEL_DIR 설정을 따른다)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core import config  # noqa: F401  (.env를 읽는다)


def main():
    size = os.getenv("STT_MODEL", "base")
    directory = os.getenv("STT_MODEL_DIR") or None
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper가 설치되어 있지 않습니다: pip install -r requirements-stt.txt")
        return 1
    print(f"모델 준비 중: {size} (처음에는 내려받느라 시간이 걸립니다)")
    WhisperModel(size, device="cpu", compute_type=os.getenv("STT_COMPUTE_TYPE", "int8"), download_root=directory)
    print("준비 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

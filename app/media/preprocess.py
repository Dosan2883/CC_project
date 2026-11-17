"""Bounded local FFmpeg preprocessing before loading an STT model."""
import json
import math
import os
import subprocess
from pathlib import Path


def run(command):
    from app.media.whisper import SpeechError
    try:
        return subprocess.run(command, capture_output=True, check=True, timeout=180).stdout
    except FileNotFoundError as error:
        raise SpeechError("FFMPEG_NOT_INSTALLED") from error
    except (subprocess.SubprocessError, OSError) as error:
        raise SpeechError("INVALID_AUDIO") from error


def prepare(path, directory, max_seconds=600):
    from app.media.whisper import SpeechError
    raw = run([os.getenv("FFPROBE_BIN", "ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe",
               "-show_entries", "format=duration:stream=codec_type,codec_name", "-of", "json", str(path)])
    try:
        info = json.loads(raw)
        duration = float(info["format"]["duration"])
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        if not math.isfinite(duration) or duration <= 0 or audio["codec_name"] not in {
            "aac", "mp3", "opus", "vorbis", "flac", "alac", "pcm_s16le", "pcm_s24le", "pcm_f32le", "pcm_u8"}:
            raise ValueError()
    except (ValueError, KeyError, StopIteration) as error:
        raise SpeechError("INVALID_AUDIO") from error
    if max_seconds and duration > max_seconds:
        raise SpeechError("AUDIO_TOO_LONG")
    chunk = max(15, min(300, int(os.getenv("STT_CHUNK_SECONDS", "120"))))
    pattern = str(Path(directory) / "audio-%04d.wav")
    run([os.getenv("FFMPEG_BIN", "ffmpeg"), "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
         "-i", str(path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", "-t", str(duration), "-f", "segment", "-segment_time", str(chunk),
         "-reset_timestamps", "1", pattern])
    files = sorted(Path(directory).glob("audio-*.wav"))
    if not files or len(files) > math.ceil(duration / chunk) + 1:
        raise SpeechError("INVALID_AUDIO")
    return files, duration, chunk

"""Safe WebVTT serialization; source text is never interpreted as cue markup."""
import html
import math


def timestamp(seconds):
    milliseconds = round(float(seconds) * 1000)
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, milliseconds = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"


def webvtt(segments, translated=False):
    cues = ["WEBVTT", ""]
    for index, segment in enumerate(sorted(segments, key=lambda s: s["start"]), 1):
        start, end = float(segment["start"]), float(segment["end"])
        if not math.isfinite(start + end) or start < 0 or end <= start:
            continue
        text = html.escape(segment.get("translated" if translated else "text", "")).replace("\r", "")
        text = "\n".join(line for line in text.split("\n") if line.strip())
        cues.extend([str(index), f"{timestamp(start)} --> {timestamp(end)}", text or " ", ""])
    return "\n".join(cues) + "\n"

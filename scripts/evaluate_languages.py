"""Evaluate labelled JSONL outputs without external API calls."""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

LANGUAGES = {"ko", "en", "ja", "zh-CN", "es", "fr"}
KINDS = {"ocr", "stt", "translation", "summary", "subtitle"}


def seconds(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError("Expected nonnegative seconds")
    return value


def subtitle_metrics(reference, actual):
    def by_id(cues):
        times = {}
        for cue in cues:
            start, end = seconds(cue["start"]), seconds(cue["end"])
            if cue["id"] in times or end < start:
                raise ValueError("Duplicate subtitle cue ID or reversed timestamps")
            times[cue["id"]] = start, end
        return times

    ref, out = by_id(reference), by_id(actual)
    shared = ref.keys() & out.keys()
    return {"matched": len(shared), "missing": len(ref.keys() - out.keys()), "extra": len(out.keys() - ref.keys()),
            "meanStartErrorMs": 1000 * sum(abs(ref[key][0] - out[key][0]) for key in shared) / len(shared) if shared else None,
            "meanEndErrorMs": 1000 * sum(abs(ref[key][1] - out[key][1]) for key in shared) / len(shared) if shared else None}


def distance(a, b):
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j]+1, current[-1]+1, previous[j-1]+(x != y)))
        previous = current
    return previous[-1]


def evaluate(rows):
    result = defaultdict(list)
    for row in rows:
        language, kind = row["language"], row["kind"]
        if language not in LANGUAGES:
            raise ValueError("Unsupported language: " + language)
        reference, actual = row["reference"], row["actual"]
        if len(reference) > 12000 or len(actual) > 12000:
            raise ValueError("Sample too long")
        metric = {"language":language, "kind":kind}
        if "durationSeconds" in row:
            metric["durationSeconds"] = seconds(row["durationSeconds"])
        if kind in {"ocr", "stt"}:
            metric["cer"] = distance(reference, actual) / max(1,len(reference))
            metric["wer"] = distance(reference.split(),actual.split()) / max(1,len(reference.split()))
            if "reviewedUnits" in row or "correctedUnits" in row:
                reviewed, corrected = row["reviewedUnits"], row["correctedUnits"]
                if type(reviewed) is not int or type(corrected) is not int or not 0 <= corrected <= reviewed or not reviewed:
                    raise ValueError("Expected 0 <= correctedUnits <= reviewedUnits and reviewedUnits > 0")
                metric["correctionRate"] = corrected / reviewed
        elif kind == "translation":
            score = float(row["humanScore"])
            if not 1 <= score <= 5:
                raise ValueError("humanScore must be between 1 and 5")
            metric["humanScore"] = score
        elif kind == "summary":
            metric["missingDecisions"] = [text for text in row["requiredDecisions"] if text not in actual]
        elif kind == "subtitle":
            metric.update(subtitle_metrics(reference, actual))
        else:
            raise ValueError("Unsupported sample kind")
        result[language].append(metric)
    return {"missingLanguages":sorted(LANGUAGES-set(result)),
            "missingKindsByLanguage": {lang:sorted(KINDS-{row["kind"] for row in result.get(lang, [])})
                                       for lang in sorted(LANGUAGES)}, "results":dict(result),
            "note":"WER uses whitespace tokens; prefer CER for Korean/Japanese/Chinese. Labelled real outputs required."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("samples", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.samples.read_text(encoding="utf-8").splitlines() if line.strip()]
    args.output.write_text(json.dumps(evaluate(rows),ensure_ascii=False,indent=2),encoding="utf-8")

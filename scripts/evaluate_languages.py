"""Evaluate labelled JSONL outputs without external API calls."""
import argparse
import json
from collections import defaultdict
from pathlib import Path

LANGUAGES = {"ko", "en", "ja", "zh-CN", "es", "fr"}


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
        if kind in {"ocr", "stt"}:
            metric["cer"] = distance(reference, actual) / max(1,len(reference))
            metric["wer"] = distance(reference.split(),actual.split()) / max(1,len(reference.split()))
        elif kind == "translation":
            score = float(row["humanScore"])
            if not 1 <= score <= 5:
                raise ValueError("humanScore must be between 1 and 5")
            metric["humanScore"] = score
        elif kind == "summary":
            metric["missingDecisions"] = [text for text in row["requiredDecisions"] if text not in actual]
        else:
            raise ValueError("Unsupported sample kind")
        result[language].append(metric)
    return {"missingLanguages":sorted(LANGUAGES-set(result)), "results":dict(result),
            "note":"WER uses whitespace tokens; prefer CER for Korean/Japanese/Chinese. Labelled real outputs required."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("samples", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.samples.read_text(encoding="utf-8").splitlines() if line.strip()]
    args.output.write_text(json.dumps(evaluate(rows),ensure_ascii=False,indent=2),encoding="utf-8")

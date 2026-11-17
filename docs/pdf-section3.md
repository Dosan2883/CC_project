# PDF section 3 implementation (2026-11-17)

Source: https://app.notion.com/p/40cbcc84e50e80e49376d991601130bc

E: MOV is accepted as video, with signature validation and a worker ffprobe
audio codec/duration check. Real Whisper processing requires FFmpeg and ffprobe
on PATH (or FFMPEG_BIN / FFPROBE_BIN absolute executable paths).
Audio is normalized to mono 16 kHz PCM WAV and split before inference.
STT_CHUNK_SECONDS defaults to 120 (15–300); STT_MAX_SECONDS remains 600.
Increase the latter explicitly to allow longer recordings. Chunk timestamps
are rebased to the original timeline. Confidence review flags are heuristics,
not calibrated recognition accuracy.

GET /api/media/{id}/subtitles?target=original exports original WebVTT.
For translated captions use target=ko/en/ja/zh-CN/es/fr and consent=true.
Both require bearer authentication and room membership; translated output is
checked against the current transcript revision. VTT files are stored by media
ID, revision and language. The player fetches authenticated VTT into Blob tracks.
Its selector is separate from text translation, initially preferredSubtitleLanguage.
MOV playback depends on browser container/codec support; normalization is for
STT, not video transcoding.

F: each message/file has its own translation language, saved per account/item in
browser localStorage. Text retains its original while bodyCurrent/page/segment
corrections supply the current version. Message edits require the sender;
document/segment correction requires membership. Both reject stale revisions.
Auto language selection is available in source settings; langdetect runs locally
with a deterministic seed, marking short, uncertain or unsupported text for review.
UI localization is Korean/English/Japanese; recognition/translation support six languages.

Caches include schema, operation, model/prompt version, source language, text,
room/source scope and source revision. Batch segment translation rechecks the
transcript version and contents before return. Source edits atomically enqueue
SOURCE_REFRESH outbox records. Existing AI worker recovery schedules new versions
of previously consented saved summaries and rebuilds original/previously requested
translated captions. Deleted summaries are excluded. Old summaries are labelled
as earlier versions. Regeneration uses existing quotas, retries three times,
and records FAILED and the error type when exhausted. Run the AI worker/recovery
schedule for this flow. Edits perform no paid model call in the request thread.

## Validation and deployment

Install requirements.txt and requirements-stt.txt, FFmpeg/ffprobe, then prepare
the Whisper model with python -m scripts.prepare_stt. OCR uses requirements-ocr.txt.
No AWS resources were created by this change.

Run: python -m unittest discover -s tests -p test_pdf_requirements.py -v

Tests cover MOV/VTT safety, preflight rejection, normalization commands, chunk
timing, six language identification samples, cache keys, edit conflicts,
subtitle permissions and summary regeneration.

Real six-language OCR/STT/translation/summary quality is not established by mock
models or identification samples. Collect real outputs as JSONL: language,
kind (ocr/stt/translation/summary), reference, actual; humanScore (1–5) for
translation; requiredDecisions (list) for summary.

Run: python -m scripts.evaluate_languages samples.jsonl --output quality-results.json

The report exposes missing languages, CER/WER, human translation ratings and
missing decisions. Use multilingual speakers and scanned/text PDFs. Manually
check subtitle synchronization at chunk boundaries and after seeking.

"""Page-local OCR. Heavy models are loaded only in the media worker."""
import io
import threading

LANGUAGES = {"ko": "korean", "en": "en", "ja": "japan", "zh-CN": "ch", "es": "es", "fr": "fr"}
_models = {}
_lock = threading.Lock()


def image(data):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 25_000_000
    source = Image.open(io.BytesIO(data))
    if source.width * source.height > 25_000_000:
        raise ValueError("image exceeds pixel limit")
    source.load()
    return ImageOps.autocontrast(ImageOps.exif_transpose(source).convert("RGB"))


def raster(data, ext, page=1):
    if ext != "pdf":
        if page != 1:
            raise ValueError("invalid page")
        return image(data)
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(data) as document:
        if not 1 <= page <= len(document):
            raise ValueError("invalid page")
        pdf_page = document[page - 1]
        try:
            width, height = pdf_page.get_size()
            scale = min(2, (25_000_000 / max(1, width * height)) ** 0.5)
            bitmap = pdf_page.render(scale=scale)
            try:
                return bitmap.to_pil().convert("RGB").copy()
            finally:
                bitmap.close()
        finally:
            pdf_page.close()


def recognize(source, language="auto"):
    import numpy as np
    from paddleocr import PaddleOCR
    candidates = list(LANGUAGES) if language == "auto" else [language]
    best = None
    # Paddle predictors are not shared concurrently between requests.
    with _lock:
        for code in candidates:
            if code not in LANGUAGES:
                raise ValueError("unsupported OCR language")
            try:
                if code not in _models:
                    _models[code] = PaddleOCR(lang=LANGUAGES[code], use_doc_orientation_classify=False,
                                             use_doc_unwarping=False, use_textline_orientation=True)
                result = next(iter(_models[code].predict(np.asarray(source))))
            except Exception:
                if language == "auto":
                    continue
                raise
            result = result.json if hasattr(result, "json") else result
            if isinstance(result, str):
                import json
                result = json.loads(result)
            result = result.get("res", result)
            regions = []
            for text, score, polygon in zip(result.get("rec_texts", []), result.get("rec_scores", []), result.get("rec_polys", [])):
                regions.append({"text": str(text), "confidence": float(score),
                                "polygon": [[float(x), float(y)] for x, y in polygon], "language": code})
            confidence = sum(r["confidence"] for r in regions) / max(1, len(regions))
            candidate = {"text": "\n".join(r["text"] for r in regions), "regions": regions,
                         "language": code, "model": "PaddleOCR-3/" + LANGUAGES[code],
                         "confidence": confidence, "needsReview": not regions or confidence < .8 or language == "auto",
                         "width": source.width, "height": source.height}
            if best is None or confidence > best["confidence"]:
                best = candidate
    if best is None:
        raise RuntimeError("no OCR model produced a result")
    return best

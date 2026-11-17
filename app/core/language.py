"""Local language identification. Short/ambiguous text stays marked for review."""
import logging
import re

logger = logging.getLogger(__name__)
_warned_missing_langdetect = False


def detect(text):
    global _warned_missing_langdetect
    try:
        from langdetect import DetectorFactory, detect_langs, LangDetectException
        DetectorFactory.seed = 0
        try:
            values = detect_langs(text[:12000])
        except LangDetectException:
            values = []
        if values:
            best = values[0]
            code = "zh-CN" if best.lang == "zh-cn" else best.lang
            supported = code in {"ko", "en", "ja", "zh-CN", "es", "fr"}
            return {"language":code if supported else "und", "confidence":round(best.prob, 3),
                    "needsReview":not supported or len(text.strip()) < 15 or best.prob < .85,
                    "method":"langdetect"}
    except ImportError:
        if not _warned_missing_langdetect:
            logger.warning("langdetect is not installed; Latin-script languages (en/es/fr) will fall back to 'und'")
            _warned_missing_langdetect = True
    code = "ko" if re.search("[가-힣]", text) else "ja" if re.search("[ぁ-ヿ]", text) else "zh-CN" if re.search("[一-鿿]", text) else "und"
    return {"language": code, "confidence": None, "needsReview": True, "method": "script-fallback"}

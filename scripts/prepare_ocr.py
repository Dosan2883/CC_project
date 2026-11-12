"""Warm each supported OCR model and record a readiness manifest."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.media.ocr import LANGUAGES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=os.getenv("OCR_READY_FILE", "/var/lib/messenger/ocr-ready.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    manifest = Path(args.manifest)
    versions = {name: importlib.metadata.version(name) for name in ("paddleocr", "paddlepaddle")}
    cache = Path(os.getenv("PADDLE_PDX_CACHE_HOME", str(Path.home() / ".paddlex")))
    if args.check:
        saved = json.loads(manifest.read_text())
        if saved.get("versions") != versions or saved.get("languages") != list(LANGUAGES) or not cache.exists():
            raise RuntimeError("OCR models require preparation")
        print("OCR readiness manifest verified")
        return
    import gc
    import numpy as np
    from paddleocr import PaddleOCR
    for code, language in LANGUAGES.items():
        model = PaddleOCR(lang=language, use_doc_orientation_classify=False,
                          use_doc_unwarping=False, use_textline_orientation=True)
        list(model.predict(np.full((64, 256, 3), 255, dtype=np.uint8)))
        del model
        gc.collect()
        print("Prepared OCR model:", code)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"versions": versions, "languages": list(LANGUAGES)}))


if __name__ == "__main__":
    main()

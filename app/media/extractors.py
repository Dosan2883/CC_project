"""문서 본문 추출 (PDF / DOCX / TXT).

네트워크나 AWS를 쓰지 않는 순수 함수라서, 나중에 EC2 미디어 워커에서 그대로 재사용한다.
결과 형태: {"pages": [페이지별 텍스트], "needsOcr": 텍스트가 없어 OCR이 필요한지}
"""
import io
import zipfile
from typing import Callable
from xml.etree import ElementTree as ET

MAX_PAGES = 300
MAX_UNCOMPRESSED = 50 * 1024 * 1024  # DOCX 압축 해제 크기 상한 (zip bomb 방지)
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

Progress = Callable[[int, int], None]


class ExtractError(Exception):
    """파일 자체의 문제. 다시 시도해도 결과가 같으므로 재시도하지 않는다."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def extract_txt(data: bytes, on_progress: Progress) -> dict:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ExtractError("UNSUPPORTED_ENCODING")
    on_progress(1, 1)
    return {"pages": [text.strip()], "needsOcr": False}


def extract_pdf(data: bytes, on_progress: Progress) -> dict:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractError("ENCRYPTED_PDF")
        total = len(reader.pages)
        if total > MAX_PAGES:
            raise ExtractError("TOO_MANY_PAGES")
        pages = []
        for i, page in enumerate(reader.pages, 1):
            pages.append((page.extract_text() or "").strip())
            on_progress(i, total)
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError("INVALID_PDF") from e
    # 모든 페이지에서 글자가 안 나오면 스캔본으로 보고 OCR 대상으로 표시한다.
    return {"pages": pages, "needsOcr": not any(pages)}


def extract_docx(data: bytes, on_progress: Progress) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_UNCOMPRESSED:
                raise ExtractError("TOO_LARGE")
            with z.open(info) as f:
                xml = f.read(MAX_UNCOMPRESSED + 1)  # 헤더가 거짓이어도 읽는 양을 제한
            if len(xml) > MAX_UNCOMPRESSED:
                raise ExtractError("TOO_LARGE")
        root = ET.fromstring(xml)
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError("INVALID_DOCX") from e

    paragraphs = []
    for p in root.iter(W + "p"):
        parts = []
        for node in p.iter():
            if node.tag == W + "t":
                parts.append(node.text or "")
            elif node.tag == W + "tab":
                parts.append("\t")
            elif node.tag in (W + "br", W + "cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    on_progress(1, 1)
    # DOCX는 고정된 페이지 개념이 없어서 한 덩어리로 돌려준다.
    return {"pages": ["\n".join(paragraphs).strip()], "needsOcr": False}


EXTRACTORS = {"txt": extract_txt, "pdf": extract_pdf, "docx": extract_docx}


def extract(ext: str, data: bytes, on_progress: Progress) -> dict:
    fn = EXTRACTORS.get(ext)
    if not fn:
        raise ExtractError("UNSUPPORTED_TYPE")
    return fn(data, on_progress)

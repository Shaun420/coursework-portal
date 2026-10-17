"""Bytes → plain text.

Analysis needs text, students submit PDFs and Word files, so this is the one
place that knows about document formats. Anything it cannot parse degrades to
"unsupported" rather than raising, so a corrupt upload never kills the queue.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from app.core.logging import get_logger

log = get_logger(__name__)

HTML_TAG = re.compile(r"<[^>]+>")
RTF_CONTROL = re.compile(r"\\[a-z]+-?\d*\s?|[{}]")


@dataclass(slots=True)
class ExtractedText:
    text: str
    method: str          # pdf | docx | plain | html | rtf | unsupported
    byte_size: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _from_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            return ""
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception as exc:  # malformed page
            log.debug("pdf page failed: %s", exc)
    return "\n".join(parts)


def _from_docx(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def _from_html(data: bytes) -> str:
    text = HTML_TAG.sub(" ", _decode(data))
    return re.sub(r"[ \t]+", " ", text)


def _from_rtf(data: bytes) -> str:
    text = RTF_CONTROL.sub(" ", _decode(data))
    return re.sub(r"[ \t]+", " ", text)


SUPPORTED = {".pdf", ".docx", ".txt", ".md", ".tex", ".html", ".htm", ".rtf", ".odt", ".doc"}


def extract(filename: str, data: bytes) -> ExtractedText:
    """Best-effort text extraction. Never raises."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    try:
        if ext == ".pdf":
            return ExtractedText(_from_pdf(data).strip(), "pdf", len(data))
        if ext == ".docx":
            return ExtractedText(_from_docx(data).strip(), "docx", len(data))
        if ext in {".html", ".htm"}:
            return ExtractedText(_from_html(data).strip(), "html", len(data))
        if ext == ".rtf":
            return ExtractedText(_from_rtf(data).strip(), "rtf", len(data))
        if ext == ".odt":
            return ExtractedText(_odt(data).strip(), "odt", len(data))
        if ext in {".doc",}:
            return ExtractedText("", "unsupported", len(data), error="legacy .doc is not parsed")
        return ExtractedText(_decode(data).strip(), "plain", len(data))
    except Exception as exc:
        log.warning("extraction failed for %s (%s): %s", filename, ext, exc)
        return ExtractedText("", "unsupported", len(data), error=str(exc)[:200])


def _odt(data: bytes) -> str:
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        if "content.xml" not in zf.namelist():
            return ""
        return HTML_TAG.sub(" ", zf.read("content.xml").decode("utf-8", "replace"))

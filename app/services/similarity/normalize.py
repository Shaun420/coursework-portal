"""Normalisation shared by the similarity engine.

Normalisation is intentionally aggressive: case, punctuation, digits, citation
noise and whitespace are all removed so that a lightly reformatted copy of the
same text produces the same shingles as the original.
"""

from __future__ import annotations

import re
import unicodedata

_TOKEN = re.compile(r"[a-z]+")
_CITATION = re.compile(
    r"\[[^\]]{0,80}\]"
    r"|\(\s*[A-Z][a-zA-Z'-]+(?:\s+(?:et al\.?|&|and)\s+[A-Z][a-zA-Z'-]+)?"
    r"\s*,?\s*\d{4}[a-z]?\s*\)"
)
_URL = re.compile(r"https?://\S+|www\.\S+")
_PAGENOISE = re.compile(r"\bpage\s+\d+\b|\b\d+\s+of\s+\d+\b", re.IGNORECASE)


def normalize(text: str) -> str:
    """Lowercase, de-accented, punctuation/digit-free single-spaced text."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _URL.sub(" ", text)
    text = _CITATION.sub(" ", text)
    text = _PAGENOISE.sub(" ", text)
    text = text.lower()
    # Drop apostrophes inside words so "it's" and "its" normalise identically;
    # otherwise contractions split into two spurious tokens.
    text = re.sub(r"(?<=\w)'(?=\w)", "", text)
    text = re.sub(r"[^a-z\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(normalize(text))


def sentences(text: str) -> list[str]:
    """Sentence split on terminal punctuation, tolerant of abbreviations."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])", cleaned)
    return [p.strip() for p in parts if len(p.strip()) > 1]


def paragraphs(text: str) -> list[str]:
    out = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return out or ([text.strip()] if text.strip() else [])


def word_count(text: str) -> int:
    return len(tokenize(text))

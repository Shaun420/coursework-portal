"""Pluggable detector backends.

The built-in :class:`HeuristicDetector` is dependency-free and runs entirely on
the portal box. :class:`ExternalApiDetector` shows the seam for a commercial
detector (GPTZero, Turnitin AI, Originality): implement `score()` and register
it in :func:`get_detector` — nothing else in the app changes, because every
caller sees the same dict.
"""

from __future__ import annotations

from typing import Protocol

from app.services.detection.model import detect as heuristic_detect


class AIDetectorBackend(Protocol):
    name: str

    async def score(self, text: str) -> dict:
        """Return at least {'ai_score': float, 'confidence': float}."""


class HeuristicDetector:
    name = "heuristic-stylometric-v1"

    async def score(self, text: str) -> dict:
        return heuristic_detect(text).as_report()


class ExternalApiDetector:  # pragma: no cover - integration point
    """Skeleton for a hosted detector. Wire in the vendor SDK/HTTP call."""

    name = "external-api"

    def __init__(self, api_key: str, endpoint: str, timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout

    async def score(self, text: str) -> dict:  # pragma: no cover
        raise NotImplementedError("plug the vendor HTTP call in here")


_detector: AIDetectorBackend | None = None


def get_detector() -> AIDetectorBackend:
    global _detector
    if _detector is None:
        _detector = HeuristicDetector()
    return _detector

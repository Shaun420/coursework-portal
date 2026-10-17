"""The heuristic AI-detection model.

    score = Σ (weightᵢ × ai_likenessᵢ) / Σ weightᵢ        ∈ [0, 1]

A weighted mean over the stylometric features in :mod:`features`. No black box:
`explain()` returns every feature with its value, its normalised AI-likeness
and its share of the final score, which is what a fairness review of an
academic-integrity decision needs.

`confidence` is deliberately separate from `score`: a 60-word paragraph and a
3000-word essay can produce the same score, but only the second is trustworthy.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from app.core.config import settings
from app.services.detection import features as F
from app.services.similarity.normalize import tokenize


@dataclass(slots=True)
class DetectionResult:
    score: float
    confidence: float
    word_count: int
    reliable: bool
    features: list[F.Feature] = field(default_factory=list)
    engine: str = "heuristic-stylometric-v1"

    def explain(self) -> list[dict]:
        """Per-feature breakdown. `share_of_score` sums to 1 across features."""
        total_contribution = sum(f.contribution for f in self.features) or 1.0
        return [
            {
                "feature": f.name,
                "value": round(f.value, 4),
                "ai_likeness": round(f.score, 4),
                "weight": f.weight,
                "share_of_score": round(f.contribution / total_contribution, 4),
                "note": f.note,
            }
            for f in sorted(self.features, key=lambda x: x.contribution, reverse=True)
        ]

    def as_report(self) -> dict:
        return {
            "ai_score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "word_count": self.word_count,
            "reliable": self.reliable,
            "engine": self.engine,
            "disclaimer": (
                "Heuristic stylometric estimate, not proof of AI authorship. "
                "Treat as a review prompt, never as a sole finding."
            ),
            "features": self.explain(),
        }


def _confidence(word_count: int, feats: list[F.Feature]) -> float:
    """Grows with length; shrinks when the features disagree with each other."""
    minimum = settings.ai_detection_min_words
    length_factor = min(1.0, math.log1p(word_count) / math.log1p(minimum * 4))
    if not feats:
        return 0.0
    values = [f.score for f in feats]
    disagreement = statistics.pstdev(values)          # 0 = unanimous, ~0.5 = split
    agreement = max(0.0, 1.0 - disagreement * 1.6)
    return round(min(1.0, 0.55 * length_factor + 0.45 * agreement), 4)


def detect(text: str) -> DetectionResult:
    feats = F.compute(text)
    word_count = len(tokenize(text))
    if not feats or word_count == 0:
        return DetectionResult(0.0, 0.0, 0, False, feats)

    total_weight = sum(f.weight for f in feats)
    score = sum(f.contribution for f in feats) / total_weight
    score = max(0.0, min(1.0, score))
    reliable = word_count >= settings.ai_detection_min_words
    return DetectionResult(
        score=score,
        confidence=_confidence(word_count, feats),
        word_count=word_count,
        reliable=reliable,
        features=feats,
    )

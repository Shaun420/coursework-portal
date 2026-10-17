"""AI detection: separation, calibration and explainability."""

from __future__ import annotations

import pytest

from app.services.detection.base import get_detector
from app.services.detection.features import compute
from app.services.detection.model import detect
from app.services.similarity.normalize import tokenize

from .conftest import AI_STYLE_ESSAY, HUMAN_ESSAY, UNRELATED_ESSAY


def test_human_text_scores_below_threshold():
    result = detect(HUMAN_ESSAY)
    assert result.score < 0.5, result.explain()[:6]
    assert result.reliable is True


def test_llm_style_text_scores_above_threshold():
    result = detect(AI_STYLE_ESSAY)
    assert result.score > 0.5, result.explain()[:6]


def test_two_human_essays_both_read_as_human():
    assert detect(HUMAN_ESSAY).score < 0.5
    assert detect(UNRELATED_ESSAY).score < 0.5


def test_ai_style_is_separated_from_human():
    human = detect(HUMAN_ESSAY).score
    ai = detect(AI_STYLE_ESSAY).score
    assert ai - human > 0.15, f"separation too small: human={human} ai={ai}"


def test_detection_is_deterministic():
    assert detect(AI_STYLE_ESSAY).score == detect(AI_STYLE_ESSAY).score


def test_explain_covers_every_feature_and_shares_sum_to_one():
    result = detect(HUMAN_ESSAY)
    rows = result.explain()
    assert len(rows) == len(compute(HUMAN_ESSAY))
    total = sum(r["share_of_score"] for r in rows)
    assert total == pytest.approx(1.0, abs=0.01)
    keys = {"feature", "value", "ai_likeness", "weight", "share_of_score"}
    assert all(keys <= set(r) for r in rows)


def test_short_text_is_flagged_unreliable_but_still_scored():
    result = detect("Distributed systems are hard. Consensus is harder.")
    assert result.reliable is False
    assert 0.0 <= result.score <= 1.0


def test_confidence_grows_with_length():
    short = detect(HUMAN_ESSAY[:400]).confidence
    long = detect(HUMAN_ESSAY * 4).confidence
    assert long >= short


def test_empty_text_is_safe():
    result = detect("")
    assert result.score == 0.0
    assert result.word_count == 0
    assert result.reliable is False


def test_report_shape():
    report = detect(AI_STYLE_ESSAY).as_report()
    keys = {
        "ai_score", "confidence", "word_count", "reliable",
        "engine", "features", "disclaimer",
    }
    assert keys <= set(report)
    assert "not proof" in report["disclaimer"]


@pytest.mark.asyncio
async def test_backend_protocol():
    backend = get_detector()
    report = await backend.score(HUMAN_ESSAY)
    assert report["word_count"] == len(tokenize(HUMAN_ESSAY))
    assert 0.0 <= report["ai_score"] <= 1.0

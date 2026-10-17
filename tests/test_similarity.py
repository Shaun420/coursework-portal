"""Similarity engine: the numbers behind the similarity index."""

from __future__ import annotations

import pytest

from app.services.similarity.engine import CorpusDocument, SimilarityEngine
from app.services.similarity.minhash import (
    LSHIndex,
    MinHasher,
    block_hashes,
    block_overlap,
    jaccard_estimate,
    pack_signature,
    shingles,
    unpack_signature,
)
from app.services.similarity.normalize import normalize, tokenize

from .conftest import HUMAN_ESSAY, HUMAN_VARIANT, UNRELATED_ESSAY


@pytest.fixture
def engine() -> SimilarityEngine:
    return SimilarityEngine(shingle_size=5, perms=128, bands=32)


def test_normalize_strips_punctuation_and_case():
    assert normalize("Hello, World! It's 2026.") == "hello world its"


def test_tokenize_ignores_digits():
    assert tokenize("There are 42 reasons, 7 of them good.") == [
        "there", "are", "reasons", "of", "them", "good",
    ]


def test_identical_documents_score_one(engine):
    fp = engine.fingerprint(HUMAN_ESSAY)
    same = engine.fingerprint(HUMAN_ESSAY)
    result = engine.compare(
        fp,
        [CorpusDocument("sub:1", "other student", same.signature, same.blocks, same.word_count)],
    )
    assert result.similarity_index == pytest.approx(1.0, abs=1e-6)
    assert result.verbatim_overlap == pytest.approx(1.0, abs=1e-6)


def test_paraphrase_is_detected(engine):
    """Same argument, lightly reworded — should land above the 50 % threshold."""
    target = engine.fingerprint(HUMAN_VARIANT)
    peer = engine.fingerprint(HUMAN_ESSAY)
    result = engine.compare(
        target,
        [CorpusDocument("sub:1", "peer", peer.signature, peer.blocks, peer.word_count)],
    )
    # Half the blocks are verbatim, so the index lands exactly on the
    # threshold — and the policy is "up to 50 %", so this is flagged.
    assert result.similarity_index >= 0.5, result.as_report()
    assert result.verbatim_overlap > 0.4


def test_unrelated_documents_stay_low(engine):
    target = engine.fingerprint(HUMAN_ESSAY)
    peer = engine.fingerprint(UNRELATED_ESSAY)
    result = engine.compare(
        target,
        [CorpusDocument("sub:1", "peer", peer.signature, peer.blocks, peer.word_count)],
    )
    assert result.similarity_index < 0.2, result.as_report()


def test_reference_material_counts(engine):
    """Verbatim reuse of instructor material is reported even with no peers."""
    target = engine.fingerprint("intro " * 20 + UNRELATED_ESSAY)
    ref = engine.fingerprint(UNRELATED_ESSAY)
    result = engine.compare(
        target,
        [CorpusDocument("ref:1", "reference: lecture", ref.signature, ref.blocks,
                        ref.word_count, kind="reference")],
    )
    assert result.reference_overlap > 0.5
    assert result.similarity_index > 0.25


def test_empty_corpus_is_clean(engine):
    target = engine.fingerprint(HUMAN_ESSAY)
    result = engine.compare(target, [])
    assert result.similarity_index == 0.0
    assert result.comparisons == 0


def test_signature_roundtrip_is_lossless(engine):
    sig = engine.fingerprint(HUMAN_ESSAY).signature
    assert unpack_signature(pack_signature(sig)) == sig


def test_minhash_estimates_jaccard_reasonably():
    hasher = MinHasher(128)
    a = set(range(0, 1000))
    b = set(range(250, 1250))
    true_jaccard = len(a & b) / len(a | b)
    est = jaccard_estimate(hasher.signature(a), hasher.signature(b))
    # 128 permutations give a standard error of ~0.044 at J=0.6, so allow ~2.5 SE.
    assert abs(est - true_jaccard) < 0.12


def test_lsh_finds_similar_and_ignores_noise():
    hasher = MinHasher(128)
    index = LSHIndex(perms=128, bands=32)
    base = shingles(tokenize(HUMAN_ESSAY), 5)
    index.add("copy", hasher.signature(base))
    index.add("noise", hasher.signature(set(range(100000, 110000))))

    cands = index.candidates(hasher.signature(base))
    assert "copy" in cands
    top = index.top_matches(hasher.signature(base), limit=3)
    assert top[0][0] == "copy"
    assert top[0][1] == pytest.approx(1.0, abs=1e-6)


def test_lsh_rejects_indivisible_bands():
    with pytest.raises(ValueError):
        LSHIndex(perms=128, bands=7)


def test_verbatim_blocks_survive_surrounding_text():
    """A copied passage is found even when embedded in unrelated writing."""
    padded = tokenize("preamble " * 30 + HUMAN_ESSAY + " " + "postscript " * 30)
    original = tokenize(HUMAN_ESSAY)
    assert block_overlap(block_hashes(original), block_hashes(padded)) > 0.5


def test_verbatim_blocks_do_not_match_unrelated_text():
    a = block_hashes(tokenize(HUMAN_ESSAY))
    b = block_hashes(tokenize(UNRELATED_ESSAY))
    assert block_overlap(a, b) == 0.0


def test_short_document_still_fingerprints(engine):
    fp = engine.fingerprint("two words")
    assert len(fp.signature) == 128
    assert fp.word_count == 2

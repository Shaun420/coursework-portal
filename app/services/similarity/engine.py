"""Similarity engine.

Produces one number — the **similarity index** — plus the evidence behind it.

    reference_excess  = max(0, reference_overlap - 0.25)
    blend             = 0.45 · verbatim_overlap
                      + 0.35 · calibrated_peer_max
                      + 0.20 · reference_overlap
    similarity_index  = clamp01( max(verbatim_overlap, blend, reference_excess) )
    calibrated_peer_max = min(1, peer_max_jaccard / 0.85)

The `max()` means a wholesale copy scores 1.00 instead of being diluted by the
weighting, while the blend still catches partial reuse. `reference_excess` lets
heavy reuse of instructor material cross the threshold on its own, but quoting
up to a quarter of a reference document is treated as legitimate and adds
nothing. Every component is reported, so a reviewer can see *why* a submission
scored what it did.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import settings
from app.core.logging import get_logger
from app.services.similarity.minhash import (
    LSHIndex,
    MinHasher,
    block_hashes,
    block_overlap,
    jaccard_estimate,
    pack_signature,
    shingles,
)
from app.services.similarity.normalize import normalize, tokenize

log = get_logger(__name__)

W_VERBATIM = 0.45
W_PEER = 0.35
W_REFERENCE = 0.20
JACCARD_CALIBRATION_CEILING = 0.85  # Jaccard above this counts as a full match
# Quoting up to this fraction of an instructor document is legitimate; only the
# excess counts towards the index.
REFERENCE_ALLOWANCE = 0.25


@dataclass(slots=True)
class Fingerprint:
    """Everything we need to compare a document, cheaply and repeatably."""

    normalized: str
    signature: list[int]
    blocks: set[str]
    word_count: int
    shingle_count: int

    def to_storable(self) -> dict:
        return {
            "signature": pack_signature(self.signature),
            "blocks": sorted(self.blocks),
            "word_count": self.word_count,
            "shingle_count": self.shingle_count,
        }


@dataclass(slots=True)
class CorpusDocument:
    doc_id: str
    label: str
    signature: list[int]
    blocks: set[str]
    word_count: int = 0
    kind: str = "peer"  # peer | reference


@dataclass(slots=True)
class SimilarityResult:
    similarity_index: float
    verbatim_overlap: float
    peer_max: float
    reference_overlap: float
    peer_label: str | None
    comparisons: int
    matches: list[dict] = field(default_factory=list)

    def as_report(self) -> dict:
        return {
            "similarity_index": round(self.similarity_index, 4),
            "components": {
                "verbatim_overlap": round(self.verbatim_overlap, 4),
                "peer_max_jaccard": round(self.peer_max, 4),
                "peer_max_calibrated": round(
                    min(1.0, self.peer_max / JACCARD_CALIBRATION_CEILING), 4
                ),
                "reference_overlap": round(self.reference_overlap, 4),
                "reference_excess": round(
                    max(0.0, self.reference_overlap - REFERENCE_ALLOWANCE), 4
                ),
            },
            "weights": {
                "verbatim_overlap": W_VERBATIM,
                "peer_similarity": W_PEER,
                "reference_material": W_REFERENCE,
                "jaccard_calibration_ceiling": JACCARD_CALIBRATION_CEILING,
                "reference_quoting_allowance": REFERENCE_ALLOWANCE,
            },
            "interpretation": (
                "similarity_index measures the share of THIS submission that is not "
                "original. It is directional: a short copy of a long essay scores high, "
                "while the long original scores low, because most of the original is not "
                "present in the copy. Review both sides of a match before acting."
            ),
            "strongest_match_label": self.peer_label,
            "documents_compared": self.comparisons,
            "matches": self.matches,
        }


class SimilarityEngine:
    def __init__(
        self,
        shingle_size: int | None = None,
        perms: int | None = None,
        bands: int | None = None,
        max_comparisons: int | None = None,
    ) -> None:
        self.shingle_size = shingle_size or settings.shingle_size
        self.perms = perms or settings.minhash_perms
        self.bands = bands or settings.lsh_bands
        self.max_comparisons = max_comparisons or settings.similarity_max_comparisons
        self.hasher = MinHasher(self.perms)

    # ── fingerprinting ──────────────────────────────────────────────────────

    def fingerprint(self, text: str) -> Fingerprint:
        normalized = normalize(text)
        tokens = tokenize(text)
        sh = shingles(tokens, self.shingle_size)
        return Fingerprint(
            normalized=normalized,
            signature=self.hasher.signature(sh),
            blocks=block_hashes(tokens),
            word_count=len(tokens),
            shingle_count=len(sh),
        )

    @staticmethod
    def fingerprint_from_stored(stored: dict) -> Fingerprint:
        from app.services.similarity.minhash import unpack_signature

        return Fingerprint(
            normalized="",
            signature=unpack_signature(stored.get("signature")) or [],
            blocks=set(stored.get("blocks") or []),
            word_count=int(stored.get("word_count") or 0),
            shingle_count=int(stored.get("shingle_count") or 0),
        )

    # ── comparison ──────────────────────────────────────────────────────────

    def compare(
        self, target: Fingerprint, corpus: list[CorpusDocument]
    ) -> SimilarityResult:
        peers = [d for d in corpus if d.kind == "peer"]
        references = [d for d in corpus if d.kind == "reference"]

        index = LSHIndex(perms=self.perms, bands=self.bands)
        for doc in peers:
            index.add(doc.doc_id, doc.signature)

        candidates = index.candidates(target.signature, limit=self.max_comparisons)
        by_id = {d.doc_id: d for d in peers}
        if not candidates and peers:
            # Tiny cohorts: LSH may miss, and a full scan is free. Compare all.
            candidates = [d.doc_id for d in peers]

        matches: list[dict] = []
        peer_max = 0.0
        peer_label: str | None = None
        verbatim = 0.0

        for doc_id in candidates:
            doc = by_id.get(doc_id)
            if doc is None:
                continue
            sim = jaccard_estimate(target.signature, doc.signature)
            v = block_overlap(target.blocks, doc.blocks)
            matches.append(
                {
                    "document": doc.label,
                    "id": doc.doc_id,
                    "jaccard": round(sim, 4),
                    "verbatim_fraction": round(v, 4),
                    "words": doc.word_count,
                }
            )
            if sim > peer_max:
                peer_max, peer_label = sim, doc.label
            verbatim = max(verbatim, v)

        matches.sort(key=lambda m: (m["jaccard"], m["verbatim_fraction"]), reverse=True)

        reference_overlap = 0.0
        for ref in references:
            reference_overlap = max(reference_overlap, block_overlap(target.blocks, ref.blocks))

        calibrated_peer = min(1.0, peer_max / JACCARD_CALIBRATION_CEILING)
        reference_excess = max(0.0, reference_overlap - REFERENCE_ALLOWANCE)
        blend = (
            W_VERBATIM * verbatim + W_PEER * calibrated_peer + W_REFERENCE * reference_overlap
        )
        # max() so that a wholesale copy scores 1.00 instead of being diluted by
        # the weighting, and so that heavy reuse of instructor material can cross
        # the threshold on its own once it exceeds the quoting allowance.
        similarity_index = max(verbatim, blend, reference_excess)
        similarity_index = max(0.0, min(1.0, similarity_index))

        return SimilarityResult(
            similarity_index=similarity_index,
            verbatim_overlap=verbatim,
            peer_max=peer_max,
            reference_overlap=reference_overlap,
            peer_label=peer_label,
            comparisons=len(candidates),
            matches=matches[:8],
        )

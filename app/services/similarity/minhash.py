"""MinHash + LSH primitives.

Why MinHash rather than pairwise Jaccard over every pair: comparing one
submission against a 10,000-document corpus pairwise is O(N) set
intersections over ~50 k shingles each. MinHash reduces a document to a
128-integer signature (1 KB), LSH banding turns the corpus lookup into a hash
table probe, and only the handful of candidates that share a band get a real
comparison. That is what keeps analysis latency flat as the cohort grows.

Everything here is deterministic and dependency-free.
"""

from __future__ import annotations

import base64
import hashlib
import random
import struct
from collections import defaultdict

PRIME = (1 << 61) - 1  # Mersenne prime, keeps (a*h+b) in 64-bit range
MAX_HASH = (1 << 64) - 1


def hash64(value: str) -> int:
    return int.from_bytes(hashlib.blake2b(value.encode(), digest_size=8).digest(), "big")


def shingles(tokens: list[str], k: int = 5) -> set[int]:
    """k-word shingles hashed to 64-bit ids."""
    if len(tokens) < k:
        return {hash64(" ".join(tokens))} if tokens else set()
    return {hash64(" ".join(tokens[i : i + k])) for i in range(len(tokens) - k + 1)}


def _permutations(perms: int) -> list[tuple[int, int]]:
    rng = random.Random(20240117)  # fixed seed → signatures comparable across runs
    return [(rng.randrange(1, PRIME), rng.randrange(0, PRIME)) for _ in range(perms)]


class MinHasher:
    __slots__ = ("perms", "_ab")

    def __init__(self, perms: int = 128) -> None:
        self.perms = perms
        self._ab = _permutations(perms)

    def signature(self, shingle_ids: set[int]) -> list[int]:
        if not shingle_ids:
            return [MAX_HASH] * self.perms
        sig = [MAX_HASH] * self.perms
        for h in shingle_ids:
            for i, (a, b) in enumerate(self._ab):
                v = (a * h + b) % PRIME
                if v < sig[i]:
                    sig[i] = v
        return sig


def jaccard_estimate(sig_a: list[int], sig_b: list[int]) -> float:
    """Fraction of agreeing permutation slots ≈ Jaccard similarity."""
    if not sig_a or not sig_b or len(sig_a) != len(sig_b):
        return 0.0
    agree = sum(1 for x, y in zip(sig_a, sig_b, strict=True) if x == y)
    return agree / len(sig_a)


# ── (de)serialisation ─────────────────────────────────────────────────────────


def pack_signature(sig: list[int]) -> str:
    return base64.b64encode(struct.pack(f"<{len(sig)}Q", *sig)).decode()


def unpack_signature(blob: str | None) -> list[int] | None:
    if not blob:
        return None
    try:
        raw = base64.b64decode(blob.encode())
    except Exception:
        return None
    if len(raw) % 8:
        return None
    return list(struct.unpack(f"<{len(raw) // 8}Q", raw))


# ── LSH banding ───────────────────────────────────────────────────────────────


class LSHIndex:
    """Band-based locality-sensitive index over MinHash signatures."""

    __slots__ = ("bands", "rows", "_buckets", "_sigs")

    def __init__(self, perms: int = 128, bands: int = 32) -> None:
        if perms % bands:
            raise ValueError(f"perms ({perms}) must be divisible by bands ({bands})")
        self.bands = bands
        self.rows = perms // bands
        self._buckets: list[dict[int, list[str]]] = [defaultdict(list) for _ in range(bands)]
        self._sigs: dict[str, list[int]] = {}

    def __len__(self) -> int:
        return len(self._sigs)

    def add(self, doc_id: str, signature: list[int]) -> None:
        self._sigs[doc_id] = signature
        for band in range(self.bands):
            chunk = tuple(signature[band * self.rows : (band + 1) * self.rows])
            key = hash(chunk)
            bucket = self._buckets[band][key]
            if doc_id not in bucket:
                bucket.append(doc_id)

    def candidates(self, signature: list[int], limit: int = 200) -> list[str]:
        """Documents sharing at least one band — the only ones worth comparing."""
        seen: set[str] = set()
        for band in range(self.bands):
            chunk = tuple(signature[band * self.rows : (band + 1) * self.rows])
            for doc_id in self._buckets[band].get(hash(chunk), ()):
                seen.add(doc_id)
                if len(seen) >= limit:
                    return list(seen)
        return list(seen)

    def signature_of(self, doc_id: str) -> list[int] | None:
        return self._sigs.get(doc_id)

    def top_matches(
        self, signature: list[int], limit: int = 5, exclude: str | None = None
    ) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for doc_id in self.candidates(signature, limit=max(limit * 20, 100)):
            if doc_id == exclude:
                continue
            other = self._sigs[doc_id]
            score = jaccard_estimate(signature, other)
            if score > 0:
                out.append((doc_id, score))
        out.sort(key=lambda kv: kv[1], reverse=True)
        return out[:limit]


# ── Verbatim block detection ──────────────────────────────────────────────────

BLOCK_WORDS = 40
BLOCK_WORD_STEP = 10


def block_hashes(
    tokens: list[str],
    block_words: int = BLOCK_WORDS,
    step: int = BLOCK_WORD_STEP,
    max_blocks: int = 400,
) -> set[str]:
    """Overlapping *word* blocks -> short hashes.

    Blocks are aligned to words rather than character offsets, so a copied
    passage produces identical blocks in both documents no matter how much
    original text surrounds it. That is what makes verbatim detection robust:
    a student who pastes three paragraphs into their own essay still trips it.
    """
    if not tokens:
        return set()
    if len(tokens) < block_words:
        return {hashlib.blake2b(" ".join(tokens).encode(), digest_size=8).hexdigest()[:16]}
    out: set[str] = set()
    for start in range(0, len(tokens) - block_words + 1, step):
        chunk = " ".join(tokens[start : start + block_words])
        out.add(hashlib.blake2b(chunk.encode(), digest_size=8).hexdigest()[:16])
        if len(out) >= max_blocks:
            break
    return out


def block_overlap(a: set[str], b: set[str]) -> float:
    """Fraction of `a`'s blocks present verbatim in `b`."""
    if not a:
        return 0.0
    return len(a & b) / len(a)

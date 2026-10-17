"""Stylometric features used by the heuristic AI detector.

Each feature returns a raw statistic plus a normalised ``[0, 1]`` AI-likeness
value, where 0 = "typical of a student", 1 = "typical of a language model".
The reference ranges below come from published stylometry of LLM output; they
are heuristics, not a validated classifier, and the app says so wherever the
score is shown.
"""

from __future__ import annotations

import math
import re
import zlib
from collections import Counter
from dataclasses import dataclass

from app.services.similarity.normalize import paragraphs, sentences, tokenize

# ── lexicons ──────────────────────────────────────────────────────────────────

AI_LEXICON = {
    "delve", "delves", "tapestry", "landscape", "realm", "realms", "underscore",
    "underscores", "pivotal", "nuanced", "nuance", "multifaceted", "myriad",
    "testament", "foster", "fostering", "leverage", "leveraging", "seamless",
    "seamlessly", "robust", "intricate", "showcase", "embark", "navigating",
    "everevolving", "ever-evolving", "harness", "harnessing", "paramount",
    "meticulous", "meticulously", "comprehensive", "holistic", "synergy",
    "crucially", "importantly", "notably", "significantly", "ultimately",
    "furthermore", "moreover", "additionally", "consequently", "thereby",
    "in conclusion", "it is important to note", "it is worth noting",
    "plays a crucial role", "a wide range of", "in today's world",
    "in todays world", "when it comes to", "on the other hand", "as a result",
    "ever evolving",
}

DISCOURSE_MARKERS = {
    "however", "therefore", "thus", "hence", "furthermore", "moreover",
    "additionally", "consequently", "nevertheless", "nonetheless",
    "specifically", "notably", "indeed", "overall", "finally", "firstly",
    "secondly", "lastly",
}

HEDGES = {"however", "although", "though", "while", "but", "perhaps", "maybe",
          "somewhat", "arguably", "seemingly", "possibly", "likely"}

BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
CONTRACTIONS = re.compile(
    r"\b(?:\w+n't|\w+'(?:re|ve|ll|d|s|m))\b", re.IGNORECASE
)
FIRST_PERSON = re.compile(
    r"\b(?:i|me|my|mine|myself|we|us|our|ours|ourselves)\b", re.IGNORECASE
)


@dataclass(slots=True)
class Feature:
    name: str
    value: float        # the raw statistic, for the reviewer
    score: float        # 0..1 AI-likeness
    weight: float
    note: str = ""

    @property
    def contribution(self) -> float:
        """Weighted AI-likeness — what actually enters the model score."""
        return self.score * self.weight


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _band(value: float, human: float, ai: float) -> float:
    """Map a statistic onto [0,1] between a `human` and an `ai` reference point.

    Works in both directions: if the AI reference is *lower* than the human one
    (burstiness, contractions, diversity) the scale simply runs downwards.
    """
    if ai == human:
        return 0.5
    return _clamp01((value - human) / (ai - human))


def _cv(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var) / mean


def _moving_ttr(tokens: list[str], window: int = 200) -> float:
    if len(tokens) <= window:
        return len(set(tokens)) / len(tokens) if tokens else 0.0
    scores = []
    for i in range(0, len(tokens) - window + 1, window // 2):
        chunk = tokens[i : i + window]
        scores.append(len(set(chunk)) / window)
    return sum(scores) / len(scores)


def compute(text: str) -> list[Feature]:
    toks = tokenize(text)
    n = len(toks)
    if n == 0:
        return []
    sents = sentences(text)
    paras = paragraphs(text)
    lower = " ".join(toks)
    counts = Counter(toks)

    sent_lens = [len(tokenize(s)) for s in sents] or [n]
    para_lens = [len(tokenize(p)) for p in paras] or [n]

    fourgrams = [tuple(toks[i : i + 4]) for i in range(max(0, n - 3))]
    distinct_4 = len(set(fourgrams)) / len(fourgrams) if fourgrams else 1.0

    hapax = sum(1 for w, c in counts.items() if c == 1) / n
    ttr = _moving_ttr(toks)

    ai_hits = sum(lower.count(term) for term in AI_LEXICON if " " not in term)
    ai_hits += sum(lower.count(term) for term in AI_LEXICON if " " in term)
    ai_density = ai_hits / max(1, n) * 100

    dm_hits = sum(counts.get(m, 0) for m in DISCOURSE_MARKERS)
    dm_density = dm_hits / max(1, n) * 100

    hedge_density = sum(counts.get(h, 0) for h in HEDGES) / max(1, n) * 100

    contractions = len(CONTRACTIONS.findall(text)) / max(1, n) * 100
    first_person = len(FIRST_PERSON.findall(text)) / max(1, n) * 100

    commas = text.count(",") / max(1, len(sents))
    semis = text.count(";") / max(1, len(sents))
    dashes = (text.count("—") + text.count(" - ")) / max(1, len(sents))
    quotes = (text.count('"') + text.count("'")) / max(1, n) * 100

    lines = [ln for ln in text.splitlines() if ln.strip()]
    bullets = sum(1 for ln in lines if BULLET.match(ln))
    list_density = bullets / max(1, len(lines))

    openers = [s.split()[0].lower() for s in sents if s.split()]
    opener_diversity = len(set(openers)) / len(openers) if openers else 1.0

    ratio = len(zlib.compress(lower.encode(), 6)) / max(1, len(lower.encode()))

    features = [
        # Humans vary sentence length a lot; LLM output is remarkably even.
        Feature("sentence_length_burstiness", _cv(sent_lens), _band(_cv(sent_lens), 0.75, 0.30),
                1.6, "std/mean of sentence length"),
        Feature("paragraph_length_burstiness", _cv(para_lens), _band(_cv(para_lens), 0.80, 0.35),
                1.1, "std/mean of paragraph length"),
        Feature("mean_sentence_length", sum(sent_lens) / len(sent_lens),
                _band(sum(sent_lens) / len(sent_lens), 17, 25), 0.9,
                "words per sentence"),
        # Lexical variety.
        Feature(
            "lexical_diversity_ttr", ttr, _band(ttr, 0.72, 0.52), 1.3,
            "moving type/token ratio",
        ),
        Feature("hapax_ratio", hapax, _band(hapax, 0.55, 0.38), 1.0,
                "share of words used exactly once"),
        # Repetition of structure.
        Feature("repeated_4gram_ratio", 1 - distinct_4, _band(1 - distinct_4, 0.05, 0.22), 1.2,
                "share of 4-word sequences that repeat"),
        Feature("sentence_opener_diversity", opener_diversity,
                _band(opener_diversity, 0.90, 0.62), 0.8, "distinct first words of sentences"),
        # Register markers.
        Feature("ai_lexicon_density", ai_density, _band(ai_density, 0.3, 2.2), 1.7,
                "LLM-favoured vocabulary per 100 words"),
        Feature("discourse_marker_density", dm_density, _band(dm_density, 1.0, 4.0), 1.1,
                "connectives per 100 words"),
        Feature("hedge_density", hedge_density, _band(hedge_density, 2.2, 0.6), 0.9,
                "hedges per 100 words"),
        Feature("contraction_density", contractions, _band(contractions, 2.0, 0.2), 1.0,
                "contractions per 100 words"),
        Feature("first_person_density", first_person, _band(first_person, 1.6, 0.25), 0.8,
                "first-person tokens per 100 words"),
        Feature("mean_word_length", sum(len(t) for t in toks) / n,
                _band(sum(len(t) for t in toks) / n, 4.6, 5.5), 0.5, "characters per word"),
        # Punctuation habits.
        Feature("commas_per_sentence", commas, _band(commas, 1.1, 2.2), 0.6, "commas per sentence"),
        Feature("semicolons_per_sentence", semis, _band(semis, 0.02, 0.16), 0.5,
                "semicolons per sentence"),
        Feature("dash_rate", dashes, _band(dashes, 0.15, 0.55), 0.4, "em/en dashes per sentence"),
        Feature(
            "quote_density", quotes, _band(quotes, 0.6, 0.15), 0.3,
            "quote marks per 100 words",
        ),
        # Structure.
        Feature("list_density", list_density, _band(list_density, 0.02, 0.20), 0.6,
                "share of lines that are bullets"),
        Feature("compression_ratio", ratio, _band(ratio, 0.42, 0.36), 0.3,
                "zlib ratio of normalised text"),
    ]
    return features

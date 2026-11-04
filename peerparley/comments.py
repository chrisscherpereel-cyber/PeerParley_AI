"""Comment support score Q (0..1).

Does the written feedback actually substantiate the numeric allocation? The
score weighs how much was written, whether it names anything concrete, whether
it reads as considered prose, and whether it is varied rather than a repeated
phrase — then penalises repetition within a comment and the same text pasted
for several teammates. It drives a Points column = round(Q * max_points), and
it dampens the grade adjustment, so its resolution matters: a score that cannot
tell two comments apart silently flattens the whole section's grades, which is
exactly what the earlier threshold-counting version did.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Iterable, List

# Words that name something a teammate actually did. The point is to tell
# "rebuilt the financial model twice" apart from "good teammate", so the list
# covers the nouns of student project work as well as the verbs — it had
# neither "model" nor any way to match "rebuilt", and scored that comment zero
# for specificity.
CONTRIBUTION_KEYWORDS = {
    "contribut", "led", "lead", "organiz", "organis", "wrote", "writ", "coded",
    "designed", "research", "present", "slide", "deck", "test", "debug",
    "meeting", "deadline", "helped", "coordinat", "manage", "review", "analy",
    "built", "build", "created", "communicat", "responsib", "delivered",
    "supported", "collaborat", "model", "spreadsheet", "data", "draft",
    "edit", "chart", "graph", "schedule", "timeline", "interview", "survey",
    "document", "report", "prepar", "attend", "plan", "budget", "calculat",
    "format", "cite", "source", "rehears", "practice", "practis", "revis",
    "summar", "structur", "section", "task", "deliverable", "fix", "finance",
    "financial", "market", "compet", "transcri", "note", "proof", "assembl",
}

# Stems short enough that a substring match would fire inside unrelated words
# ("led" inside "settled", "fix" inside "prefix"), so they must match from the
# start of a word.
_PREFIX_ONLY = {k for k in CONTRIBUTION_KEYWORDS if len(k) <= 4}

_WORD = re.compile(r"[a-zA-Z']+")
_SENT = re.compile(r"[.!?]+")


@dataclass
class CommentScore:
    q: float
    points: int
    word_count: int
    char_count: int
    sentence_count: int
    unique_ratio: float
    keyword_count: int
    flags: List[str]


def _tokens(text: str) -> List[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def _ramp(value: float, full: float) -> float:
    """0 at nothing, 1 at `full`, proportional in between.

    The whole point of this function is that it is not a threshold: the old
    scoring gave a nineteen-word comment exactly as much credit as a one-word
    one, because both fell short of 25 words.
    """
    if full <= 0:
        return 1.0
    return max(0.0, min(1.0, float(value) / float(full)))


@lru_cache(maxsize=200_000)
def _similarity_cached(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _similarity(a: str, b: str) -> float:
    """Similarity between two comments, computed once per pair.

    Similarity is symmetric, but the grader asks for it from both ends: once
    while scoring the comments a student *received* and again while scoring the
    comments they *wrote*. For 40 students in teams of five that was 5,120
    SequenceMatcher calls per regrade over roughly 1,500 distinct pairs, and it
    accounted for almost all of the time spent grading (measured 4.3s → 1.3s for
    teams of five, 45s → 12s for teams of ten).

    Ordering the pair makes the two directions one cache entry. The cache is
    keyed on the comment text itself, so it stays correct across sections and
    reruns; it is bounded, and it holds student comments only in memory, never
    on disk.
    """
    return _similarity_cached(*(a, b) if a <= b else (b, a))


def score_comment(
    text: str,
    others: Iterable[str] = (),
    max_points: int = 5,
) -> CommentScore:
    text = (text or "").strip()
    words = _tokens(text)
    wc = len(words)
    cc = len(text)
    sc = len([s for s in _SENT.split(text) if s.strip()])
    unique_ratio = (len(set(words)) / wc) if wc else 0.0
    # Longer stems match anywhere in the word, so "rebuilt" counts through
    # "built" and "rewrote" through "wrote"; short ones must start it.
    kw = sum(1 for w in words
             if any(w.startswith(k) if k in _PREFIX_ONLY else k in w
                    for k in CONTRIBUTION_KEYWORDS))

    flags: List[str] = []

    # ---- the score ------------------------------------------------------
    #
    # This used to be `passed / 5` over five all-or-nothing thresholds (25+
    # words, 150+ characters, 2+ sentences, enough variety, 2+ keywords), which
    # meant it could only ever return six values — and measured on ten real
    # comments, *nine* of them returned exactly 0.20. A one-word "fine" scored
    # the same as a thoughtful nineteen-word sentence, because a single word
    # trivially passes the variety check.
    #
    # That made the instructor summary's Q and Resp.Pts columns constants, and
    # because Q multiplies the grade adjustment it also removed about four
    # fifths of every adjustment, flattening Mult and Grade Δ for the whole
    # section. One coarse function, four dead columns.
    #
    # So each component is now a ramp rather than a cliff, and they are weighted
    # by how much they actually say about whether a comment substantiates a
    # rating: how much was written, whether it names anything concrete, and
    # whether it is varied prose rather than a repeated phrase. The result is
    # continuous over 0..1, and a longer, more specific comment always scores
    # above a shorter, vaguer one.
    if wc == 0:
        q = 0.0
        flags.append("no comment")
    else:
        # Substance: words, with full credit at 30 and real credit along the
        # way. `_ramp` is linear so that twice the writing is twice the score
        # up to the cap, rather than nothing at all until a threshold.
        substance = _ramp(wc, 30) * (0.25 + 0.75 * _ramp(unique_ratio, 0.5))
        # Specificity: does it name what the teammate did? Two keyword hits is
        # a solid comment; more adds a little.
        specific = _ramp(kw, 3)
        # Shape: more than one sentence suggests a considered answer.
        shape = _ramp(sc, 2)
        # Variety: only meaningful once there is something to vary. Below six
        # words this is not evidence of anything, so it does not count — which
        # is what let "fine" score 0.2.
        variety = unique_ratio if wc >= 6 else 0.0

        q = (0.45 * substance + 0.30 * specific + 0.10 * shape
             + 0.15 * variety)

        if wc < 6:
            flags.append("very short")
        if kw == 0:
            flags.append("nothing specific named")
        if sc < 2:
            flags.append("single sentence")

    # Repetition penalty (the same phrase over and over).
    if wc >= 6 and unique_ratio < 0.35:
        q *= 0.7
        flags.append("high repetition")

    # Cross-comment similarity: the same text pasted for several teammates.
    max_sim = 0.0
    for o in others:
        if not o or o.strip() == text:
            continue
        sim = _similarity(text, o.strip())
        max_sim = max(max_sim, sim)
    if max_sim >= 0.85 and wc:
        q *= 0.6
        flags.append(f"duplicate of another comment ({max_sim:.0%})")

    q = max(0.0, min(1.0, q))

    return CommentScore(
        q=round(q, 3),
        points=round(q * max_points),
        word_count=wc,
        char_count=cc,
        sentence_count=sc,
        unique_ratio=round(unique_ratio, 3),
        keyword_count=kw,
        flags=flags,
    )

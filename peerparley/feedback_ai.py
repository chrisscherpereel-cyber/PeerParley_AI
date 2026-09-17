"""AI-assisted narrative feedback, grounded in what teammates actually wrote.

This is the v2 feature. PeerParley already collects written peer comments and
already prints them as bullets in each student's PDF. Bullets are honest but
rough: they are the raw words of five hurried teammates, often terse, often
repetitive, sometimes blunter than the writer intended. This module offers the
instructor a readable narrative built from those same bullets — and nothing
else.

The pipeline, per student:

    1. ``build_source`` gathers the evidence: the written comments, plus the
       numeric ratings if the instructor chose to share them. This is the
       complete universe the model is allowed to draw on, and it is assembled
       here rather than in the prompt so that the verifier can be handed the
       identical set.

    2. ``generate_draft`` asks for a narrative in which every point carries the
       verbatim comment behind it.

    3. ``check_quotes`` verifies those citations *deterministically*, with no
       API call: a quote that does not appear in the evidence was fabricated,
       and no amount of model self-assessment is a better test than string
       matching. This catches the specific failure that matters most — a
       recommendation dressed in a quotation that was never said.

    4. ``verify_draft`` (optional, on by default) asks a fresh context to audit
       the finished prose against the evidence and name anything that goes
       beyond it. This catches what string matching cannot: a real quote
       attached to a point it does not support, invented causes, generic advice
       smuggled into a paragraph.

    5. The instructor reads, edits, and approves. Nothing reaches a student
       otherwise. Steps 3 and 4 exist to make that review fast and to tell the
       instructor where to look — not to remove them from the loop.

Confidential comments are deliberately never included. They are written to the
instructor, and a narrative that echoes one back to the student would leak it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import ai_prompts
from .aiconfig import AISettings, get_provider
from .grading import StudentResult, TeamResult
from .llm import (
    AuthLLMError,
    LLMClient,
    LLMError,
    TruncatedResponseError,
    Usage,
    readable_fragment,
    salvage_object_fields,
    salvage_partial_strings,
)

# Ceiling for the automatic retry after a truncated reply. Generous, because
# you pay for tokens produced rather than for the cap, and a second truncation
# wastes the whole retry.
MAX_RETRY_TOKENS = 12000

# Narrative fields that hold prose, in the order they read. Used by the string
# salvage so a reply cut mid-paragraph still yields something.
NARRATIVE_KEYS = ("strengths", "focus", "disagreement")

# How much of a cited quote must appear in the evidence for the citation to
# count. Models normalize whitespace, fix a typo, or trim a trailing clause when
# copying, and failing a citation for that would cry wolf constantly. Below this
# the quote is not a copy of anything the student's teammates wrote.
QUOTE_MATCH_THRESHOLD = 0.80

# A citation shorter than this is not evidence of anything — "good" appears in
# almost any comment pool — so it is treated as missing rather than matched.
MIN_QUOTE_CHARS = 12

DIMENSION_LABELS = ("Team player", "Quantity of work", "Quality of work",
                    "Effect on team")

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Lowercase, collapse whitespace, drop punctuation that copying mangles."""
    text = (text or "").lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return _WS.sub(" ", text).strip()


def _is_nan(x: Any) -> bool:
    try:
        return x != x
    except Exception:
        return False


def _clean(items: Iterable[str]) -> List[str]:
    """Non-empty, de-duplicated, order-preserving."""
    out: List[str] = []
    seen = set()
    for raw in items or []:
        text = (raw or "").strip()
        if not text:
            continue
        keyed = _norm(text)
        if not keyed or keyed in seen:
            continue
        seen.add(keyed)
        out.append(text)
    return out


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #

@dataclass
class FeedbackSource:
    """Everything the model is allowed to see about one student.

    Assembled once and handed to both the generator and the verifier, so the
    audit is against exactly the evidence the draft was written from. Built
    without any reference to confidential comments — see the module docstring.
    """

    name: str
    team: str
    key: str
    valued: List[str] = field(default_factory=list)
    focus: List[str] = field(default_factory=list)
    other: List[str] = field(default_factory=list)
    ratings: List[Tuple[str, float, str]] = field(default_factory=list)
    performance: str = ""

    @property
    def comments(self) -> List[str]:
        return [*self.valued, *self.focus, *self.other]

    @property
    def comment_count(self) -> int:
        return len(self.comments)

    def has_material(self) -> bool:
        """Is there enough here to be worth an API call?

        Two short comments can produce an honest three-sentence narrative. One
        comment of four words cannot produce anything a bullet would not say
        better, and asking anyway invites the model to fill the gap — which is
        the exact failure this feature is built to avoid.
        """
        words = sum(len(c.split()) for c in self.comments)
        return self.comment_count >= 2 and words >= 15

    def evidence_text(self) -> str:
        """The comment pool as the verifier sees it."""
        lines: List[str] = []
        for label, items in (("VALUED", self.valued),
                             ("FOCUS", self.focus),
                             ("OTHER", self.other)):
            for item in items:
                lines.append(f"[{label}] {item}")
        return "\n".join(lines) if lines else "(no written comments)"


def build_source(m: StudentResult, include_ratings: bool = True) -> FeedbackSource:
    """Gather one student's evidence from their computed result."""
    valued = _clean(m.contributions)
    focus = _clean(m.improvements)
    # public_comments overlaps contributions in some survey shapes, so anything
    # already captured above is dropped rather than shown to the model twice —
    # duplicated evidence is what makes a model write "several teammates".
    seen = {_norm(c) for c in (*valued, *focus)}
    other = [c for c in _clean(m.public_comments) if _norm(c) not in seen]

    ratings: List[Tuple[str, float, str]] = []
    performance = ""
    if include_ratings:
        letters = (m.team_player, m.quantity, m.quality, m.effect)
        for label, value, letter in zip(DIMENSION_LABELS, m.peer_vals, letters):
            if not _is_nan(value):
                ratings.append((label, float(value), str(letter or "")))
        performance = str(m.performance or "")

    return FeedbackSource(
        name=m.name, team=m.team, key=m.key,
        valued=valued, focus=focus, other=other,
        ratings=ratings, performance=performance,
    )


# --------------------------------------------------------------------------- #
# Drafts
# --------------------------------------------------------------------------- #

@dataclass
class Point:
    """One claim, with the comment it came from and whether that checks out."""

    point: str
    source: str
    raised_by: int = 1
    quote_ok: bool = True
    quote_match: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "point": self.point, "source": self.source,
            "raised_by": self.raised_by, "quote_ok": self.quote_ok,
            "quote_match": round(self.quote_match, 3),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Point":
        return cls(
            point=str(d.get("point", "")),
            source=str(d.get("source", "")),
            raised_by=int(d.get("raised_by", 1) or 1),
            quote_ok=bool(d.get("quote_ok", True)),
            quote_match=float(d.get("quote_match", 1.0) or 0.0),
        )


@dataclass
class Flag:
    """One problem found in a draft, by the quote check or the verifier."""

    text: str
    problem: str
    severity: str = "high"
    origin: str = "verifier"  # verifier | quote-check

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text, "problem": self.problem,
            "severity": self.severity, "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Flag":
        return cls(
            text=str(d.get("text", "")),
            problem=str(d.get("problem", "")),
            severity=str(d.get("severity", "high")),
            origin=str(d.get("origin", "verifier")),
        )


@dataclass
class Draft:
    """One student's generated feedback, its checks, and its approval state."""

    key: str
    name: str
    team: str
    strengths: str = ""
    focus: str = ""
    disagreement: str = ""
    insufficient_evidence: str = ""
    strength_points: List[Point] = field(default_factory=list)
    focus_points: List[Point] = field(default_factory=list)

    # Checks
    score: float = 1.0
    flags: List[Flag] = field(default_factory=list)
    verified: bool = False          # did the grounding pass actually run
    quotes_checked: bool = False

    # Instructor state. `edited` holds their rewrite; empty means "as generated".
    edited: str = ""
    approved: bool = False

    # Bookkeeping
    model: str = ""
    provider: str = ""
    error: str = ""
    truncated: bool = False
    usage: Usage = field(default_factory=Usage)
    # Whatever arrived before a cut, kept even when nothing could be parsed out
    # of it. Seven thousand characters of readable prose is not a failure to be
    # discarded — it is a draft in the wrong shape, and the instructor can see
    # it and use it.
    raw_partial: str = ""

    # ---------------------------------------------------------------- #

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def high_severity(self) -> List[Flag]:
        return [f for f in self.flags if f.severity == "high"]

    @property
    def clean(self) -> bool:
        """Safe to approve without a close read?

        Deliberately strict: no flag of any severity, a passing score, and —
        when the instructor asked for verification — evidence that it actually
        ran. A draft that was never audited is not a clean draft; it is an
        unaudited one, and the bulk-approve action must not treat the two alike.
        """
        return (self.ok and not self.flags and self.score >= 0.999
                and not self.empty)

    def text(self) -> str:
        """The narrative as it would appear in the PDF."""
        if self.edited.strip():
            return self.edited.strip()
        if self.insufficient_evidence.strip():
            return ""
        parts = [p.strip() for p in (self.strengths, self.focus,
                                     self.disagreement) if p and p.strip()]
        return "\n\n".join(parts)

    def sentences(self) -> List[str]:
        return [s.strip() for s in _SENT_SPLIT.split(self.text()) if s.strip()]

    @property
    def empty(self) -> bool:
        """Succeeded as a request, produced no narrative.

        A distinct state from both "failed" and "too thin to write": the call
        came back, so nothing errored, but every narrative field was blank. It
        used to surface as "1 minor flag(s)" beside an empty text box, which
        reads as though the draft were fine — the single most confusing thing
        the panel could say.
        """
        return (self.ok and not self.insufficient_evidence
                and not self.text().strip())

    def summary(self) -> str:
        """One-line status for the review table."""
        if self.error:
            return f"⚠ {self.error[:80]}"
        if self.insufficient_evidence:
            return "Not enough comments to write from"
        if self.empty:
            return ("⚠ came back empty — retry this student"
                    + (" (partial text kept below)" if self.raw_partial else ""))
        if not self.flags:
            return "Grounded" if self.verified else "Generated (not verified)"
        high = len(self.high_severity)
        if high:
            return f"⚠ {high} unsupported claim(s)"
        return f"{len(self.flags)} minor flag(s)"

    def to_dict(self) -> Dict[str, Any]:
        """Serializable form, for the audit trail saved into the vault bundle."""
        return {
            "key": self.key, "name": self.name, "team": self.team,
            "strengths": self.strengths, "focus": self.focus,
            "disagreement": self.disagreement,
            "insufficient_evidence": self.insufficient_evidence,
            "strength_points": [p.to_dict() for p in self.strength_points],
            "focus_points": [p.to_dict() for p in self.focus_points],
            "score": round(self.score, 3),
            "flags": [f.to_dict() for f in self.flags],
            "verified": self.verified, "quotes_checked": self.quotes_checked,
            "edited": self.edited, "approved": self.approved,
            "model": self.model, "provider": self.provider,
            "error": self.error, "truncated": self.truncated,
            "raw_partial": self.raw_partial,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "calls": self.usage.calls,
            },
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Draft":
        """Rebuild a Draft saved to the vault.

        The exact inverse of to_dict, including the instructor's edits and
        approvals — the whole point of saving is that a session ending does not
        undo an afternoon of review.
        """
        u = d.get("usage") or {}
        return cls(
            key=str(d.get("key", "")), name=str(d.get("name", "")),
            team=str(d.get("team", "")),
            strengths=str(d.get("strengths", "") or ""),
            focus=str(d.get("focus", "") or ""),
            disagreement=str(d.get("disagreement", "") or ""),
            insufficient_evidence=str(d.get("insufficient_evidence", "") or ""),
            strength_points=[Point.from_dict(x)
                             for x in (d.get("strength_points") or [])
                             if isinstance(x, dict)],
            focus_points=[Point.from_dict(x)
                          for x in (d.get("focus_points") or [])
                          if isinstance(x, dict)],
            score=float(d.get("score", 1.0) or 0.0),
            flags=[Flag.from_dict(x) for x in (d.get("flags") or [])
                   if isinstance(x, dict)],
            verified=bool(d.get("verified", False)),
            quotes_checked=bool(d.get("quotes_checked", False)),
            edited=str(d.get("edited", "") or ""),
            approved=bool(d.get("approved", False)),
            model=str(d.get("model", "") or ""),
            provider=str(d.get("provider", "") or ""),
            error=str(d.get("error", "") or ""),
            truncated=bool(d.get("truncated", False)),
            raw_partial=str(d.get("raw_partial", "") or ""),
            usage=Usage(int(u.get("input_tokens", 0) or 0),
                        int(u.get("output_tokens", 0) or 0),
                        int(u.get("calls", 0) or 0)),
        )


# --------------------------------------------------------------------------- #
# Prompt assembly
# --------------------------------------------------------------------------- #

def _bullet_list(items: Sequence[str], empty: str) -> str:
    if not items:
        return empty
    return "\n".join(f"- {t}" for t in items)


def build_user_prompt(src: FeedbackSource, settings: AISettings) -> str:
    ratings_block = ""
    if src.ratings:
        rows = "\n".join(
            f"- {label}: {value:.2f} of 4" + (f" ({letter})" if letter else "")
            for label, value, letter in src.ratings
        )
        perf = (f"Overall performance label the instructor's rules produced: "
                f"{src.performance}" if src.performance else "")
        ratings_block = ai_prompts.RATINGS_BLOCK.format(
            rows=rows, performance=perf,
            example=src.ratings[0][0].lower(),
        )

    other_block = ""
    if src.other:
        other_block = ai_prompts.OTHER_BLOCK.format(
            n_other=len(src.other), other=_bullet_list(src.other, "(none)")
        )

    guidance_block = ""
    if settings.extra_guidance.strip():
        guidance_block = ai_prompts.GUIDANCE_BLOCK.format(
            guidance=settings.extra_guidance.strip()
        )

    return ai_prompts.NARRATIVE_USER.format(
        ratings_block=ratings_block,
        n_valued=len(src.valued),
        valued=_bullet_list(src.valued, "(no comments in this category)"),
        n_focus=len(src.focus),
        focus=_bullet_list(src.focus, "(no comments in this category)"),
        other_block=other_block,
        guidance_block=guidance_block,
        target_words=settings.target_words,
    )


# --------------------------------------------------------------------------- #
# Deterministic quote check
# --------------------------------------------------------------------------- #

def _best_match(quote: str, pool: Sequence[str]) -> float:
    """How well a cited quote matches anything in the evidence, 0..1.

    Substring containment first, because an exact copy is the normal case and
    should cost nothing. Otherwise the fraction of the quote's words that appear
    in the single best-matching comment — word overlap rather than character
    similarity, since a model that trims or reorders a clause is still citing
    that comment, while one that invented a sentence shares only stopwords with
    everything.
    """
    q = _norm(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return 0.0
    best = 0.0
    q_words = q.split()
    if not q_words:
        return 0.0
    for candidate in pool:
        c = _norm(candidate)
        if not c:
            continue
        if q in c or c in q:
            return 1.0
        c_words = set(c.split())
        hit = sum(1 for w in q_words if w in c_words) / len(q_words)
        best = max(best, hit)
    return best


def check_quotes(draft: Draft, src: FeedbackSource) -> None:
    """Validate every citation against the evidence, in place. No API call.

    A fabricated quote is the highest-value thing to catch and the cheapest: a
    recommendation the model made up, then attributed to a teammate, is both the
    worst outcome for the student and perfectly detectable by string matching.
    """
    pool = src.comments
    draft.quotes_checked = True
    for point in (*draft.strength_points, *draft.focus_points):
        match = _best_match(point.source, pool)
        point.quote_match = match
        point.quote_ok = match >= QUOTE_MATCH_THRESHOLD
        if not point.quote_ok:
            draft.flags.append(Flag(
                text=point.point,
                problem=(
                    "cites a comment that does not appear in this student's "
                    "feedback" if match < 0.4 else
                    f"citation only loosely matches any real comment "
                    f"({match:.0%} overlap)"
                ),
                severity="high" if match < 0.4 else "low",
                origin="quote-check",
            ))
        # An honest citation can still be over-claimed: one comment cannot have
        # been raised by three teammates.
        if point.raised_by > 1:
            supporting = sum(
                1 for c in pool if _best_match(point.source, [c]) >= 0.6
            )
            if point.raised_by > max(supporting, 1):
                draft.flags.append(Flag(
                    text=point.point,
                    problem=(f"claims {point.raised_by} teammates raised this; "
                             f"{supporting or 1} comment(s) support it"),
                    severity="low",
                    origin="quote-check",
                ))


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

def _points(raw: Any) -> List[Point]:
    out: List[Point] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("point", "") or "").strip()
        if not text:
            continue
        try:
            raised = int(item.get("raised_by", 1) or 1)
        except (TypeError, ValueError):
            raised = 1
        out.append(Point(
            point=text,
            source=str(item.get("source", "") or "").strip(),
            raised_by=max(1, raised),
        ))
    return out


def _draft_from_payload(
    payload: Dict[str, Any], src: FeedbackSource, settings: AISettings
) -> Draft:
    return Draft(
        key=src.key, name=src.name, team=src.team,
        strengths=str(payload.get("strengths", "") or "").strip(),
        focus=str(payload.get("focus", "") or "").strip(),
        disagreement=str(payload.get("disagreement", "") or "").strip(),
        insufficient_evidence=str(
            payload.get("insufficient_evidence", "") or ""
        ).strip(),
        strength_points=_points(payload.get("strength_points")),
        focus_points=_points(payload.get("focus_points")),
        model=settings.model, provider=settings.provider,
    )


def generate_draft(
    client: LLMClient, src: FeedbackSource, settings: AISettings
) -> Draft:
    """One student: generate, check citations, optionally audit."""
    if not src.has_material():
        return Draft(
            key=src.key, name=src.name, team=src.team,
            insufficient_evidence=(
                f"Only {src.comment_count} written comment(s) were submitted for "
                "this student — too little to write a narrative from. The raw "
                "comments still appear in the PDF."
            ),
            model=settings.model, provider=settings.provider,
        )

    system = ai_prompts.narrative_system(settings.tone)
    user = build_user_prompt(src, settings)
    before = Usage(client.usage.input_tokens, client.usage.output_tokens,
                   client.usage.calls)

    # A truncated reply is recovered through four levels, in descending order
    # of fidelity, because a reply cut off is not a reply lost:
    #
    #   1. Ask again with a bigger ceiling. This is the only level that yields a
    #      *complete* draft, so it is tried first — and it is what an instructor
    #      would otherwise do by hand, once per student.
    #   2. Structural salvage: the top-level fields that closed cleanly.
    #   3. String salvage: prose recovered from a field cut mid-sentence. This is
    #      the one that matters most in practice, because a rambling model hits
    #      the cap inside its first paragraph, where structural salvage finds
    #      nothing and thousands of readable characters are at stake.
    #   4. A readable fragment of whatever arrived, kept on the draft so the
    #      instructor can see and use it even when no parser could.
    truncated = False
    partial_raw = ""
    tokens = settings.max_tokens
    attempts = 1 + (1 if settings.retry_truncated else 0)
    payload: Optional[Dict[str, Any]] = None
    last_cut: Optional[TruncatedResponseError] = None

    for attempt in range(attempts):
        try:
            payload = client.complete_json(system, user, max_tokens=tokens)
            # A retry that came back whole produced a complete draft, so the
            # earlier cut is history: it must not be flagged as incomplete or
            # carry a stale partial. That is the whole point of retrying.
            truncated = False
            partial_raw = ""
            break
        except AuthLLMError:
            # Not this student's problem, and not survivable by moving to the
            # next one. Let it out so the batch can stop at the first occurrence.
            raise
        except TruncatedResponseError as exc:
            truncated = True
            last_cut = exc
            partial_raw = exc.raw or partial_raw
            payload = None
            if attempt + 1 < attempts:
                # Retry once, with real headroom rather than a nudge: a model
                # that overran 4k by a little will overrun 4.5k too.
                tokens = min(max(tokens * 3, 6000), MAX_RETRY_TOKENS)
                continue
            break
        except LLMError as exc:
            return _failed(src, settings, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            return _failed(src, settings, f"Unexpected error: {exc}")

    if payload is None:
        # The retry (if any) was also cut. Work down the recovery ladder.
        payload = salvage_object_fields(partial_raw)
        recovered_by = "the fields that finished" if payload else ""
        if not payload:
            strings = salvage_partial_strings(partial_raw, NARRATIVE_KEYS)
            if strings:
                payload = dict(strings)
                recovered_by = "prose recovered from the cut-off reply"
        if not payload:
            fragment = readable_fragment(partial_raw)
            if fragment:
                payload = {"strengths": fragment}
                recovered_by = "readable text pulled from an unparseable reply"
        if not payload:
            failed = _failed(src, settings,
                             str(last_cut) if last_cut else "Reply was cut off.",
                             truncated=True)
            failed.raw_partial = partial_raw
            return failed

    draft = _draft_from_payload(payload, src, settings)
    draft.truncated = truncated
    draft.raw_partial = partial_raw if truncated else ""
    draft.usage = Usage(
        client.usage.input_tokens - before.input_tokens,
        client.usage.output_tokens - before.output_tokens,
        client.usage.calls - before.calls,
    )
    if truncated:
        detail = ("the model's reply was cut off twice, even with a raised token "
                  "cap" if attempts > 1 else "the model's reply was cut off")
        if partial_raw:
            detail += f"; {len(partial_raw)} characters arrived"
        draft.flags.append(Flag(
            text="(whole draft)",
            problem=(f"{detail} — this draft is incomplete and needs reading "
                     "against the comments"),
            severity="low", origin="quote-check",
        ))

    check_quotes(draft, src)

    if draft.empty:
        # The call succeeded and produced nothing. Flag it explicitly rather
        # than letting it sit in the panel looking finished.
        draft.flags.append(Flag(
            text="(whole draft)",
            problem=("the model returned no narrative text at all — retry this "
                     "student, or pin a stronger model"),
            severity="high", origin="quote-check",
        ))
        draft.score = 0.0
        return draft

    if settings.verify:
        verify_draft(client, draft, src)
    else:
        # No audit ran, so the score is not a judgement — only the citation
        # check stands behind this draft. Reflect that rather than reporting a
        # confident 1.0 nobody earned.
        draft.score = 1.0 if not draft.flags else 0.5

    return draft


def _failed(src: FeedbackSource, settings: AISettings, message: str,
            truncated: bool = False) -> Draft:
    return Draft(
        key=src.key, name=src.name, team=src.team,
        error=message, truncated=truncated, score=0.0,
        model=settings.model, provider=settings.provider,
    )


# --------------------------------------------------------------------------- #
# Grounding audit
# --------------------------------------------------------------------------- #

def verify_draft(client: LLMClient, draft: Draft, src: FeedbackSource) -> None:
    """Second pass: audit the finished prose against the evidence, in place."""
    ratings_note = ""
    if src.ratings:
        rows = ", ".join(f"{label} {value:.2f}/4"
                         for label, value, _ in src.ratings)
        ratings_note = (
            f"\nThe draft was also given these numeric ratings as context: "
            f"{rows}. It was told it may refer to them only in general terms "
            f"and only where a written comment also supports the point, and "
            f"never to quote the figures or the performance label. Flag any "
            f"sentence that reports a number, a letter grade, or a ranking.\n"
        )

    user = ai_prompts.VERIFY_USER.format(
        evidence=src.evidence_text(),
        ratings_note=ratings_note,
        draft=draft.text(),
    )
    before = Usage(client.usage.input_tokens, client.usage.output_tokens,
                   client.usage.calls)
    try:
        payload = client.complete_json(
            ai_prompts.VERIFY_SYSTEM, user, max_tokens=1200
        )
    except AuthLLMError:
        raise
    except Exception as exc:
        # A failed audit is not a passed audit. Say the check did not run and
        # leave the draft for a human read rather than quietly blessing it.
        draft.verified = False
        draft.score = min(draft.score, 0.5)
        draft.flags.append(Flag(
            text="(whole draft)",
            problem=f"grounding check could not run ({exc}) — read this one closely",
            severity="low", origin="verifier",
        ))
        return

    draft.usage.add(Usage(
        client.usage.input_tokens - before.input_tokens,
        client.usage.output_tokens - before.output_tokens,
        client.usage.calls - before.calls,
    ))
    draft.verified = True

    for item in payload.get("unsupported") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "") or "").strip()
        problem = str(item.get("problem", "") or "unsupported claim").strip()
        if not text and not problem:
            continue
        severity = str(item.get("severity", "high") or "high").lower()
        draft.flags.append(Flag(
            text=text or "(unspecified sentence)",
            problem=problem,
            severity="high" if severity not in ("low", "medium") else "low",
            origin="verifier",
        ))

    try:
        score = float(payload.get("score", 1.0))
    except (TypeError, ValueError):
        score = 1.0
    # The verifier's own score is advisory; a flag it raised outranks it, and so
    # does a citation the quote check already rejected.
    if draft.flags:
        score = min(score, 0.99)
    if draft.high_severity:
        score = min(score, 0.5)
    draft.score = max(0.0, min(1.0, score))


# --------------------------------------------------------------------------- #
# Batch
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
#
# Drafts used to live only in st.session_state, which meant signing out — or
# Streamlit recycling an idle session — threw away an afternoon of review. They
# are worth keeping: each one cost an API call, and the edits and approvals on
# top of them cost the instructor's judgement, which is the expensive part.
#
# They go to the same encrypted vault as everything else rather than to a local
# file, because a draft contains a student's name and their teammates' comments
# about them. Vault.put_bytes Fernet-encrypts before the bytes leave the
# process, so the storage provider holds ciphertext — the same guarantee the
# rest of PeerParley makes. Saving is always best-effort: a vault hiccup must
# never cost the in-memory work it was trying to protect.

DRAFTS_VERSION = 1


def drafts_key(slug: str) -> str:
    """Vault object name for one survey's drafts."""
    return f"aidrafts__{slug}.json"


def drafts_payload(drafts: Dict[str, Draft],
                   meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "version": DRAFTS_VERSION,
        "meta": dict(meta or {}),
        "drafts": [d.to_dict() for d in (drafts or {}).values()],
    }


def drafts_from_payload(payload: Dict[str, Any]) -> Dict[str, Draft]:
    out: Dict[str, Draft] = {}
    for item in (payload or {}).get("drafts") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if key:
            out[key] = Draft.from_dict(item)
    return out


def save_drafts(vault: Any, slug: str, drafts: Dict[str, Draft],
                meta: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """Write drafts to the vault, encrypted. Returns (saved, error message).

    Never raises: the caller is mid-review, and losing the panel to a storage
    error would be a worse outcome than a stale save.
    """
    if not slug:
        return False, "No survey selected, so there is nowhere to save."
    try:
        import json as _json
        vault.put_bytes(
            drafts_key(slug),
            _json.dumps(drafts_payload(drafts, meta), default=str,
                        ensure_ascii=False).encode("utf-8"),
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def load_drafts(vault: Any, slug: str) -> Tuple[Dict[str, Draft], str]:
    """Read drafts back. Returns ({}, "") when none are stored yet.

    A missing object is the normal first-run case and not an error, so it comes
    back empty rather than noisy. A *corrupt* or undecryptable one does report,
    because that usually means the Fernet key changed and the instructor needs
    to know their saved review is unreadable rather than absent.
    """
    if not slug:
        return {}, ""
    try:
        raw = vault.get_bytes(drafts_key(slug))
    except Exception:
        return {}, ""          # not saved yet
    try:
        import json as _json
        return drafts_from_payload(_json.loads(raw.decode("utf-8"))), ""
    except Exception as exc:  # noqa: BLE001
        return {}, f"Saved drafts for this survey could not be read: {exc}"


def delete_drafts(vault: Any, slug: str) -> Tuple[bool, str]:
    try:
        vault.delete(drafts_key(slug))
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def fingerprint(drafts: Dict[str, Draft]) -> str:
    """Cheap change detector, so an idle rerun doesn't re-upload the batch.

    Streamlit reruns the script on every widget interaction. Without this, every
    keystroke in a draft's text box would push the whole set to the vault.
    """
    import hashlib
    parts = []
    for key in sorted((drafts or {})):
        d = drafts[key]
        parts.append(f"{key}|{d.approved}|{d.error}|{len(d.edited)}|"
                     f"{hash(d.edited)}|{len(d.text())}|{d.score}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


class BatchAborted(RuntimeError):
    """A batch stopped on a failure that would repeat for every remaining student.

    Carries ``drafts``: whatever completed before the stop, so the review panel
    can keep that work instead of discarding it alongside the error.
    """

    def __init__(self, message: str, drafts: Optional[Dict[str, Draft]] = None):
        super().__init__(message)
        self.drafts: Dict[str, Draft] = drafts or {}


def calls_per_student(settings: AISettings) -> int:
    """One request, or two when the grounding audit runs."""
    return 2 if settings.verify else 1


def make_client(settings: AISettings,
                on_usage: Optional[Callable] = None) -> LLMClient:
    spec = get_provider(settings.provider)
    rpm = max(0, int(settings.requests_per_minute or 0))
    return LLMClient(
        min_interval=(60.0 / rpm) if rpm else 0.0,
        provider=settings.provider,
        model=settings.model,
        api_key=settings.resolved_api_key(),
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        on_usage=on_usage,
        base_url_override=settings.local_base_url if spec.is_local else "",
        app_name="PeerParley",
        app_url="https://github.com/",
    )


def test_credentials(settings: AISettings) -> Tuple[bool, str]:
    """One tiny request, to find out whether the key works.

    Worth its own function because the alternative is discovering a bad
    credential forty students into a batch. The prompt is deliberately trivial —
    a few tokens, costing effectively nothing — and the reply's content is
    irrelevant: what is being tested is whether the provider accepts the key at
    all.
    """
    ready, why = settings.ready()
    if not ready:
        return False, why
    try:
        client = make_client(settings)
    except LLMError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not build the client: {exc}"

    try:
        client.complete("Reply with the single word OK.", "Ready?", max_tokens=16)
    except AuthLLMError as exc:
        return False, str(exc)
    except TruncatedResponseError:
        # It answered, then ran out of room. The credential is fine, which is
        # the only thing being asked.
        return True, ""
    except LLMError as exc:
        # Reached the provider and got a non-auth failure — a bad model name, a
        # rate limit, a local server that is not running. Not a key problem, and
        # saying so saves the instructor from rotating a working key.
        return False, (
            f"The key was accepted, but the request failed: {exc} "
            "Check the model name (and, for a local server, that it is running)."
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error: {exc}"
    return True, ""


def generate_for_teams(
    teams: Sequence[TeamResult],
    settings: AISettings,
    client: Optional[LLMClient] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    only_keys: Optional[Iterable[str]] = None,
    on_usage: Optional[Callable] = None,
) -> Dict[str, Draft]:
    """Generate a draft per student. Returns {student key: Draft}.

    One student's failure does not abort the batch — a rate limit on the
    fourteenth student should not discard thirteen finished drafts. Failures
    come back as Drafts carrying an error, so the review table can show what
    happened and offer a retry for those alone.
    """
    if client is None:
        client = make_client(settings, on_usage=on_usage)

    wanted = set(only_keys) if only_keys is not None else None
    members = [
        m for t in teams for m in t.members
        if wanted is None or m.key in wanted
    ]
    total = len(members)
    drafts: Dict[str, Draft] = {}

    for i, m in enumerate(members, start=1):
        if progress:
            progress(i, total, m.name)
        src = build_source(m, include_ratings=settings.include_ratings)
        try:
            drafts[m.key] = generate_draft(client, src, settings)
        except AuthLLMError as exc:
            # Stop here. Every remaining student would fail the same way, and
            # forty copies of one credential error is worse than one — it hides
            # the answer instead of delivering it. Whatever finished is kept.
            raise BatchAborted(str(exc), drafts) from exc

    return drafts


def approved_narratives(drafts: Dict[str, Draft]) -> Dict[str, str]:
    """{student key: narrative text} for approved, non-empty drafts only.

    This is the single gate between generated text and anything a student sees.
    The PDF builder and the email path both read from here, so there is exactly
    one definition of "approved" and no way to ship an unapproved draft by
    taking a different route to the PDF.
    """
    return {
        key: d.text() for key, d in (drafts or {}).items()
        if d.approved and d.ok and d.text().strip()
    }


def error_groups(drafts: Dict[str, Draft]) -> List[Tuple[str, List[str]]]:
    """Failures grouped by message, worst-shared-first.

    Forty students failing for one reason is one problem, and listing it forty
    times hides that. Auth failures now abort the batch, but a wrong model name
    fails per-request and would still fill the panel with identical rows, so the
    panel groups whatever repeats.
    """
    buckets: Dict[str, List[str]] = {}
    for d in (drafts or {}).values():
        if not d.ok:
            buckets.setdefault(d.error.strip(), []).append(d.name)
    return sorted(buckets.items(), key=lambda kv: -len(kv[1]))


def batch_stats(drafts: Dict[str, Draft]) -> Dict[str, Any]:
    """Counts for the review header."""
    values = list((drafts or {}).values())
    usage = Usage()
    for d in values:
        usage.add(d.usage)
    return {
        "total": len(values),
        # Drafts that actually produced usable text. "total" counts Draft
        # objects, and a failure is still a Draft — so reporting "total" as the
        # number drafted tells an instructor 40 narratives were written when
        # none were. This is the number that belongs on screen.
        "written": sum(1 for d in values if d.ok and d.text().strip()),
        "approved": sum(1 for d in values if d.approved),
        "clean": sum(1 for d in values if d.clean),
        "flagged": sum(1 for d in values if d.ok and d.flags and not d.empty),
        # Empty drafts carry a high-severity flag of their own, but they are not
        # ungrounded claims — counting them under "unsupported claims" would
        # describe the wrong problem. They get their own number.
        "high": sum(1 for d in values if d.high_severity and not d.empty),
        "empty": sum(1 for d in values if d.empty),
        "errors": sum(1 for d in values if not d.ok),
        "thin": sum(1 for d in values if d.insufficient_evidence),
        "truncated": sum(1 for d in values if d.ok and d.truncated),
        "usage": usage,
    }

"""Screening what reaches a student for language that should not.

Peer evaluation invites anonymous criticism of a classmate, and anonymity
occasionally produces something that is not criticism at all. PeerParley has
always forwarded teammates' written comments to the student more or less
verbatim, which means an abusive remark had a clear path from one student to
another with nobody in between. That is the harm this module exists to catch.

**Two surfaces, not one.** The AI narrative is screened, but the *raw comments*
matter more: they are a teammate's unedited words, and in the "comments only"
delivery mode they are the entire report. A screen that only looked at
generated text would miss the likelier problem.

**Two screens, in order of reliability.**

1. This module: a local, deterministic pass over the text. No API call, always
   runs, catches unambiguous profanity, slurs, threats and direct personal
   attacks. It is cheap and it is dumb.
2. The grounding audit, when it is on, is also asked to flag abusive language.
   It reads context, so it catches what a word list cannot: contempt expressed
   in clean language, "she should not be in this major", a sneer with no
   profanity in it. This costs nothing extra — the audit call already happens.

**Neither is a guarantee, and the module does not pretend otherwise.** A word
list is trivially evaded and blind to tone; a model is inconsistent. Both exist
to direct the instructor's attention, which is the only real control. So
nothing here censors anything: a flag is raised, bulk approval is blocked, and
the instructor decides. Silently deleting a teammate's genuine — if blunt —
criticism would be its own failure, and a system that quietly edited peer
feedback would be worse than one that asks.

**Severity drives what the UI suggests, not what it does.**

* ``severe`` — slurs, threats, sexual harassment. Should not reach a student.
  The UI proposes switching that student to summary-only delivery, which is
  what keeps the remark out while still returning the substance.
* ``moderate`` — profanity, direct insults ("useless", "an idiot"). The
  instructor's call; often worth rewording rather than removing.
* ``mild`` — harsh but arguably legitimate ("lazy", "did nothing"). Noted only,
  because "he did nothing" may be the honest and useful truth about a project.

**The lists are deliberately short and extensible.** They cover the
unambiguous and leave nuance to the model and the human. An institution with
its own conduct vocabulary can extend ``EXTRA_PATTERNS`` without touching the
logic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #
#
# Word-boundary anchored so "class" never matches a slur fragment and
# "assignment" never matches profanity — false positives train people to ignore
# warnings, which costs more than the occasional miss.

_SEVERE: Tuple[Tuple[str, str], ...] = (
    # Threats and incitement.
    (r"\b(?:kill|murder|stab|shoot)\s+(?:him|her|them|you|himself|herself|themselves)\b",
     "appears to threaten violence"),
    (r"\b(?:should|ought to)\s+(?:die|kill\s+(?:him|her|them)self)\b",
     "appears to wish harm"),
    (r"\bkys\b", "appears to tell someone to kill themselves"),
    # Sexual harassment.
    (r"\b(?:slut|whore|rapist)\b", "sexual slur or accusation"),
    (r"\b(?:sexy|hot)\b[^.!?]{0,20}\b(?:body|ass|tits)\b",
     "sexualised comment about a classmate"),
    # Identity-based slurs. Deliberately a short list of unambiguous terms;
    # the model screen is what catches the rest and the coded ones.
    (r"\b(?:f[a4]gg?(?:ot)?s?|tr[a4]nn(?:y|ies)|n[i1]gg(?:er|a)s?|k[i1]ke|"
     r"sp[i1]cs?|ch[i1]nks?|retard(?:ed|s)?)\b",
     "slur targeting a protected characteristic"),
)

_MODERATE: Tuple[Tuple[str, str], ...] = (
    (r"\b(?:fuck(?:ing|ed|er)?|shit(?:ty)?|bitch|bastard|asshole|dick(?:head)?|"
     r"douche(?:bag)?|prick|cunt)\b", "profanity"),
    (r"\b(?:idiot|moron|imbecile|dumbass|stupid|dumb|brain-?dead)\b",
     "direct personal insult"),
    (r"\b(?:useless|worthless|pathetic|hopeless|incompetent)\b",
     "attacks the person rather than the work"),
    (r"\b(?:hate|despise|loathe)\s+(?:him|her|them|this\s+person|working\s+with)\b",
     "expresses personal hostility"),
    (r"\bshould(?:n't| not)\s+(?:be\s+)?(?:in|allowed)\b[^.!?]{0,30}"
     r"\b(?:major|program|class|school|university|business)\b",
     "says the student does not belong in the course or programme"),
)

_MILD: Tuple[Tuple[str, str], ...] = (
    (r"\b(?:lazy|slacker|freeloader|deadweight|dead\s+weight)\b",
     "harsh characterisation of the person"),
    (r"\b(?:did|contributed)\s+(?:absolutely\s+)?nothing\b",
     "blunt, though possibly accurate"),
    (r"\bnever\s+(?:showed\s+up|did\s+anything|helped)\b",
     "blunt, though possibly accurate"),
)

# Institutions with their own conduct vocabulary extend this rather than
# editing the tables above: (pattern, reason, severity).
EXTRA_PATTERNS: List[Tuple[str, str, str]] = []

_SEVERITY_ORDER = {"severe": 3, "moderate": 2, "mild": 1}


def _compiled() -> List[Tuple[re.Pattern, str, str]]:
    rules: List[Tuple[re.Pattern, str, str]] = []
    for table, severity in ((_SEVERE, "severe"), (_MODERATE, "moderate"),
                            (_MILD, "mild")):
        for pattern, reason in table:
            rules.append((re.compile(pattern, re.IGNORECASE), reason, severity))
    for pattern, reason, severity in EXTRA_PATTERNS:
        try:
            rules.append((re.compile(pattern, re.IGNORECASE), reason,
                          severity if severity in _SEVERITY_ORDER else "moderate"))
        except re.error:
            continue        # a bad custom pattern must not break the screen
    return rules


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #

@dataclass
class Concern:
    """One passage worth an instructor's eyes, and why."""

    text: str               # the comment or sentence it was found in
    reason: str
    severity: str           # severe | moderate | mild
    matched: str = ""       # the specific phrase that triggered it
    where: str = "comment"  # "comment" (a teammate wrote it) | "narrative"

    @property
    def icon(self) -> str:
        return {"severe": "🛑", "moderate": "⚠️", "mild": "•"}.get(
            self.severity, "•")

    def to_dict(self) -> Dict[str, str]:
        return {"text": self.text, "reason": self.reason,
                "severity": self.severity, "matched": self.matched,
                "where": self.where}

    @classmethod
    def from_dict(cls, d: Dict[str, str]) -> "Concern":
        return cls(text=str(d.get("text", "")), reason=str(d.get("reason", "")),
                   severity=str(d.get("severity", "moderate")),
                   matched=str(d.get("matched", "")),
                   where=str(d.get("where", "comment")))


def screen(text: str, where: str = "comment") -> List[Concern]:
    """Findings for one piece of text, worst first. Never raises."""
    body = (text or "").strip()
    if not body:
        return []
    found: List[Concern] = []
    seen: set = set()
    for rule, reason, severity in _compiled():
        match = rule.search(body)
        if not match:
            continue
        key = (reason, severity)
        if key in seen:
            continue
        seen.add(key)
        found.append(Concern(text=body, reason=reason, severity=severity,
                             matched=match.group(0), where=where))
    found.sort(key=lambda c: -_SEVERITY_ORDER.get(c.severity, 0))
    return found


def screen_many(comments: Iterable[str], where: str = "comment") -> List[Concern]:
    out: List[Concern] = []
    for comment in comments or []:
        out.extend(screen(comment, where=where))
    out.sort(key=lambda c: -_SEVERITY_ORDER.get(c.severity, 0))
    return out


def worst(concerns: Sequence[Concern]) -> str:
    """The highest severity present, or "" for none."""
    if not concerns:
        return ""
    return max(concerns, key=lambda c: _SEVERITY_ORDER.get(c.severity, 0)).severity


def blocks_release(concerns: Sequence[Concern]) -> bool:
    """Is anything here serious enough that it should not reach a student?

    True only for ``severe``. Moderate and mild are judgement calls, and
    treating them as blocking would make the block meaningless — which is how
    warnings come to be clicked through without reading.
    """
    return worst(concerns) == "severe"


def advice(concerns: Sequence[Concern]) -> str:
    """What the instructor can actually do about it, in one line."""
    level = worst(concerns)
    from_comments = any(c.where == "comment" for c in concerns)
    if level == "severe" and from_comments:
        return ("A teammate's own words are the problem here. Setting this "
                "student to **Summary only** keeps the remark out of their "
                "report while still returning the substance — and this is "
                "worth handling as a conduct matter separately.")
    if level == "severe":
        return ("The generated summary contains it, so regenerate or edit it "
                "before approving.")
    if level == "moderate" and from_comments:
        return ("Your call. Summary-only delivery would reword it; sending the "
                "comments as written passes the phrasing along unchanged.")
    if level == "moderate":
        return "Worth editing the wording before approving."
    return "Noted only — blunt criticism can still be the useful truth."

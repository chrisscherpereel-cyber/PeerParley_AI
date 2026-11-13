"""Grading engine.

Peer-allocation method: each student distributes 100 points across teammates.
Individual score = Team Score x [1 + B * A * Q * (pay_grade - 1)], capped to
configurable min/max multipliers.

  pay_grade   : a student's average received allocation / the TEAM average
                (100% = exactly average). Above average → bonus; below → deduction;
                exactly average → no change. Being relative to the team average, the
                whole team can never be pushed into a deduction by self-allocations.
  B           : global sensitivity (how much peer input moves the grade)
  A           : agreement weight from SD of received points (evaluator consensus, ≤1)
  Q           : comment support score for that student (0..1), from comments.py
                (A and Q only shrink the adjustment; they never flip its sign)

In addition — reproducing the original PeerParley workbook — this engine also
computes, when the survey collected them:

  * Per-dimension letter grades from the 4-statement rating matrix
    (Team Player / Quantity / Quality / Effect), each = mean(rating)/7 → letter.
  * Performance from the FORCED RANKING (High / Adequate / Low) when present,
    otherwise from the allocation ratio (banded ±8%).
  * Pay grade = a student's average received allocation ÷ the team average.
  * Self-evaluation grades from the student's ratings of themselves.

All of the extra inputs are optional: with only allocation + comments (e.g. a
plain Qualtrics export), the engine behaves exactly as before and the extra
fields are simply blank.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .comments import score_comment


# --------------------------------------------------------------------------- #
# Scales
# --------------------------------------------------------------------------- #
GRADE_SCALE: List[Tuple[float, str]] = [
    (0.00, "F"), (0.36, "D"), (0.43, "C-"), (0.50, "C"), (0.57, "C+"),
    (0.64, "B-"), (0.71, "B"), (0.78, "B+"), (0.86, "A-"), (0.93, "A"), (1.00, "A"),
]
DIMENSIONS = ["Team Player", "Quantity", "Quality", "Effect"]

# Shown instead of a performance band when nobody evaluated a student. Kept
# short because the summary table truncates this column to nine characters.
NO_DATA = "No data"


def letter_grade(value) -> str:
    """Map a 0..1 score to a letter grade (blank for missing)."""
    try:
        if value is None or math.isnan(float(value)):
            return ""
    except (TypeError, ValueError):
        return ""
    out = GRADE_SCALE[0][1]
    for thr, lbl in GRADE_SCALE:
        if value >= thr:
            out = lbl
    return out


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass
class GradeSettings:
    team_score_default: float = 100.0
    sensitivity_B: float = 0.5
    min_multiplier: float = 0.50    # max 50% DECREASE (deduction cap)
    max_multiplier: float = 1.05    # max  5% INCREASE (bonus cap)
    max_comment_points: int = 10
    # 1 or 5 (percent). The default used to be 5, and that single setting was
    # what made the results tab look broken: a normal section produces grade
    # adjustments in the ±2% to ±8% range, and a 5-point grid flattens all of
    # them onto {-5, 0, +5}. Ten students came out with three distinct grades
    # and half of them at exactly the team score — "the same for everyone",
    # with nothing actually wrong with the data. 1% steps report what was
    # computed; 5% is still available for instructors who want round numbers.
    rounding_step: int = 1          # 1 or 5 (percent)
    rounding_mode: str = "nearest"  # nearest | up | down
    performance_method: str = "allocation_ratio"  # allocation_ratio|rank_linear|rank_one_mean
    performance_band: float = 0.08  # +-8%
    agreement_guard: bool = True
    # Which peer measure drives the grade adjustment (the normalized peer factor):
    #   allocation | rating | ranking | combined
    adjustment_source: str = "allocation"
    normalize_raters: bool = False   # z-score each evaluator (corrects for leniency)


# --------------------------------------------------------------------------- #
# A — how much did this student's raters agree with each other?
# --------------------------------------------------------------------------- #
# A dampens the grade adjustment when the peers who evaluated a student did not
# agree about them: weak evidence should move a grade less than strong evidence.
#
# It used to be four fixed bands — 1.00 / 0.75 / 0.50 / 0.25 — cut at 10%, 20%
# and 30% of an even share. Two things were wrong with that.
#
# First the resolution. Forty students could only ever land on four values, and
# because A multiplies the adjustment, the three numbers downstream of it
# (multiplier, individual score, grade change) inherited the same coarseness.
# Measured on a section where raters disagreed a normal amount, A came out 0.25
# for almost everybody and every grade change rounded to zero.
#
# Second, and worse, the bands measured spread against a *scale* rather than
# against normal. Four teammates splitting $100 four ways and differing by five
# dollars is ordinary disagreement, not a reason to distrust them — but it put
# the student past the 20% cut. The threshold for "my raters disagree" has to
# come from how much raters in this section actually disagree.
#
# So A is now continuous and measured against the median spread of the section
# it is in: 1.00 when a student's raters are no more split than is typical here,
# sliding down to AGREEMENT_FLOOR when they are AGREEMENT_SPAN times more split
# than typical. The floor is 0.50 rather than 0.25 because A is one of three
# factors already shrinking the adjustment — stacking three halvings and then
# rounding is how a whole section ends up at no change at all.
AGREEMENT_FLOOR = 0.50
AGREEMENT_SPAN = 1.50
# Used when the section is too small to have a median of its own (fewer than
# four students with more than one rating). 0.15 of an even share is the median
# measured on simulated 8-team sections with realistic rater noise.
AGREEMENT_TYPICAL = 0.15
# A section where every rater typed exactly the same numbers would have a
# median spread near zero, and dividing by it would turn a one-dollar
# difference into a maximum penalty. Below this, use the typical value.
AGREEMENT_MIN_REFERENCE = 0.05


def agreement_weight(received: List[float], expected_share: float,
                     reference: float = 0.0) -> float:
    """How much weight do this student's peer ratings deserve? 0.5 … 1.0.

    `reference` is the section's typical spread, as a fraction of an even
    share, from `dispersion_reference()`. Left at 0 it falls back to
    AGREEMENT_TYPICAL, so calling this with two arguments still works.
    """
    vals = [v for v in received if v == v]  # drop NaN
    if len(vals) < 2 or expected_share <= 0:
        return 1.0
    frac = float(np.std(vals, ddof=0)) / expected_share
    ref = reference if reference > 0 else AGREEMENT_TYPICAL
    ref = max(ref, AGREEMENT_MIN_REFERENCE)
    excess = frac / ref - 1.0
    if excess <= 0:
        return 1.0
    shrink = min(excess / AGREEMENT_SPAN, 1.0)
    return round(AGREEMENT_FLOOR + (1.0 - AGREEMENT_FLOOR) * (1.0 - shrink), 3)


def dispersion_reference(long_df: pd.DataFrame) -> float:
    """The median rater spread in this section, as a fraction of an even share.

    This is what makes A mean "your raters agreed less than is usual *in this
    class*" rather than "your raters differed by more than ten dollars". It is
    computed once per section, before any team is graded, so every student is
    measured against the same yardstick.

    Returns 0.0 when there is not enough data to form a median, and the caller
    then falls back to AGREEMENT_TYPICAL.
    """
    if long_df is None or long_df.empty or "points" not in long_df.columns:
        return 0.0
    if not {"evaluatee_key", "evaluator_key"} <= set(long_df.columns):
        return 0.0
    # `compute` adds a "team" column (the evaluator's team, so a student on two
    # teams is counted once). Called on raw long-format data — from a test, or
    # from a caller wanting the number on its own — fall back to the column the
    # intake paths write.
    team_col = ("team" if "team" in long_df.columns
                else "evaluator_team" if "evaluator_team" in long_df.columns
                else "")
    if not team_col:
        return 0.0
    fracs: List[float] = []
    for team, tdf in long_df.groupby(team_col):
        if not str(team).strip():
            continue
        keys = {k for k in set(tdf["evaluatee_key"]) | set(tdf["evaluator_key"])
                if str(k).strip()}
        share = 100.0 / (len(keys) - 1) if len(keys) > 1 else 100.0
        if share <= 0:
            continue
        for _, grp in tdf.groupby("evaluatee_key"):
            vals = [float(v) for v in grp["points"] if v == v]
            if len(vals) > 1:
                fracs.append(float(np.std(vals, ddof=0)) / share)
    if len(fracs) < 4:
        return 0.0
    return float(np.median(fracs))


def _round_pct(x: float, step: int, mode: str) -> float:
    if step <= 0:
        return round(x, 2)
    q = x / step
    if mode == "up":
        q = np.ceil(q)
    elif mode == "down":
        q = np.floor(q)
    else:
        q = np.round(q)
    return float(q * step)


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #
@dataclass
class StudentResult:
    name: str
    key: str
    team: str
    team_score: float
    received_total: float
    expected_share: float
    peer_ratio: float
    A: float
    Q: float
    multiplier: float
    individual_score: float
    signed_pct: float           # deviation from team score, signed
    performance: str            # High / Adequate / Expected / Low
    comment_points: int
    submitted_self_eval: bool
    # True when no teammate evaluated this student at all. Their grade is left
    # at the team score; the instructor is told rather than the student being
    # silently handed the largest deduction the settings allow.
    no_peer_input: bool = False
    # ---- rating-matrix dimension grades + pay grade (workbook parity) ----
    pay_grade: float = float("nan")
    peer_vals: List[float] = field(default_factory=lambda: [float("nan")] * 4)
    team_player: str = ""
    quantity: str = ""
    quality: str = ""
    effect: str = ""
    forced_rank_mean: float = float("nan")
    # ---- self-evaluation ----
    self_responded: bool = False
    self_vals: List[float] = field(default_factory=lambda: [float("nan")] * 4)
    self_team_player: str = ""
    self_quantity: str = ""
    self_quality: str = ""
    self_effect: str = ""
    # Mean (self − peers) across the rating dimensions, in points of the
    # 7-point scale. NaN when the student did not rate themselves, or when no
    # peer ratings exist to compare with. See `self_peer_gap`.
    self_gap: float = float("nan")
    # ---- comments received (split for the feedback report) ----
    public_comments: List[str] = field(default_factory=list)
    contributions: List[str] = field(default_factory=list)   # "what teammates valued"
    improvements: List[str] = field(default_factory=list)     # "where to focus next"
    confidential_comments: List[str] = field(default_factory=list)
    received_breakdown: List[float] = field(default_factory=list)
    # ---- feedback the student WROTE (response quality) ----
    response_Q: float = float("nan")        # comment-support of the feedback they gave
    response_points: int = 0                # points earned for their response quality


@dataclass
class TeamResult:
    team: str
    members: List[StudentResult]
    team_score: float


def _performance_label(method, received_avg, team_avg, band, rank, n):
    if method == "rank_linear":
        if n <= 1:
            return "Expected"
        top = rank <= max(1, n // 3)
        bottom = rank > n - max(1, n // 3)
        return "High" if top else ("Low" if bottom else "Expected")
    if method == "rank_one_mean":
        return "High" if rank == 1 else "Expected"
    if team_avg <= 0:
        return "Expected"
    ratio = received_avg / team_avg
    if ratio >= 1 + band:
        return "High"
    if ratio <= 1 - band:
        return "Low"
    return "Expected"


def _forced_performance(mean_rank: float) -> Optional[str]:
    """The label this student's teammates settled on. 1=High, 2=Adequate, 3=Low.

    Their average rank, rounded to whichever of the three labels is nearest:

        mean < 1.5          High
        1.5 ≤ mean ≤ 2.5    Adequate
        mean > 2.5          Low

    So two Adequates and one Low (mean 2.33) is **Adequate** — the label most
    of the team chose — and a dead tie between two labels resolves to Adequate,
    because a split verdict is not a clear High and not a clear Low either.

    It used to map the mean onto 0–1 as (3 − mean)/2 and cut at 0.8 and 0.4,
    which needed a mean of 1.2 or better for High and 2.2 or better for
    Adequate. Those cuts sit nowhere near the midpoints between the labels, so
    the commonest split verdicts came out a tier low: two Adequates and one Low
    read "Low", and two Highs and one Adequate (mean 1.33) read "Adequate".
    """
    if mean_rank is None or (isinstance(mean_rank, float) and math.isnan(mean_rank)):
        return None
    if mean_rank < 1.5:
        return "High"
    if mean_rank <= 2.5:
        return "Adequate"
    return "Low"


def _num(v):
    try:
        f = float(v)
        return f if not math.isnan(f) else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# How a student's view of themselves compares with their team's
# --------------------------------------------------------------------------- #
# "Did they self-evaluate?" was a yes/no that said nothing — every student in
# the table submitted, so it read Yes for all of them. The useful question is
# the *gap*: a student who rates themselves a point and a half above everyone
# else is the conversation an instructor wants to have, and so is one who rates
# themselves well below.
#
# The measure: for each of the four rating statements where both a self-rating
# and at least one peer rating exist, take self minus the peers' mean; average
# those differences; report it in points of the original 7-point scale. One
# number, in the units the students actually answered in, so "+1.2" reads as
# "rated themselves 1.2 points higher than their team did, on average across
# the four statements".
#
# Dimensions the student skipped, and dimensions no peer rated, are left out
# rather than counted as zero — a blank is not agreement.
SELF_GAP_ALIGNED = 0.5      # within half a scale point either way: in line


def self_peer_gap(self_vals: List[float], peer_vals: List[float]) -> float:
    """Mean (self − peers) across the rating dimensions, in 7-point units.

    `self_vals` and `peer_vals` are both already divided by 7 (the 0–1 form the
    dimension letter grades are cut from), so the difference is multiplied back
    up to the scale the question was asked on. NaN when there is nothing to
    compare.
    """
    diffs = [(s - p) * 7.0 for s, p in zip(self_vals, peer_vals)
             if s == s and p == p]
    return float(np.mean(diffs)) if diffs else float("nan")


def describe_self_gap(gap: float) -> str:
    """The gap as the results table shows it: a signed number and a word."""
    if gap is None or (isinstance(gap, float) and math.isnan(gap)):
        return "—"
    if abs(gap) <= SELF_GAP_ALIGNED:
        word = "in line"
    elif gap > 0:
        word = "higher"
    else:
        word = "lower"
    return f"{gap:+.1f} ({word})"


# --------------------------------------------------------------------------- #
# Core computation
# --------------------------------------------------------------------------- #
def compute(
    long_df: pd.DataFrame,
    settings: GradeSettings,
    team_scores: Optional[Dict[str, float]] = None,
    self_evals: Optional[Dict] = None,
) -> List[TeamResult]:
    """Per-team, per-student results from tidy long-format data.

    Optional long_df columns `r0..r3` (1–7 ratings for the four statements) and
    `rank` (1/2/3 forced ranking) drive the dimension grades and performance when
    present. `self_evals` maps (team, name_key) -> {ratings, rank} for self grades.
    """
    team_scores = team_scores or {}
    self_evals = self_evals or {}
    if long_df.empty:
        return []

    results: List[TeamResult] = []
    ev_team = (
        long_df.dropna(subset=["evaluator_team"])
        .groupby("evaluator_key")["evaluator_team"].first().to_dict()
    )

    def team_of(key: str) -> str:
        return ev_team.get(key, "")

    long_df = long_df.copy()
    long_df["team"] = long_df["evaluator_key"].map(team_of)
    if "points" in long_df.columns:
        # A column where *every* cell failed to parse stays object dtype, and
        # `None == None` is True, so the "is this a number" guard below would let
        # a None through to float(). Coercing once here turns the whole class of
        # bad allocation column (renamed question, text in every cell) into
        # missing data, which the rest of this function already handles, instead
        # of a TypeError that takes the results tab down.
        long_df["points"] = pd.to_numeric(long_df["points"], errors="coerce")
    has_ratings = all(f"r{d}" in long_df.columns for d in range(4))
    has_rank = "rank" in long_df.columns
    # One yardstick for the whole section, so A compares students with each
    # other rather than with a fixed number of dollars. See agreement_weight.
    agreement_ref = dispersion_reference(long_df)

    for team, tdf in long_df.groupby("team"):
        if team == "":
            continue
        member_keys = sorted(set(tdf["evaluatee_key"]) | set(tdf["evaluator_key"]))
        member_keys = [k for k in member_keys if k]
        n = len(member_keys)
        expected_share = 100.0 / (n - 1) if n > 1 else 100.0
        ts = float(team_scores.get(team, settings.team_score_default))
        submitters = set(tdf["evaluator_key"])

        received: Dict[str, List[float]] = {k: [] for k in member_keys}
        pub: Dict[str, List[str]] = {k: [] for k in member_keys}
        contrib: Dict[str, List[str]] = {k: [] for k in member_keys}
        improve: Dict[str, List[str]] = {k: [] for k in member_keys}
        conf: Dict[str, List[str]] = {k: [] for k in member_keys}
        dims: Dict[str, List[List[float]]] = {k: [[], [], [], []] for k in member_keys}
        ranks_recv: Dict[str, List[float]] = {k: [] for k in member_keys}
        authored: Dict[str, List[str]] = {k: [] for k in member_keys}  # comments WRITTEN by k
        authored_slots: Dict[str, int] = {}   # teammates each student was asked about
        display_name: Dict[str, str] = {}
        has_texts = ("contribution_text" in long_df.columns
                     and "improve_text" in long_df.columns)

        for _, r in tdf.iterrows():
            k = r["evaluatee_key"]
            e = r["evaluator_key"]
            if k not in received:
                received[k] = []; pub[k] = []; conf[k] = []
                contrib[k] = []; improve[k] = []
                dims[k] = [[], [], [], []]; ranks_recv[k] = []
            authored.setdefault(e, [])
            display_name.setdefault(k, r["evaluatee"])
            if e == k:
                # A presence row: the intake adds one for a student on a team
                # that nobody has evaluated yet, so they are reported as "No
                # data" rather than vanishing from the results. It establishes
                # that they are on this team and must contribute nothing else —
                # above all not a phantom teammate to the denominator below.
                continue
            # Every teammate this student was asked about, whether or not they
            # wrote anything: the denominator for "the quality of the feedback
            # you wrote". Without it, skipping three teammates and writing one
            # good comment would score the same as writing four.
            authored_slots[e] = authored_slots.get(e, 0) + 1
            if r["points"] == r["points"]:
                received[k].append(float(r["points"]))
            if r.get("public_comment"):
                pub[k].append(str(r["public_comment"]).strip())
                authored[e].append(str(r["public_comment"]).strip())
            if has_texts:
                if r.get("contribution_text"):
                    contrib[k].append(str(r["contribution_text"]).strip())
                if r.get("improve_text"):
                    improve[k].append(str(r["improve_text"]).strip())
            if r.get("confidential_comment"):
                # The confidential note is one free-text box per submission — a
                # message from the student to the instructor, not feedback about
                # a particular teammate. Both intake paths park it on the first
                # teammate row of the submission, so filing it by evaluatee made
                # it read as evidence *about* whoever happened to be listed
                # first. It belongs to its author.
                conf.setdefault(e, []).append(str(r["confidential_comment"]).strip())
            if has_ratings:
                for d in range(4):
                    v = _num(r.get(f"r{d}"))
                    if v is not None:
                        dims[k][d].append(v)
            if has_rank:
                rk = _num(r.get("rank"))
                if rk is not None:
                    ranks_recv[k].append(rk)
            display_name.setdefault(k, r["evaluatee"])
        for _, r in tdf.iterrows():
            display_name.setdefault(r["evaluator_key"], r["evaluator"])

        # Only students somebody actually rated have a received average. Treating
        # "nobody submitted an evaluation of this student" as "every teammate gave
        # them $0" is the difference between no adjustment and the maximum
        # deduction, and it also used to drag the team average down for everyone
        # else. A missing average means missing data, so it is left out of both.
        received_avgs = {k: float(np.mean(v)) for k, v in received.items() if v}
        unrated = [k for k in member_keys if k not in received_avgs]
        team_avg = float(np.mean(list(received_avgs.values()))) if received_avgs else 0.0
        ranking = sorted(received_avgs, key=lambda k: received_avgs[k], reverse=True)
        rank_of = {k: i + 1 for i, k in enumerate(ranking)}

        # ---- alternative peer-adjustment measures (each a normalized peer factor:
        #      a student's peer score ÷ the team average, so 1.0 = average) --------
        member_rating = {}   # mean of the four 0-1 dimension scores
        for k in member_keys:
            _rv = [float(np.mean(dims[k][d])) / 7.0 for d in range(4) if dims[k][d]]
            member_rating[k] = float(np.mean(_rv)) if _rv else float("nan")
        member_rankscore = {}  # (3 - mean forced rank)/2  → High 1.0 · Adequate 0.5 · Low 0.0
        for k in member_keys:
            member_rankscore[k] = ((3.0 - float(np.mean(ranks_recv[k]))) / 2.0) \
                if ranks_recv[k] else float("nan")

        alloc_source, alloc_team = dict(received_avgs), team_avg
        if settings.normalize_raters:
            # Correct for easy and harsh graders, in dollars.
            #
            # Each rater's allocations are standardised and then mapped back onto
            # the team's own pooled mean and spread, so a rater who splits
            # $40/$35/$25 and one who splits $34/$33/$33 carry the same weight
            # while the numbers stay dollars. The earlier version stopped at the
            # standard score and used it where a ratio was expected, which put
            # the adjustment on a different scale entirely: a single lenient
            # rater could swing a teammate from +0% to -35% and reorder the team.
            #
            # Two properties worth keeping in mind, both covered by tests: when
            # every rater spreads their dollars the same way this is a no-op, and
            # a rater with one rating (or who gave everyone the same amount) has
            # no spread to correct, so their ratings land on the team mean rather
            # than counting as a verdict.
            given: Dict[str, List[float]] = {}
            pooled: List[float] = []
            for _, r in tdf.iterrows():
                v = _num(r.get("points"))
                if v is not None:
                    given.setdefault(r["evaluator_key"], []).append(v)
                    pooled.append(v)
            if pooled:
                pooled_mean = float(np.mean(pooled))
                pooled_sd = float(np.std(pooled, ddof=0))
                zstat = {e: (float(np.mean(g)),
                             float(np.std(g, ddof=0)) if len(g) > 1 else 0.0)
                         for e, g in given.items()}
                adj: Dict[str, List[float]] = {}
                for _, r in tdf.iterrows():
                    v = _num(r.get("points"))
                    if v is None:
                        continue
                    mu, sd = zstat.get(r["evaluator_key"], (pooled_mean, 0.0))
                    z = ((v - mu) / sd) if sd > 0 else 0.0
                    adj.setdefault(r["evaluatee_key"], []).append(
                        pooled_mean + z * pooled_sd)
                alloc_source = {k: float(np.mean(v)) for k, v in adj.items() if v}
                alloc_team = (float(np.mean(list(alloc_source.values())))
                              if alloc_source else team_avg)

        _rt = [v for v in member_rating.values() if v == v]
        rating_team = float(np.mean(_rt)) if _rt else float("nan")
        _kt = [v for v in member_rankscore.values() if v == v]
        rank_team = float(np.mean(_kt)) if _kt else float("nan")

        def _factor(x, avg):
            try:
                if x is None or math.isnan(float(x)) or avg is None or math.isnan(float(avg)) \
                        or avg == 0:
                    return None
            except (TypeError, ValueError):
                return None
            return float(x) / float(avg)

        # ---- written support for each student's ratings (Q) ------------------
        # The AVERAGE of the comments written about a student, not the best one.
        # Taking the best made Q a property of the team rather than the student:
        # one articulate teammate writes well about everyone, so everybody's Q
        # came out the same, and in the instructor summary the column looked
        # broken because it was — within a team it could not vary.
        #
        # Two cases that are not the student's doing are handled deliberately:
        # a survey that collected no written feedback at all leaves the
        # dampener inactive (Q = 1.0 for everyone, so numbers-only sections
        # still grade), and a student whose teammates all left their boxes
        # blank, in a team where others did write, takes the team's average
        # rather than a zero they did not earn or a free pass they did not
        # either.
        q_scores: Dict[str, float] = {}
        q_points: Dict[str, int] = {}
        for _k in member_keys:
            _mine = [c for c in pub.get(_k, []) if c and c.strip()]
            if not _mine:
                continue
            _others = [c for kk, cl in pub.items() if kk != _k for c in cl]
            _scored = [score_comment(c, _others, settings.max_comment_points)
                       for c in _mine]
            q_scores[_k] = float(np.mean([s.q for s in _scored]))
            q_points[_k] = int(round(q_scores[_k] * settings.max_comment_points))
        team_q = float(np.mean(list(q_scores.values()))) if q_scores else 1.0

        members: List[StudentResult] = []
        for k in member_keys:
            all_pub = pub.get(k, [])
            if q_scores:
                Q = q_scores.get(k, team_q)
                best_pts = q_points.get(k, int(round(team_q *
                                                     settings.max_comment_points)))
            else:
                Q, best_pts = 1.0, 0      # nothing written anywhere: no dampening

            rec = received.get(k, [])
            received_total = float(np.sum(rec)) if rec else 0.0
            ravg = received_avgs.get(k)
            A = agreement_weight(rec, expected_share, agreement_ref)
            # Nobody evaluated this student at all: no dollars, no ratings, no
            # ranking. There is nothing to grade them on, so they keep the team
            # score and the instructor gets told who they are.
            no_peer_input = (not rec and not any(dims[k]) and not ranks_recv[k])

            # ---- dimension grades from the rating matrix ----
            peer_vals = [float("nan")] * 4
            for d in range(4):
                if dims[k][d]:
                    peer_vals[d] = float(np.mean(dims[k][d])) / 7.0
            dim_letters = [letter_grade(v) for v in peer_vals]

            # ---- performance: forced ranking if we have it, else allocation ----
            rmean = float(np.mean(ranks_recv[k])) if ranks_recv[k] else float("nan")
            forced = _forced_performance(rmean)
            if no_peer_input:
                perf = NO_DATA
            elif forced is not None:
                perf = forced
            else:
                perf = _performance_label(settings.performance_method,
                                          ravg if ravg is not None else team_avg,
                                          team_avg,
                                          settings.performance_band,
                                          rank_of.get(k, n), n)
                if settings.agreement_guard and perf == "Low" and A <= 0.5:
                    perf = "Expected"

            # ---- pay grade = share of the team average ($100 allocation) ----------
            # Always shown to students (100% = an even share). Its ratio also drives
            # the grade when the "allocation" method is chosen.
            pay_grade = (ravg / team_avg) if (ravg is not None and team_avg > 0) \
                else float("nan")

            # ---- rel = the chosen peer factor (1.0 = team average) ----------------
            # A student ABOVE the team average (rel > 1) earns a bonus; BELOW takes a
            # deduction; exactly average gets no change. Being relative to the team
            # average, self-allocations or abstentions can't push a whole team down.
            paf_alloc = _factor(alloc_source.get(k), alloc_team)
            paf_rating = _factor(member_rating.get(k), rating_team)
            paf_rank = _factor(member_rankscore.get(k), rank_team)
            src = settings.adjustment_source
            if src == "rating":
                rel = paf_rating if paf_rating is not None else (paf_alloc or 1.0)
            elif src == "ranking":
                rel = paf_rank if paf_rank is not None else (paf_alloc or 1.0)
            elif src == "combined":
                _parts = [p for p in (paf_alloc, paf_rating) if p is not None]
                rel = (sum(_parts) / len(_parts)) if _parts else 1.0
            else:  # allocation (default)
                rel = paf_alloc if paf_alloc is not None else 1.0
            peer_ratio = rel

            # Grade adjustment centred on the team average, scaled by sensitivity B,
            # then dampened by evaluator agreement A and comment support Q (both ≤ 1,
            # so they only shrink the adjustment — they never flip its sign).
            raw_mult = 1 + settings.sensitivity_B * A * Q * (rel - 1)
            mult = max(settings.min_multiplier, min(settings.max_multiplier, raw_mult))
            if no_peer_input:
                mult = 1.0
            # The rounding setting decides the grade, not just the label on it.
            # Rounding only the displayed delta meant a student could be told
            # "+5%" while the gradebook held 104.44 — two numbers for one
            # student, and the instructor had no way to tell which was real.
            # The rounded delta is applied, then re-capped, so what the report
            # says is what the score is.
            signed = _round_pct(ts * mult - ts, settings.rounding_step,
                                settings.rounding_mode)
            signed = min(max(signed, (settings.min_multiplier - 1) * ts),
                         (settings.max_multiplier - 1) * ts)
            signed = signed + 0.0           # turns -0.0 into 0.0, so no "+-0%"
            individual = ts + signed
            mult = (individual / ts) if ts else mult

            # ---- self evaluation ----
            self_vals = [float("nan")] * 4
            self_responded = False
            se = self_evals.get((team, k))
            if se and se.get("ratings"):
                for d, v in enumerate(se["ratings"][:4]):
                    vv = _num(v)
                    if vv is not None:
                        self_vals[d] = vv / 7.0
                self_responded = any(not math.isnan(x) for x in self_vals)
            self_letters = [letter_grade(v) for v in self_vals]

            # ---- response quality: score the feedback THIS student WROTE ----
            # The average across the teammates they were asked about, not their
            # best single comment. Taking the best meant one careful comment and
            # "fine" for the other three scored full marks, and — with the old
            # coarse scorer — every student landed on the same number anyway.
            # Teammates they wrote nothing about count as zero, which is the
            # difference between scoring effort and scoring one good sentence.
            my_written = authored.get(k, [])
            others_written = [c for kk, cl in authored.items() if kk != k for c in cl]
            slots = max(authored_slots.get(k, 0), len(my_written))
            if slots:
                total_q = sum(
                    score_comment(c, others_written,
                                  settings.max_comment_points).q
                    for c in my_written)
                rq = total_q / slots
                rpts = int(round(rq * settings.max_comment_points))
            else:
                rq, rpts = float("nan"), 0
            response_Q = rq

            recv_contrib = contrib.get(k, []) if has_texts else list(all_pub)
            recv_improve = improve.get(k, []) if has_texts else []

            members.append(StudentResult(
                name=display_name.get(k, k), key=k, team=team, team_score=ts,
                received_total=round(received_total, 2), expected_share=round(expected_share, 2),
                peer_ratio=round(peer_ratio, 3), A=A, Q=round(Q, 3), multiplier=round(mult, 4),
                individual_score=round(individual, 2), signed_pct=signed, performance=perf,
                comment_points=best_pts, submitted_self_eval=(k in submitters),
                no_peer_input=no_peer_input,
                pay_grade=pay_grade, peer_vals=peer_vals,
                team_player=dim_letters[0], quantity=dim_letters[1],
                quality=dim_letters[2], effect=dim_letters[3],
                forced_rank_mean=rmean,
                self_responded=self_responded, self_vals=self_vals,
                self_team_player=self_letters[0], self_quantity=self_letters[1],
                self_quality=self_letters[2], self_effect=self_letters[3],
                self_gap=self_peer_gap(self_vals, peer_vals),
                public_comments=all_pub, contributions=recv_contrib, improvements=recv_improve,
                confidential_comments=conf.get(k, []),
                received_breakdown=[round(x, 1) for x in rec],
                response_Q=(round(response_Q, 3) if not math.isnan(response_Q) else float("nan")),
                response_points=rpts))
        members.sort(key=lambda m: m.name.lower())
        results.append(TeamResult(team=team, members=members, team_score=ts))

    results.sort(key=lambda t: t.team.lower())
    return results


_SOURCE_LABEL = {
    "allocation": ("the $100 allocation", "Allocate $100 among your teammates"),
    "rating": ("the rating matrix", "Rating matrix — 4 statements"),
    "ranking": ("the forced ranking", "Forced ranking — High / Adequate / Low"),
    "combined": ("the $100 allocation and the rating matrix",
                 "Allocate $100 among your teammates"),
}


def missing_adjustment_source(teams: List["TeamResult"],
                              settings: Optional[GradeSettings] = None) -> str:
    """"Nothing to compute the adjustment from", said plainly, or "".

    Every grade adjustment is a deviation from the team average, measured on
    one chosen peer measure (`settings.adjustment_source`). When that measure
    is absent from the responses, every deviation is zero by construction —
    which the results tab shows as five identical columns and no explanation.
    This is the explanation.

    Separated from `adjustment_note` because it is a different kind of answer:
    not "your settings are conservative" but "this survey did not ask the
    question", and the remedy is in the survey design, not the grading rules.
    """
    members = [m for t in teams for m in t.members]
    if not members:
        return ""
    settings = settings or GradeSettings()
    src = settings.adjustment_source
    # peer_ratio is the normalized peer factor actually used. Exactly 1.0 for
    # every student means no student deviated from their team's average on the
    # chosen measure — either because nothing was collected, or because every
    # rater really did split the work evenly.
    if any(abs(m.peer_ratio - 1.0) > 1e-9 for m in members):
        return ""
    label, question = _SOURCE_LABEL.get(src, (src, src))
    collected = any(m.pay_grade == m.pay_grade for m in members)  # not NaN
    if src in ("allocation", "combined") and not collected:
        return (
            f"**No grade adjustment was possible: this evaluation did not "
            f"collect {label}.** Grades are set from the deviation of each student's "
            f"share from their team's average, so with no shares there is "
            f"nothing to deviate from and every student keeps the team score. "
            f"Switch on “{question}” in Survey design (or map that "
            f"column when importing), or pick a different measure under "
            f"“What drives the grade adjustment” on the Grading "
            f"rules tab — the ratings and the forced ranking can both "
            f"drive it instead.")
    if src in ("rating", "ranking"):
        return (
            f"**No grade adjustment was possible: this evaluation has no "
            f"usable data for {label}.** The grade adjustment is set from it, so with none "
            f"collected every student keeps the team score. Switch on "
            f"“{question}” in Survey design, or choose a different "
            f"measure under “What drives the grade adjustment” on the "
            f"Grading rules tab.")
    return (
        f"**Every student's share of {label} came out exactly at their team's "
        f"average**, so no grade moved. That is a real result, not a missing "
        f"one: it happens when every rater splits the points evenly. The "
        f"dimension grades, performance labels and written feedback below are "
        f"unaffected.")


def adjustment_note(teams: List["TeamResult"],
                    settings: Optional[GradeSettings] = None) -> str:
    """Why is every grade adjustment zero? Said plainly, or "" when some moved.

    A section where nothing moved looks like a broken report — it is the shape
    the instructor sees when a column is "the same for all students". Usually
    nothing is broken: the adjustment is deliberately scaled by sensitivity, by
    evaluator agreement and by how well the written comments support the
    ratings, and then rounded. Three factors of about a half, a quarter and a
    rounding step of 5% multiply out to nothing for a normal section.

    So rather than leave the instructor to infer that, the arithmetic is stated
    with their own numbers, along with the two settings that change it.

    But the arithmetic is only the answer when there *was* an input to scale.
    The commonest cause of a flat section is that the measure the adjustment is
    computed from was never collected — the survey did not ask for the $100
    allocation, or the imported export has no column for it — and this used to
    answer that case with "the furthest anyone sat from their team's average
    was 0%, so at sensitivity 0.5 …", which blames the settings for missing
    data. That case is now named first, because it is the one the instructor
    can actually fix.
    """
    members = [m for t in teams for m in t.members]
    if not members or any(abs(m.signed_pct) > 1e-9 for m in members):
        return ""
    settings = settings or GradeSettings()

    missing = missing_adjustment_source(teams, settings)
    if missing:
        return missing

    qs = [m.Q for m in members if m.Q == m.Q]
    spread = [m.pay_grade for m in members if m.pay_grade == m.pay_grade]
    mean_q = (sum(qs) / len(qs)) if qs else 1.0
    widest = max((abs(p - 1.0) for p in spread), default=0.0)
    implied = settings.sensitivity_B * mean_q * widest * 100.0
    step = int(settings.rounding_step or 0)
    bits = [f"No student's grade moved from the team score."]
    bits.append(
        f"The furthest anyone sat from their team's average was "
        f"{widest:.0%}, and written support averaged {mean_q:.2f}, so at "
        f"sensitivity {settings.sensitivity_B:g} the largest adjustment this "
        f"section could produce was about {implied:.1f}%")
    if step:
        bits.append(f"which rounds to 0 in {step}% steps.")
        if step > 1:
            bits.append("Switch rounding to 1% steps on the Grading rules tab "
                        "to let differences this size reach the grade, or raise "
                        "sensitivity.")
    else:
        bits.append("— too small to show.")
        bits.append("Raise sensitivity on the Grading rules tab to widen it.")
    return " ".join(bits)


def data_warnings(long_df: pd.DataFrame,
                  teams: Optional[List[TeamResult]] = None,
                  roster_names: Optional[List[str]] = None) -> List[str]:
    """Problems in the responses that change the grades, in plain English.

    These are the failures that used to be invisible: a name typed differently
    in one response becomes an extra team member with no ratings; a student
    listed on two teams merges the two; nobody evaluating a student used to hand
    them the largest deduction the settings allow. Each one is still *handled*
    conservatively — but the instructor is the only person who can actually fix
    the cause, so they have to be told it happened.
    """
    out: List[str] = []
    if long_df is None or long_df.empty:
        return out

    if {"evaluator_key", "evaluator_team"} <= set(long_df.columns):
        pairs = (long_df.dropna(subset=["evaluator_team"])
                 .groupby("evaluator_key")["evaluator_team"]
                 .apply(lambda s: sorted({str(x) for x in s if str(x).strip()})))
        for key, team_list in pairs.items():
            if len(team_list) > 1:
                shown = long_df.loc[long_df["evaluator_key"] == key, "evaluator"]
                name = str(shown.iloc[0]) if len(shown) else key
                out.append(
                    f"**{name}** is listed on more than one team "
                    f"({', '.join(team_list)}). Everything they submitted has been "
                    f"counted under {team_list[0]}. Fix the team column and reload.")

    if roster_names:
        from .ingest import name_key          # local: keeps the import one-way
        known = {name_key(n) for n in roster_names if str(n).strip()}
        seen: Dict[str, str] = {}
        for col_key, col_name in (("evaluatee_key", "evaluatee"),
                                  ("evaluator_key", "evaluator")):
            if col_key in long_df.columns and col_name in long_df.columns:
                for k, nm in zip(long_df[col_key], long_df[col_name]):
                    if str(k).strip() and str(k) not in known:
                        seen.setdefault(str(k), str(nm))
        if seen:
            all_keys = {str(k) for col in ("evaluatee_key", "evaluator_key")
                        if col in long_df.columns for k in long_df[col]
                        if str(k).strip()}
            names = ", ".join(sorted(seen.values())[:8])
            more = f" (+{len(seen) - 8} more)" if len(seen) > 8 else ""
            if all_keys and len(seen) > len(all_keys) / 2:
                # Nearly nothing matched: this is not a typo, it is two files
                # that write names differently. Listing 40 names would bury that.
                out.append(
                    f"Almost none of the names in the responses match your contact "
                    f"list ({len(seen)} of {len(all_keys)}), so the two files are "
                    f"probably writing names differently — \"{sorted(seen.values())[0]}\" "
                    f"here against \"{sorted(roster_names)[0]}\" there. Grading still "
                    f"works from the responses alone; matching them up is what lets "
                    f"PeerParley email the reports.")
            else:
                out.append(
                    f"These names appear in the responses but not in your contact list: "
                    f"**{names}**{more}. Each one is being graded as a separate team "
                    f"member. Usually it is a spelling difference — correct it in the "
                    f"export or the contact list so the two match.")

    if teams:
        blind = [m.name for t in teams for m in t.members if m.no_peer_input]
        if blind:
            names = ", ".join(sorted(blind)[:8])
            more = f" (+{len(blind) - 8} more)" if len(blind) > 8 else ""
            out.append(
                f"No teammate evaluated **{names}**{more}. They keep the team score "
                f"with no adjustment, and they are left out of their team's average.")
    return out


def _pct(x) -> str:
    try:
        if math.isnan(float(x)):
            return ""
    except (TypeError, ValueError):
        return ""
    return f"{x:.0%}"


# --------------------------------------------------------------------------- #
# What every column means
# --------------------------------------------------------------------------- #
# Written once, used three times: the column tooltips in the results table, the
# glossary under it, and the legend on the section-summary PDF. Keeping one
# copy is the point — the PDF legend and the on-screen table had drifted apart,
# and a column whose meaning you have to reverse-engineer from the source is a
# column an instructor cannot defend to a student who asks.
COLUMN_HELP: Dict[str, str] = {
    "Team": "The team this student was evaluated within.",
    "Name": "The student, as their teammates' responses spell it. A name that "
            "appears twice with different spellings becomes two rows — the "
            "warnings above the table say so when it happens.",
    "Team Player": "Peers' average answer to the first rating statement, on "
                   "the 7-point scale, shown as a letter grade. Blank means no "
                   "rating matrix was collected.",
    "Quantity": "Peers' average answer to the second rating statement — how "
                "much work the student did — as a letter grade.",
    "Quality": "Peers' average answer to the third rating statement — how good "
               "that work was — as a letter grade.",
    "Effect": "Peers' average answer to the fourth rating statement — the "
              "effect the student had on the team — as a letter grade.",
    "Pay Grade": "The student's share of the $100 their teammates allocated, "
                 "as a percentage of an even share. 100% is an even share; "
                 "125% means they were given a quarter more than even. Empty "
                 "(a dash on the PDF) means no allocation was collected.",
    "Peer Factor": "Pay Grade divided by the team's average, which is the "
                   "number the grade adjustment is actually computed from. "
                   "1.000 is exactly average. Which measure feeds it — the "
                   "allocation, the ratings or the ranking — is set on the "
                   "Grading rules tab.",
    "A (agreement)": "How much the student's own raters agreed with each "
                     "other, compared with how much raters in this section "
                     "agreed on average. 1.00 means as much or more; lower "
                     "means their raters were unusually split, so the "
                     "adjustment is trusted less. Never below 0.50, and it "
                     "only ever shrinks an adjustment — it cannot flip it.",
    "Q (support)": "How well the written comments about this student support "
                   "the numbers given for them, from 0 to 1, averaged over the "
                   "comments they received. Like A, it only shrinks the "
                   "adjustment. 1.00 for everyone means the survey collected "
                   "no written feedback, so this dampener is switched off.",
    "Response Pts": "What the student earned for the feedback THEY wrote about "
                    "their teammates, out of the maximum on the Grading rules "
                    "tab. Averaged over every teammate they were asked about, "
                    "so skipping three and writing one good comment does not "
                    "score full marks. This is the only column about their own "
                    "effort as an evaluator.",
    "Individual Score": "The student's score: the team score plus the grade "
                        "change. This is the number that goes in the gradebook.",
    "Grade Δ": "Individual Score minus the team score, in points. Rounded to "
               "the step set on the Grading rules tab, and the rounded figure "
               "is what is applied, so the report and the gradebook always "
               "agree.",
    "Performance": "High / Adequate / Expected / Low. When the survey "
                   "collected the forced ranking, this is the label the "
                   "student's teammates settled on: their average rank "
                   "(High=1, Adequate=2, Low=3) rounded to the nearest of the "
                   "three, so two Adequates and one Low is Adequate. Without a "
                   "ranking it comes from how far the student sat from their "
                   "team's average. “No data” means no teammate "
                   "evaluated them at all.",
    "Self vs peers": "How the student's view of themselves compares with their "
                     "team's. For each rating statement they answered about "
                     "themselves, their own rating minus their peers' average "
                     "for it; averaged across the four statements and shown in "
                     "points of the 7-point scale. “+1.2 (higher)” "
                     "means they rated themselves 1.2 points above their team, "
                     "on average; “in line” is within half a point "
                     "either way. A dash means they did not rate themselves, "
                     "or the survey collected no rating matrix to compare "
                     "with.",
}


def results_to_frame(teams: List[TeamResult]) -> pd.DataFrame:
    rows = []
    for t in teams:
        for m in t.members:
            rows.append({
                "Team": t.team,
                "Name": m.name,
                "Team Player": m.team_player,
                "Quantity": m.quantity,
                "Quality": m.quality,
                "Effect": m.effect,
                "Pay Grade": _pct(m.pay_grade),
                # The number the adjustment is computed from was the one column
                # NOT in this table, which made the three columns downstream of
                # it impossible to check: an instructor could see the
                # multiplier but not what produced it.
                "Peer Factor": round(m.peer_ratio, 3),
                "A (agreement)": m.A,
                "Q (support)": m.Q,
                "Response Pts": m.response_points,
                # The multiplier used to sit here, beside the score and the
                # delta. All three are the same fact: score = team × multiplier
                # = team + delta, so two of the three were restating the first
                # in different units and taking up width the table needed for
                # something that varies. The delta is the one an instructor
                # reads (it is in gradebook points and carries a sign), so the
                # multiplier is gone. `StudentResult.multiplier` is unchanged
                # for anything that needs the factor itself.
                "Individual Score": m.individual_score,
                "Grade Δ": f"{'+' if m.signed_pct >= 0 else ''}{m.signed_pct:g}%",
                "Performance": m.performance,
                # Was "Self-eval?" — first `submitted_self_eval`, which is true
                # for anyone who submitted at all, then `self_responded`, which
                # at least varied but still only said yes or no. The question
                # worth a column is how far their view of themselves sits from
                # their team's; a dash answers the yes/no on the way past.
                "Self vs peers": describe_self_gap(m.self_gap),
            })
    return pd.DataFrame(rows)


def constant_columns(frame: pd.DataFrame) -> List[str]:
    """Which columns have the same value in every row?

    "These numbers are the same for all students" is a bug report PeerParley
    received more than once, and the honest answer varies: a column can be
    constant because the survey never asked the question, because the rounding
    step is wider than the differences, or because the students really were
    indistinguishable. The report cannot tell which without saying it, so the
    results tab names the constant columns and points at the explanation.
    """
    skip = {"Team", "Name"}
    return [c for c in frame.columns
            if c not in skip and frame[c].astype(str).nunique(dropna=False) <= 1]


def trim_constant_columns(frame: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, str]]:
    """Drop the columns that say the same thing about every student.

    A column with one value in it carries no information about any individual,
    and across fourteen columns on a landscape page the ones that do carry
    information are what get squeezed. So they come out of the displayed table
    and the PDF — and the single value comes back as a caption, because "every
    student scored the same" is itself worth knowing and silently dropping it
    would be worse than printing it fourteen times.

    Returns the trimmed frame and {column: the one value}, in column order.
    The CSV export is deliberately left whole: it is the archival copy.

    Not applied to a section of one student, where every column is constant by
    arithmetic rather than by anything about the data.
    """
    if len(frame) < 2:
        return frame, {}
    dropped = {c: str(frame[c].iloc[0]) for c in constant_columns(frame)}
    if not dropped:
        return frame, {}
    return frame.drop(columns=list(dropped)), dropped

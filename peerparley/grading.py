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
    rounding_step: int = 5          # 1 or 5 (percent)
    rounding_mode: str = "nearest"  # nearest | up | down
    performance_method: str = "allocation_ratio"  # allocation_ratio|rank_linear|rank_one_mean
    performance_band: float = 0.08  # +-8%
    agreement_guard: bool = True
    # Which peer measure drives the grade adjustment (the normalized peer factor):
    #   allocation | rating | ranking | combined
    adjustment_source: str = "allocation"
    normalize_raters: bool = False   # z-score each evaluator (corrects for leniency)


def agreement_weight(received: List[float], expected_share: float) -> float:
    vals = [v for v in received if v == v]  # drop NaN
    if len(vals) < 2 or expected_share <= 0:
        return 1.0
    sd = float(np.std(vals, ddof=0))
    frac = sd / expected_share
    if frac <= 0.10:
        return 1.00
    if frac <= 0.20:
        return 0.75
    if frac <= 0.30:
        return 0.50
    return 0.25


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
    """Forced ranking: 1=High, 2=Adequate, 3=Low → High/Adequate/Low."""
    if mean_rank is None or (isinstance(mean_rank, float) and math.isnan(mean_rank)):
        return None
    v = (3.0 - mean_rank) / 2.0          # rank 1 → 1.0, 2 → 0.5, 3 → 0.0
    if v >= 0.8:
        return "High"
    if v >= 0.4:
        return "Adequate"
    return "Low"


def _num(v):
    try:
        f = float(v)
        return f if not math.isnan(f) else None
    except (TypeError, ValueError):
        return None


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

        members: List[StudentResult] = []
        for k in member_keys:
            all_pub = pub.get(k, [])
            others = [c for kk, cl in pub.items() if kk != k for c in cl]
            best_q, best_pts = 0.0, 0
            for c in all_pub:
                cs = score_comment(c, others, settings.max_comment_points)
                if cs.q >= best_q:
                    best_q, best_pts = cs.q, cs.points
            Q = best_q if all_pub else 1.0   # neutral when no comments were written about them

            rec = received.get(k, [])
            received_total = float(np.sum(rec)) if rec else 0.0
            ravg = received_avgs.get(k)
            A = agreement_weight(rec, expected_share)
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
            my_written = authored.get(k, [])
            others_written = [c for kk, cl in authored.items() if kk != k for c in cl]
            rq, rpts = 0.0, 0
            for c in my_written:
                cs = score_comment(c, others_written, settings.max_comment_points)
                if cs.q >= rq:
                    rq, rpts = cs.q, cs.points
            response_Q = rq if my_written else float("nan")

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
                public_comments=all_pub, contributions=recv_contrib, improvements=recv_improve,
                confidential_comments=conf.get(k, []),
                received_breakdown=[round(x, 1) for x in rec],
                response_Q=(round(response_Q, 3) if not math.isnan(response_Q) else float("nan")),
                response_points=rpts))
        members.sort(key=lambda m: m.name.lower())
        results.append(TeamResult(team=team, members=members, team_score=ts))

    results.sort(key=lambda t: t.team.lower())
    return results


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
                "A (agreement)": m.A,
                "Q (support)": m.Q,
                "Response Pts": m.response_points,
                "Multiplier": m.multiplier,
                "Individual Score": m.individual_score,
                "Grade Δ": f"{'+' if m.signed_pct >= 0 else ''}{m.signed_pct:g}%",
                "Performance": m.performance,
                "Self-eval?": "Yes" if m.submitted_self_eval else "No",
            })
    return pd.DataFrame(rows)

"""The instructor summary's per-student metrics, and the score behind them.

Reported from use: *"in the instructor summary the metrics don't seem to be
reporting correctly, they are same for all students regardless of input."*

Measured, that was true of four columns, and all four came from one function.
`score_comment` was `passed / 5` over five all-or-nothing thresholds, so it
could return only six values — and nine of ten realistic comments returned
exactly 0.20, a one-word "fine" among them, because a single word trivially
passes the "variety" check. That constant was the **Q** column, it was the
**Resp.Pts** column, and because Q multiplies the grade adjustment it removed
about four fifths of every adjustment, which the 5% rounding step then took to
zero — flattening **Mult** and **Grade Δ** for the whole section.

These tests hold the resolution in place, and hold the two aggregation
decisions that came with the fix: Q is the average of the comments written
about a student (not the best one, which made it a property of the team), and
the feedback points are the average across the teammates a student was asked
about (not their single best comment).

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from peerparley import pdfgen  # noqa: E402
from peerparley.comments import score_comment  # noqa: E402
from peerparley.grading import (  # noqa: E402
    GradeSettings,
    adjustment_note,
    compute,
)

# Ten comments of the kind students actually write, ordered best to worst.
LADDER = [
    "Ann chaired every meeting, kept the schedule current, and chased the rest "
    "of us politely when we drifted. She also rewrote my section the night "
    "before it was due.",
    "Bob rebuilt the financial model twice after the data changed and explained "
    "the assumptions so we could follow them.",
    "Cid ran all six customer interviews and wrote up the transcripts for "
    "everyone else to use.",
    "He did a good job on his parts and was easy to work with.",
    "Always on time and well prepared.",
    "Did the analysis section.",
    "Good teammate.",
    "fine",
]


# --------------------------------------------------------------------------- #
# The score itself
# --------------------------------------------------------------------------- #

def test_the_comment_score_can_tell_real_comments_apart():
    """The defect, stated as a number: nine of ten used to score 0.20."""
    scores = [score_comment(c, others=(), max_points=10).q for c in LADDER]
    assert len(set(scores)) >= 7, dict(zip(LADDER, scores))
    most_common = max(set(scores), key=scores.count)
    assert scores.count(most_common) <= 2


def test_a_better_comment_always_scores_at_least_as_well_as_a_worse_one():
    scores = [score_comment(c, others=(), max_points=10).q for c in LADDER]
    assert scores == sorted(scores, reverse=True), list(zip(LADDER, scores))


def test_a_one_word_comment_scores_near_zero():
    """It used to score 0.20 — the same as a thoughtful sentence — because one
    word is trivially "varied"."""
    for text in ("fine", "n/a", "good", "ok"):
        cs = score_comment(text, others=(), max_points=10)
        assert cs.q < 0.15, (text, cs.q)
        assert cs.points <= 1
    assert score_comment("", others=(), max_points=10).q == 0.0


def test_a_substantial_specific_comment_scores_well():
    cs = score_comment(LADDER[0], others=(), max_points=10)
    assert cs.q > 0.6
    assert cs.points >= 6


def test_the_score_still_catches_padding_and_copy_paste():
    padded = "good " * 40
    assert score_comment(padded, others=(), max_points=10).q < 0.35
    assert any("repetition" in f
               for f in score_comment(padded, others=(), max_points=10).flags)

    real = LADDER[1]
    pasted = score_comment(real, others=(real.replace("Bob", "Cid"),),
                           max_points=10)
    alone = score_comment(real, others=(), max_points=10)
    assert pasted.q < alone.q
    assert any("duplicate" in f for f in pasted.flags)


def test_the_flags_say_what_is_missing_in_words():
    flags = score_comment("Good teammate.", others=(), max_points=10).flags
    assert any("very short" in f for f in flags)
    assert any("nothing specific" in f for f in flags)


# --------------------------------------------------------------------------- #
# How the score becomes a per-student metric
# --------------------------------------------------------------------------- #

def section(comment_by_author: dict, dollars: dict | None = None,
            ratings: dict | None = None) -> pd.DataFrame:
    """A team where each student writes `comment_by_author[name]` about everyone."""
    names = list(comment_by_author)
    rows = []
    for ev in names:
        for ee in names:
            if ev == ee:
                continue
            row = {"evaluator": ev, "evaluator_key": ev.lower(),
                   "evaluator_team": "1", "evaluatee": ee,
                   "evaluatee_key": ee.lower(),
                   "points": (dollars or {}).get(ev, {}).get(ee, 100 / (len(names) - 1)),
                   "public_comment": comment_by_author[ev].format(who=ee)}
            for d in range(4):
                row[f"r{d}"] = (ratings or {}).get(ee, [5, 5, 5, 5])[d]
            rows.append(row)
    return pd.DataFrame(rows)


def test_one_articulate_teammate_no_longer_lifts_everyones_support_score():
    """Q was the *best* comment written about a student, so the team's best
    writer set everybody's Q — which is why the column could not vary."""
    df = section({
        "Ann": "{who} " + LADDER[0],      # writes carefully about everyone
        "Bob": "{who} fine",
        "Cid": "{who} ok",
        "Dee": "{who} good",
    })
    members = {m.name: m for t in compute(df, GradeSettings()) for m in t.members}
    qs = {n: round(m.Q, 3) for n, m in members.items()}
    # Everybody receives Ann's good comment, so a max would make these equal.
    assert len(set(qs.values())) > 1, qs
    # And everyone's Q reflects the average of what was written about them,
    # which here is mostly thin.
    assert all(m.Q < 0.5 for m in members.values()), qs


def test_support_differs_when_the_comments_about_students_differ():
    df = section({
        "Ann": "{who} did the thing",
        "Bob": "{who} did the thing",
        "Cid": "{who} did the thing",
        "Dee": "{who} did the thing",
    })
    # Rewrite what everyone says about Ann specifically, in detail.
    df.loc[df["evaluatee"] == "Ann", "public_comment"] = LADDER[0]
    members = {m.name: m for t in compute(df, GradeSettings()) for m in t.members}
    assert members["Ann"].Q > members["Bob"].Q + 0.2


def test_a_numbers_only_survey_is_not_dampened_at_all():
    """With no written feedback anywhere, Q must stay neutral or nothing could
    ever move a grade — a survey with the comment questions switched off still
    has to grade."""
    df = section({n: "" for n in ("Ann", "Bob", "Cid", "Dee")},
                 dollars={"Ann": {"Bob": 60, "Cid": 20, "Dee": 20},
                          "Bob": {"Ann": 60, "Cid": 20, "Dee": 20},
                          "Cid": {"Ann": 60, "Bob": 20, "Dee": 20},
                          "Dee": {"Ann": 60, "Bob": 20, "Cid": 20}})
    members = {m.name: m for t in compute(df, GradeSettings()) for m in t.members}
    assert all(m.Q == 1.0 for m in members.values())
    assert members["Ann"].signed_pct > 0        # the dollars still reach the grade


def test_a_student_nobody_wrote_about_takes_the_team_average():
    """Not a zero they did not earn, and not a free pass either."""
    df = section({n: "{who} " + LADDER[2] for n in ("Ann", "Bob", "Cid", "Dee")})
    df.loc[df["evaluatee"] == "Dee", "public_comment"] = ""
    members = {m.name: m for t in compute(df, GradeSettings()) for m in t.members}
    others = [m.Q for n, m in members.items() if n != "Dee"]
    assert members["Dee"].Q == pytest.approx(sum(others) / len(others), abs=0.05)
    assert 0.0 < members["Dee"].Q < 1.0


# --------------------------------------------------------------------------- #
# Points for the feedback a student wrote
# --------------------------------------------------------------------------- #

def test_feedback_points_average_across_teammates_rather_than_taking_the_best():
    """One careful comment and "fine" for the rest used to score full marks."""
    df = section({
        "Ann": "{who} " + LADDER[0],       # careful about everyone
        "Bob": "{who} fine",               # nothing for anyone
        "Cid": "{who} " + LADDER[2],       # solid about everyone
        "Dee": "{who} " + LADDER[0],
    })
    # Bob writes one good comment, about Ann only.
    df.loc[(df["evaluator"] == "Bob") & (df["evaluatee"] == "Ann"),
           "public_comment"] = LADDER[0]
    members = {m.name: m for t in compute(df, GradeSettings()) for m in t.members}
    assert members["Bob"].response_points < members["Ann"].response_points
    assert members["Bob"].response_points < members["Cid"].response_points
    assert members["Ann"].response_points >= 6


def test_skipping_teammates_costs_points():
    full = section({n: "{who} " + LADDER[1] for n in ("Ann", "Bob", "Cid", "Dee")})
    partial = full.copy()
    partial.loc[(partial["evaluator"] == "Bob")
                & (partial["evaluatee"] != "Ann"), "public_comment"] = ""
    a = {m.name: m for t in compute(full, GradeSettings()) for m in t.members}
    b = {m.name: m for t in compute(partial, GradeSettings()) for m in t.members}
    assert b["Bob"].response_points < a["Bob"].response_points
    assert b["Bob"].response_Q == pytest.approx(a["Bob"].response_Q / 3, abs=0.05)


# --------------------------------------------------------------------------- #
# Saying so when nothing moved
# --------------------------------------------------------------------------- #

def test_a_flat_section_explains_itself_with_the_instructors_own_numbers():
    df = section({n: "{who} " + LADDER[4] for n in ("Ann", "Bob", "Cid", "Dee")},
                 dollars={"Ann": {"Bob": 34, "Cid": 33, "Dee": 33},
                          "Bob": {"Ann": 35, "Cid": 33, "Dee": 32},
                          "Cid": {"Ann": 35, "Bob": 33, "Dee": 32},
                          "Dee": {"Ann": 34, "Bob": 33, "Cid": 33}})
    settings = GradeSettings(rounding_step=5)
    teams = compute(df, settings)
    assert all(m.signed_pct == 0 for t in teams for m in t.members)
    note = adjustment_note(teams, settings)
    assert "No student's grade moved" in note
    assert "5% steps" in note
    assert "1% steps" in note                     # what to do about it
    assert "written support averaged" in note


def test_nothing_is_said_when_adjustments_did_move():
    df = section({n: "{who} " + LADDER[0] for n in ("Ann", "Bob", "Cid", "Dee")},
                 dollars={"Ann": {"Bob": 60, "Cid": 20, "Dee": 20},
                          "Bob": {"Ann": 60, "Cid": 20, "Dee": 20},
                          "Cid": {"Ann": 60, "Bob": 20, "Dee": 20},
                          "Dee": {"Ann": 60, "Bob": 20, "Cid": 20}})
    teams = compute(df, GradeSettings())
    assert any(m.signed_pct != 0 for t in teams for m in t.members)
    assert adjustment_note(teams, GradeSettings()) == ""


# --------------------------------------------------------------------------- #
# The summary PDF itself — the report the complaint was about
# --------------------------------------------------------------------------- #

def test_the_instructor_summary_shows_different_numbers_for_different_students():
    """Reads the finished PDF, because that is what the instructor looked at."""
    fitz = pytest.importorskip("fitz", reason="PyMuPDF is an optional extra")
    df = section(
        {"Ann": "{who} " + LADDER[0], "Bob": "{who} " + LADDER[2],
         "Cid": "{who} fine", "Dee": "{who} " + LADDER[3]},
        dollars={"Ann": {"Bob": 34, "Cid": 10, "Dee": 56},
                 "Bob": {"Ann": 60, "Cid": 10, "Dee": 30},
                 "Cid": {"Ann": 55, "Bob": 35, "Dee": 10},
                 "Dee": {"Ann": 60, "Bob": 30, "Cid": 10}},
        ratings={"Ann": [7, 7, 7, 7], "Bob": [5, 5, 5, 5],
                 "Cid": [2, 1, 2, 2], "Dee": [6, 4, 6, 3]})
    teams = compute(df, GradeSettings())
    members = {m.name: m for t in teams for m in t.members}

    # Every column the complaint was about now varies between students.
    for field in ("Q", "response_points", "pay_grade", "peer_ratio",
                  "multiplier", "signed_pct", "team_player"):
        values = {getattr(m, field) for m in members.values()}
        assert len(values) > 1, f"{field} is the same for every student: {values}"

    raw = pdfgen.build_section_summary_pdf(teams, "QA101", "1")
    assert raw[:5] == b"%PDF-"
    with fitz.open(stream=raw, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)

    # The values are in the document, not just in the objects behind it.
    assert "Ann" in text and "Cid" in text
    for m in members.values():
        assert f"{m.pay_grade:.0%}" in text, f"{m.name}'s pay grade is missing"
    deltas = {f"{'+' if m.signed_pct >= 0 else ''}{m.signed_pct:g}%"
              for m in members.values()}
    assert len(deltas) > 1 and all(d in text for d in deltas), deltas

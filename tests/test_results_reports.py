"""Results & reports: the columns, their explanations, and the previews.

Reported from use: *"provide explanations for each column of the output. The
individual score, grade change, performance, and self-eval and agreement
scores seem to be the same for everyone. I can't preview the instructor report
on screen (I get the errors). Confidential feedback is empty."*

Four separate defects, each reproduced before it was fixed, and each held here.

**The previews, and the empty confidential report — one cause.**
``render_pdf`` rasterises a report with PyMuPDF and, failing that, offers a
download button instead. The button had no ``key``, so Streamlit derived its id
from its label and arguments: identical for every call. The instructor summary
and the confidential report sit on the same page, so the second call raised
``StreamlitDuplicateElementId`` *while the page was being drawn* — the summary
preview appeared, and everything after it, the confidential report included,
never rendered at all. It only showed where PyMuPDF was absent, which was every
Streamlit Community Cloud deployment, because PyMuPDF was listed in
``requirements-optional.txt`` and Cloud installs only ``requirements.txt``.

**"The same for everyone" — two causes.**
``agreement_weight`` had four fixed bands cut against a *scale* (a fraction of
an even share) rather than against how much raters in the section actually
disagreed, so forty students could land on four values and a normally-split
section put almost everybody on the lowest. And the default rounding step was
5%, which quantises the ±2–8% adjustments a real section produces onto
{-5, 0, +5}.

**Self-eval? never varied**, because it reported ``submitted_self_eval`` —
true for anyone who submitted anything — rather than whether they rated
themselves.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import numpy as np
import pandas as pd
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from peerparley import pdfgen  # noqa: E402
from peerparley.grading import (  # noqa: E402
    AGREEMENT_FLOOR,
    COLUMN_HELP,
    GradeSettings,
    adjustment_note,
    agreement_weight,
    compute,
    constant_columns,
    dispersion_reference,
    missing_adjustment_source,
    results_to_frame,
)


# --------------------------------------------------------------------------- #
# A section that looks like a real one
# --------------------------------------------------------------------------- #
TEAMS = {str(t): [f"Student {t}{i}" for i in range(5)] for t in range(6)}
# What the team broadly thinks each position contributed, before each rater's
# own noise. A real section has a view and disagrees about the margins.
BASE = [34, 27, 22, 17]
STYLES = [
    "{who} rebuilt the model after the data changed and walked us through the "
    "assumptions in the Tuesday meeting.",
    "{who} wrote the deck and took over the literature review when we fell behind.",
    "{who} ran the customer interviews and wrote up every transcript.",
    "{who} did fine.",
    "{who} kept the schedule current and chased the rest of us politely.",
]


def _key(name: str) -> str:
    return name.lower().replace(" ", "_")


def section(alloc: bool = True, comments: bool = True, rank: bool = False,
            spread: float = 5.0, even_split: bool = False,
            seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for team, members in TEAMS.items():
        for pos, me in enumerate(members):
            others = [i for i in range(len(members)) if i != pos]
            raw = {i: max(1.0, BASE[others.index(i)] + rng.normal(0, spread))
                   for i in others}
            total = sum(raw.values())
            for i in others:
                text = STYLES[pos].format(who=members[i]) if comments else ""
                if not alloc:
                    points = float("nan")
                elif even_split:
                    points = 25.0
                else:
                    points = round(raw[i] * 100 / total)
                row = {
                    "evaluator": me, "evaluator_key": _key(me),
                    "evaluator_team": team, "evaluatee": members[i],
                    "evaluatee_key": _key(members[i]), "points": points,
                    "public_comment": text, "contribution_text": text,
                    "improve_text": "", "confidential_comment": "",
                    "rank": float(1 + i % 3) if rank else float("nan"),
                }
                for d in range(4):
                    row[f"r{d}"] = float(rng.integers(3, 8))
                rows.append(row)
    return pd.DataFrame(rows)


def self_evals(every: bool = True) -> dict:
    out = {}
    for team, members in TEAMS.items():
        for pos, name in enumerate(members):
            if every or pos % 2 == 0:
                out[(team, _key(name))] = {"ratings": [5, 6, 5, 6]}
    return out


# --------------------------------------------------------------------------- #
# A (agreement) — resolution, and what it is measured against
# --------------------------------------------------------------------------- #
def test_agreement_is_continuous_not_four_bands():
    """The complaint was a column with two values in it. The old function could
    only ever return 1.00, 0.75, 0.50 or 0.25."""
    teams = compute(section(), GradeSettings())
    values = {m.A for t in teams for m in t.members}
    assert len(values) > 6, (
        f"A took only {len(values)} distinct values over "
        f"{sum(len(t.members) for t in teams)} students: {sorted(values)}")


def test_agreement_is_measured_against_this_section_not_a_fixed_dollar_spread():
    """Four teammates splitting $100 and differing by five dollars is ordinary
    disagreement. Against a fixed 20%-of-an-even-share cut it was penalised; it
    should not be, because it is what the rest of the section does too."""
    df = section(spread=5.0)
    ref = dispersion_reference(df)
    assert ref > 0, "a six-team section should produce a reference spread"
    # The student whose raters are the most ordinary keeps full weight.
    teams = compute(df, GradeSettings())
    assert max(m.A for t in teams for m in t.members) == 1.0


def test_agreement_still_shrinks_when_raters_genuinely_disagree():
    """It is a dampener and has to keep dampening: a student whose raters are
    far more split than the section's norm must lose weight."""
    received = [5.0, 45.0, 10.0, 40.0]          # an even share is 25
    assert agreement_weight(received, 25.0, reference=0.10) == AGREEMENT_FLOOR
    tight = [24.0, 25.0, 26.0, 25.0]
    assert agreement_weight(tight, 25.0, reference=0.10) == 1.0


def test_agreement_never_falls_below_the_floor_and_never_exceeds_one():
    for reference in (0.0, 0.05, 0.15, 0.4):
        for received in ([0.0, 100.0], [25.0] * 4, [1.0, 2.0, 97.0]):
            a = agreement_weight(received, 25.0, reference=reference)
            assert AGREEMENT_FLOOR <= a <= 1.0


def test_a_single_rating_is_not_treated_as_disagreement():
    assert agreement_weight([30.0], 25.0) == 1.0
    assert agreement_weight([], 25.0) == 1.0


def test_a_unanimous_section_does_not_turn_a_dollar_into_a_penalty():
    """If every rater in the section typed the same numbers, the median spread
    is ~0 — and dividing by it would make any difference at all look extreme."""
    df = section(even_split=True)
    teams = compute(df, GradeSettings())
    assert {m.A for t in teams for m in t.members} == {1.0}


# --------------------------------------------------------------------------- #
# The grade columns actually differentiate students
# --------------------------------------------------------------------------- #
def test_the_grade_columns_are_not_the_same_for_everyone():
    """The report as the instructor sees it, with the shipped defaults."""
    frame = results_to_frame(compute(section(), GradeSettings()))
    for column in ("Individual Score", "Grade Δ", "Multiplier",
                   "A (agreement)", "Peer Factor"):
        assert frame[column].astype(str).nunique() >= 8, (
            f"{column} has only {frame[column].astype(str).nunique()} distinct "
            f"values across {len(frame)} students")


def test_the_default_rounding_step_lets_a_real_adjustment_through():
    """5% steps quantise the ±2–8% a section produces onto three values. The
    default is 1%; 5% stays available for instructors who want round numbers."""
    assert GradeSettings().rounding_step == 1
    coarse = results_to_frame(compute(section(), GradeSettings(rounding_step=5)))
    fine = results_to_frame(compute(section(), GradeSettings(rounding_step=1)))
    assert (fine["Grade Δ"].nunique() > coarse["Grade Δ"].nunique())


def test_performance_still_separates_students():
    frame = results_to_frame(compute(section(), GradeSettings()))
    assert frame["Performance"].nunique() >= 2


# --------------------------------------------------------------------------- #
# Self-eval?
# --------------------------------------------------------------------------- #
def test_self_eval_column_asks_whether_they_rated_themselves():
    """It used to report `submitted_self_eval` — "did they submit at all" —
    which is Yes for every student in the table, by construction."""
    teams = compute(section(), GradeSettings(), self_evals=self_evals(every=False))
    frame = results_to_frame(teams)
    assert set(frame["Self-eval?"]) == {"Yes", "No"}
    # And it matches the flag the report actually renders a self-grade from.
    by_name = {m.name: m for t in teams for m in t.members}
    for _, row in frame.iterrows():
        expected = "Yes" if by_name[row["Name"]].self_responded else "No"
        assert row["Self-eval?"] == expected


def test_a_student_who_submitted_but_skipped_their_own_ratings_reads_no():
    teams = compute(section(), GradeSettings(), self_evals={})
    frame = results_to_frame(teams)
    assert set(frame["Self-eval?"]) == {"No"}
    assert all(m.submitted_self_eval for t in teams for m in t.members), (
        "every student in this fixture did submit — which is exactly why the "
        "old column could not vary")


# --------------------------------------------------------------------------- #
# Every column is explained, and the explanations are not a second copy
# --------------------------------------------------------------------------- #
def test_every_results_column_has_a_plain_english_explanation():
    frame = results_to_frame(compute(section(), GradeSettings()))
    missing = [c for c in frame.columns if c not in COLUMN_HELP]
    assert not missing, f"columns with no entry in COLUMN_HELP: {missing}"


def test_no_explanation_is_left_for_a_column_that_no_longer_exists():
    """A glossary that outlives its columns is how the PDF legend drifted out
    of step with the table in the first place."""
    frame = results_to_frame(compute(section(), GradeSettings()))
    stale = [k for k in COLUMN_HELP if k not in frame.columns]
    assert not stale, f"COLUMN_HELP describes columns that are gone: {stale}"


def test_the_explanations_are_sentences_not_labels():
    for column, text in COLUMN_HELP.items():
        assert len(text) > 40, f"{column}: {text!r} restates the heading"
        assert text.strip().endswith("."), f"{column}: not a sentence"


def test_the_peer_factor_that_drives_the_grade_is_in_the_table():
    """The number every adjustment is computed from was the one column missing,
    so the multiplier could not be checked against anything."""
    frame = results_to_frame(compute(section(), GradeSettings()))
    assert "Peer Factor" in frame.columns


# --------------------------------------------------------------------------- #
# A flat column says *which* kind of flat it is
# --------------------------------------------------------------------------- #
def test_no_allocation_collected_is_named_as_the_cause():
    teams = compute(section(alloc=False), GradeSettings())
    note = adjustment_note(teams, GradeSettings())
    assert note
    assert "did not collect" in note and "$100 allocation" in note
    # And not the old answer, which blamed the instructor's sensitivity setting
    # for data that was never collected.
    assert "sensitivity" not in note.split("Grading rules")[0]


def test_an_even_split_by_every_rater_is_named_as_a_real_result():
    teams = compute(section(even_split=True), GradeSettings())
    note = adjustment_note(teams, GradeSettings())
    assert note and "exactly at their team's average" in note
    assert "real result, not a missing one" in note


def test_a_section_whose_grades_did_move_says_nothing():
    teams = compute(section(), GradeSettings())
    assert adjustment_note(teams, GradeSettings()) == ""
    assert missing_adjustment_source(teams, GradeSettings()) == ""


def test_constant_columns_names_the_flat_ones_and_ignores_the_identifiers():
    frame = results_to_frame(compute(section(alloc=False), GradeSettings()))
    flat = constant_columns(frame)
    assert "Individual Score" in flat and "Grade Δ" in flat
    assert "Team" not in flat and "Name" not in flat


# --------------------------------------------------------------------------- #
# The PDFs
# --------------------------------------------------------------------------- #
def _pdf_text(data: bytes) -> str:
    fitz = pytest.importorskip("fitz")
    with fitz.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def test_the_summary_pdf_prints_the_score_that_goes_in_the_gradebook():
    """It showed the multiplier and the signed delta but not the score — the
    one figure an instructor transcribes."""
    teams = compute(section(), GradeSettings())
    text = _pdf_text(pdfgen.build_section_summary_pdf(teams, "QC101", "1"))
    assert "Score" in text
    one = teams[0].members[0]
    assert f"{one.individual_score:g}" in text


def test_the_summary_pdf_explains_every_column_it_prints():
    teams = compute(section(), GradeSettings())
    text = _pdf_text(pdfgen.build_section_summary_pdf(teams, "QC101", "1"))
    assert "What each column means" in text
    # Written from COLUMN_HELP, so a sentence from it has to appear verbatim.
    for column in ("A (agreement)", "Individual Score", "Performance"):
        first_clause = COLUMN_HELP[column].split(",")[0].split(" — ")[0]
        assert first_clause[:40] in text.replace("\n", " "), column


def test_the_summary_pdf_column_headers_fit_on_the_page():
    """The last column used to start 9.4 inches into a 9.5-inch text area, so
    the performance label printed off the edge of the sheet."""
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.units import inch
    from reportlab.pdfbase.pdfmetrics import stringWidth
    usable = landscape(letter)[0] - 2 * pdfgen.MARGIN
    # Mirrors the layout in build_section_summary_pdf: the final column needs
    # room for the longest performance label.
    assert 8.7 * inch + stringWidth("Expected", "Helvetica", 8) <= usable
    # And the old layout did not, which is why it moved.
    assert 9.4 * inch + stringWidth("Expected", "Helvetica", 8) > usable


def test_an_empty_confidential_report_says_so_rather_than_being_blank():
    """"Confidential feedback is empty" has two causes — nobody wrote one, or
    the box was never asked — and a blank sheet cannot tell them apart."""
    teams = compute(section(), GradeSettings())
    assert not any(m.confidential_comments for t in teams for m in t.members)
    text = _pdf_text(pdfgen.build_confidential_pdf(teams, "QC101", "1"))
    assert "No student wrote a confidential note" in text
    assert "Confidential note to the instructor" in text


def test_a_confidential_note_appears_under_the_student_who_wrote_it():
    df = section()
    author = TEAMS["0"][0]
    mask = df["evaluator"] == author
    df.loc[df.index[mask][0], "confidential_comment"] = \
        "Please look at how our team split the work."
    teams = compute(df, GradeSettings())
    text = _pdf_text(pdfgen.build_confidential_pdf(teams, "QC101", "1"))
    assert "Please look at how our team split the work." in text
    assert author in text
    # Not filed against whichever teammate happened to be listed first.
    holders = [m.name for t in teams for m in t.members if m.confidential_comments]
    assert holders == [author]


# --------------------------------------------------------------------------- #
# The previews
# --------------------------------------------------------------------------- #
PREVIEW_PAGE = textwrap.dedent('''
    import builtins, sys
    _real = builtins.__import__
    def guard(name, *a, **kw):
        if name.split(".")[0] in ("fitz", "pymupdf"):
            raise ModuleNotFoundError("No module named %r" % name)
        return _real(name, *a, **kw)
    builtins.__import__ = guard                  # as on Streamlit Cloud
    sys.path.insert(0, {repo!r})
    import streamlit as st
    from peerparley.ui_helpers import render_pdf
    PDF = b"%PDF-1.4\\n1 0 obj<<>>endobj\\ntrailer<<>>\\n%%EOF\\n"
    st.markdown("summary")
    render_pdf(PDF, key="instructor_summary", label="instructor summary")
    st.markdown("confidential")
    render_pdf(PDF, key="instructor_confidential", label="confidential feedback")
    st.markdown("student")
    render_pdf(PDF, key="student_preview", label="one student's report")
    st.markdown("END-OF-PAGE")
''')

PREVIEW_DRIVER = textwrap.dedent('''
    import sys
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(sys.argv[1], default_timeout=120)
    app.run()
    seen = [str(getattr(e, "value", "")) for e in app.markdown]
    print("EXCEPTIONS:", [str(e.value)[:200] for e in app.exception])
    print("BUTTONS:", len(app.get("download_button")))
    print("REACHED-END:", any("END-OF-PAGE" in s for s in seen))
''')


def test_three_pdf_previews_on_one_page_do_not_collide(tmp_path):
    """Reproduces the reported error. Without a `key`, the second preview's
    download button has the same auto-generated id as the first, Streamlit
    raises while drawing the page, and the confidential report below it never
    renders. Run in a subprocess: AppTest drives a whole Streamlit script, and
    its secrets are a process-wide singleton the rest of the suite shares.
    """
    page = tmp_path / "page.py"
    page.write_text(PREVIEW_PAGE.format(repo=REPO), encoding="utf-8")
    driver = tmp_path / "driver.py"
    driver.write_text(PREVIEW_DRIVER, encoding="utf-8")
    done = subprocess.run([sys.executable, str(driver), str(page)],
                          capture_output=True, text=True, timeout=600, cwd=REPO)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "EXCEPTIONS: []" in done.stdout, done.stdout
    assert "BUTTONS: 3" in done.stdout, done.stdout
    assert "REACHED-END: True" in done.stdout, done.stdout


def test_pymupdf_ships_in_requirements_so_hosted_previews_actually_render():
    """Streamlit Community Cloud installs only `requirements.txt`. With
    PyMuPDF in `requirements-optional.txt`, every hosted deployment fell back
    to the download button for a feature the app offers to everyone — and that
    fallback was the one with the duplicate-id bug in it."""
    core = open(os.path.join(REPO, "requirements.txt"), encoding="utf-8").read()
    optional = open(os.path.join(REPO, "requirements-optional.txt"),
                    encoding="utf-8").read()
    assert "PyMuPDF" in core, (
        "PyMuPDF must be in requirements.txt: the on-screen report previews "
        "are offered to every user and Cloud installs no other file")
    assert not any(ln.strip().startswith("PyMuPDF")
                   for ln in optional.splitlines()), \
        "PyMuPDF must not also be declared as optional"


@pytest.mark.parametrize("pymupdf", [True, False],
                         ids=["with-pymupdf", "without-pymupdf"])
def test_the_whole_results_tab_renders_in_the_real_app(tmp_path, pymupdf):
    """The failure was in the *page*, not in the data: a duplicate widget id
    raised while Streamlit was drawing it, so the confidential report below the
    first preview never appeared. Only driving the real tab catches that, and
    only with both preview boxes ticked at once.

    A separate process, because Streamlit's secrets are a process-wide
    singleton that the rest of this suite shares. The driver is
    tests/_results_tab_app.py.
    """
    args = [sys.executable, os.path.join(REPO, "tests", "_results_tab_app.py"),
            str(tmp_path / ("fitz" if pymupdf else "nofitz"))]
    if not pymupdf:
        args.append("--no-fitz")
    done = subprocess.run(args, capture_output=True, text=True, timeout=900,
                          cwd=REPO)
    assert "FAILURES: none" in done.stdout, done.stdout[-4000:]
    assert done.returncode == 0, done.stdout[-4000:]
    assert ("PASS - the confidential feedback panel rendered" in done.stdout), \
        done.stdout[-4000:]
    assert "PASS - every column on screen has an explanation" in done.stdout
    assert "PASS - Self-eval? separates who rated themselves" in done.stdout


def test_selfcheck_requires_everything_requirements_txt_declares():
    """`selfcheck.py` is what a new user runs to be told whether their install
    is sound, so its idea of "required" has to be requirements.txt's. The two
    drifted: msal, dropbox and PyMuPDF were still listed as optional long after
    they became core, and an install missing one was told it was fine."""
    import re
    core = open(os.path.join(REPO, "requirements.txt"), encoding="utf-8").read()
    declared = {re.split(r"[<>=!~\s]", ln.strip())[0].lower()
                for ln in core.splitlines()
                if ln.strip() and not ln.strip().startswith("#")}
    source = open(os.path.join(REPO, "selfcheck.py"), encoding="utf-8").read()
    block = source.split("REQUIRED = [", 1)[1].split("]", 1)[0]
    # The import name is not always the distribution name.
    alias = {"fitz": "pymupdf"}
    checked = {alias.get(m, m) for m in re.findall(r'\("([\w.]+)"', block)}
    missing = declared - checked
    assert not missing, (
        f"requirements.txt declares {sorted(missing)} but selfcheck.py does "
        f"not check for them, so a broken install is reported as sound")


def test_render_pdf_requires_a_key_per_call_site():
    """Every call in app.py passes a distinct key — the thing whose absence
    took the results tab down."""
    source = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
    calls = [seg for seg in source.split("_render_pdf(")[1:]]
    assert len(calls) >= 3
    keys = []
    for seg in calls:
        head = seg[:200]
        assert "key=" in head, f"a render_pdf call with no key: {head[:80]!r}"
        keys.append(head.split('key="')[1].split('"')[0])
    assert len(set(keys)) == len(keys), f"duplicate preview keys: {keys}"

"""The two crash matrices, run from the suite.

`tests/_every_state_app.py` puts the instructor console into each state an
instructor can reach and presses every button, ticks every checkbox and
chooses every option of every dropdown, failing on any exception.
`tests/_student_form_app.py` opens the public student form with every kind of
link a student can arrive with — fresh, pre-2.16.0, tampered, for a student
since moved or removed, for a closed or not-yet-open evaluation — and fills in
and submits it where it should be submittable.

The suite runs the console matrix in its quick form (the richest state, and
the same state with the Teams editor opened). The full six-state matrix takes several minutes and is a release gate:

    python tests/_every_state_app.py /tmp/ppstate

Both run in their own process, because Streamlit's secrets are a process-wide
singleton that the rest of the suite shares.
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(script, tmp_path, *args, timeout=900):
    done = subprocess.run(
        [sys.executable, os.path.join(REPO, "tests", script),
         str(tmp_path / "w"), *args],
        capture_output=True, text=True, timeout=timeout, cwd=REPO)
    return done


def test_every_control_in_the_richest_state_works(tmp_path):
    done = _run("_every_state_app.py", tmp_path, "--quick")
    assert "FAILURES: none" in done.stdout, done.stdout[-6000:]
    assert done.returncode == 0, done.stdout[-6000:]
    assert "STATE [C responses loaded]" in done.stdout
    assert "STATE [G Teams editor open]" in done.stdout


def test_every_way_a_student_can_arrive_works(tmp_path):
    done = _run("_student_form_app.py", tmp_path)
    assert "FAILURES: none" in done.stdout, done.stdout[-6000:]
    assert done.returncode == 0, done.stdout[-6000:]
    for line in ("PASS - the completed form can be submitted",
                 "PASS - its answers are keyed by teammate id",
                 "PASS - an old link still opens its own student's form",
                 "PASS - the same link opens their new team's form",
                 "PASS - it does not open anybody's form",
                 "PASS - submit stays off until the form is complete"):
        assert line in done.stdout, done.stdout[-6000:]


def test_no_uploaded_file_can_crash_the_app(tmp_path):
    """Thirty malformed and awkward files through every path an upload takes.

    The release audit found five that raised raw pandas errors — and since the
    upload was unguarded and every tab renders on every run, each replaced the
    whole app with a traceback — and six that read *as nothing*: Excel's
    Unicode Text (UTF-16), a semicolon CSV, a workbook with the roster on its
    second sheet. The driver is tests/_fuzz_inputs.py.
    """
    done = _run("_fuzz_inputs.py", tmp_path)
    assert "CRASHES: none" in done.stdout, done.stdout[-6000:]
    assert done.returncode == 0, done.stdout[-6000:]
    out = done.stdout
    # The silent failures read correctly now.
    for label in ("utf-16 (Excel 'Unicode text')", "semicolon separated",
                  "xlsx with the roster on sheet 2"):
        row = next(ln for ln in out.splitlines() if label in ln)
        assert "1 team(s), 3 student(s)" in row, row
    # The unusable files say why, in words.
    for label in ("empty file", "a PDF renamed .csv", "garbage renamed .xlsx",
                  "no name columns", "a contact list by mistake"):
        row = next(ln for ln in out.splitlines() if label in ln)
        assert row.lstrip().startswith("message"), row

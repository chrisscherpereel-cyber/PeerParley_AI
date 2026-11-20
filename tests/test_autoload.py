"""Responses are loaded when an evaluation is opened (2.21.1).

Reported: *"I should not have to reload responses in step 2 if I have done it
already when I reenter the application - but this is being required (for
example I already sent out the student feedback but when I reenter the
evaluation I have to resend the data - was this done for a reason?)"*

The submissions were always in storage; the graded copy lived only in the
browser session, so every sign-in and every switch between evaluations started
with nothing to grade. Now an evaluation with submissions is loaded as soon as
it is opened, and the page says when submissions have arrived since the last
load — which matters after feedback has gone out.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_page(tmp_path):
    done = subprocess.run(
        [sys.executable, os.path.join(REPO, "tests", "_autoload_app.py"),
         str(tmp_path / "al")], capture_output=True, text=True, timeout=900,
        cwd=REPO)
    assert "FAILURES: none" in done.stdout, done.stdout[-5000:]
    assert done.returncode == 0, done.stdout[-5000:]
    for line in ("PASS - responses are loaded without pressing anything",
                 "PASS - and says it arrived after the last load",
                 "PASS - someone else's evaluation is not loaded",
                 "PASS - data brought in another way is never replaced"):
        assert line in done.stdout, done.stdout[-5000:]


def test_the_last_load_is_recorded_without_touching_the_rules(tmp_path, monkeypatch):
    import dataclasses
    sys.path.insert(0, REPO)
    from cryptography.fernet import Fernet
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_BACKEND", "local")
    monkeypatch.setenv("VAULT_FOLDER", "al-unit")
    from peerparley import survey, vault as vmod
    from peerparley.grading import GradeSettings
    vmod.clear_cache()
    v = vmod.Vault()
    rules = dataclasses.asdict(GradeSettings(sensitivity_B=0.7))
    survey.save_grading(v, "x-eval1", rules, {"ann": {"adjust": -5}})
    assert survey.note_loaded(v, "x-eval1", 3) is None
    prev = survey.note_loaded(v, "x-eval1", 5)
    assert prev["count"] == 3
    got = survey.load_grading(v, "x-eval1")
    assert got["settings"] == rules and got["overrides"] == {"ann": {"adjust": -5}}
    assert got["loaded"]["count"] == 5
    # Saving the rules again keeps the record of the last load.
    survey.save_grading(v, "x-eval1", rules, {})
    assert survey.load_grading(v, "x-eval1")["loaded"]["count"] == 5

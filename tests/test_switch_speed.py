"""Opening a survey without one storage round trip per student.

Reported: *"why does it take so long to go from one survey to another?"*

Opening a survey fetched and decrypted every student's submission, one after
another, only to learn who had answered — and re-read every survey's roster
one after another for the sidebar list. On cloud storage each read is an HTTPS
round trip. Measured with 6 surveys of 40 students and 0.3 s per read: 14.9 s
per switch before, 1.7 s after.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pandas as pd
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from peerparley import survey  # noqa: E402
from peerparley import vault as vmod  # noqa: E402
from peerparley.vault import Vault  # noqa: E402


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_BACKEND", "local")
    monkeypatch.setenv("VAULT_FOLDER", "switch")
    vmod.clear_cache()
    yield Vault()
    vmod.clear_cache()


def _seed(vault, course="MGT 301", ev="1", n_teams=5, answer_all=True):
    rows = [{"First Name": f"S{t}x{j}", "Last Name": "Lee",
             "Email": f"s{t}{j}@x.edu", "Team": str(t)}
            for t in range(1, n_teams + 1) for j in range(4)]
    slug, saved = survey.save_setup(course, ev, pd.DataFrame(rows),
                                    dict(survey.DEFAULT_SURVEY), vault=vault)
    for team, members in saved.items():
        for pos, me in enumerate(members):
            if not answer_all and pos == 0:
                continue
            survey.save_response_by_id(vault, slug, me["id"], {
                "slug": slug, "team": team, "evaluator": me["name"],
                "members": {}})
    return slug


class _Counting:
    """Wraps the backend: counts reads, and makes each one take a while."""

    def __init__(self, inner, delay=0.0):
        self.inner, self.delay = inner, delay
        self.gets: list = []
        self.lock = threading.Lock()
        self.active = self.peak = 0

    def get(self, name):
        with self.lock:
            self.gets.append(name)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return self.inner.get(name)
        finally:
            with self.lock:
                self.active -= 1

    def __getattr__(self, attr):
        return getattr(self.inner, attr)


def test_who_has_answered_comes_from_the_names_not_the_contents(vault):
    slug = _seed(vault, answer_all=False)
    vmod.clear_cache()
    vault._backend = spy = _Counting(vault._backend)
    rows = survey.response_status(vault, slug)
    assert sum(r["responded"] for r in rows) == 15
    assert sum(not r["responded"] for r in rows) == 5
    assert not [n for n in spy.gets if n.startswith("resp__")], \
        "submissions were fetched just to see who had answered"


def test_a_survey_from_before_ids_still_reads_its_submissions(vault):
    """Positional names carry no id, so those are still opened."""
    slug = _seed(vault, n_teams=1, answer_all=False)
    for n in [n for n in vault.list() if n.startswith(f"resp__{slug}__m")]:
        vault.delete(n)
    snap = survey.roster_now(vault, slug)
    survey._save_json(vault, survey.key_response_legacy(slug, "1", 2),
                      {"slug": slug, "team": "1", "pos": 2, "members": {}})
    rows = survey.response_status(vault, slug)
    got = {r["name"] for r in rows if r["responded"]}
    assert got == {snap["teams"]["1"][2]["name"]}


def test_prefetch_reads_side_by_side_and_fills_the_cache(vault):
    slug = _seed(vault)
    names = [n for n in vault.list() if n.startswith(f"resp__{slug}__")]
    assert len(names) == 20
    vmod.clear_cache()
    vault._backend = spy = _Counting(vault._backend, delay=0.05)
    t0 = time.perf_counter()
    vault.prefetch(names)
    took = time.perf_counter() - t0
    assert spy.peak > 1, "nothing was fetched in parallel"
    assert spy.peak <= vmod.PREFETCH_WORKERS
    assert took < 20 * 0.05 * 0.6, f"took {took:.2f}s — not much faster than serial"
    before = len(spy.gets)
    assert len(survey.all_responses(vault, slug)) == 20
    assert len(spy.gets) == before, "the reads after a prefetch went to storage"


def test_prefetch_signs_in_once_before_going_parallel(vault):
    """The first uncached object goes alone, so a provider's token refresh
    is not raced by six threads."""
    slug = _seed(vault)
    names = [n for n in vault.list() if n.startswith(f"resp__{slug}__")]
    vmod.clear_cache()
    order = []
    inner = vault._backend

    class _First:
        def get(self, name):
            order.append(("start", name, threading.current_thread().name))
            time.sleep(0.02)
            return inner.get(name)

        def __getattr__(self, attr):
            return getattr(inner, attr)

    vault._backend = _First()
    vault.prefetch(names)
    assert order[0][1] == names[0]
    assert not order[0][2].startswith("pp-vault"), \
        "the first read should run before any worker thread starts"


def test_a_failed_read_is_still_reported_by_the_caller(vault):
    slug = _seed(vault, n_teams=1)
    names = [n for n in vault.list() if n.startswith(f"resp__{slug}__")]
    vmod.clear_cache()
    inner = vault._backend

    class _Broken:
        def get(self, name):
            if name == names[1]:
                raise OSError("network down")
            return inner.get(name)

        def __getattr__(self, attr):
            return getattr(inner, attr)

    vault._backend = _Broken()
    vault.prefetch(names)                      # does not raise
    with pytest.raises(Exception):
        vault.get_bytes(names[1])
    assert len(survey.all_responses(vault, slug)) == 3


def test_the_sidebar_list_reads_every_roster_in_parallel(vault):
    for c in ("A 1", "B 2", "C 3", "D 4", "E 5"):
        _seed(vault, course=c, n_teams=1)
    vmod.clear_cache()
    vault._backend = spy = _Counting(vault._backend, delay=0.05)
    got = survey.list_surveys(vault)
    assert len(got) == 5
    assert spy.peak > 1


# --------------------------------------------------------------------------- #
# 2.20.2 — the rest of the slow paths
# --------------------------------------------------------------------------- #
# Asked: *"Are there other similar issues that are slow in the application
# that can be fixed."* Measured action by action at 0.3 s per storage read.

def _age(seconds):
    for k, (t, d) in list(vmod._reads.items()):
        vmod._reads[k] = (t - seconds, d)
    for k, (t, d) in list(vmod._listings.items()):
        vmod._listings[k] = (t - seconds, d)


def test_the_survey_list_does_not_reread_every_roster_each_minute(vault):
    for c in ("A 1", "B 2", "C 3"):
        _seed(vault, course=c, n_teams=1)
    survey.list_surveys(vault)
    _age(61)                                   # the normal cache has expired
    vault._backend = spy = _Counting(vault._backend)
    assert len(survey.list_surveys(vault)) == 3
    assert not [n for n in spy.gets if n.startswith("roster__")]
    _age(vmod.SUMMARY_MAX_AGE)                 # ...and now the summaries too
    survey.list_surveys(vault)
    assert len([n for n in spy.gets if n.startswith("roster__")]) == 3


def test_a_change_made_here_shows_in_the_list_at_once(vault):
    slug = _seed(vault, course="A 1", n_teams=1)
    survey.list_surveys(vault)
    _age(61)
    snap = survey.roster_now(vault, slug)
    mid = snap["teams"]["1"][0]["id"]
    survey.remove_student(vault, slug, mid)
    got = {s["slug"]: s["students"] for s in survey.list_surveys(vault)}
    assert got[slug] == 3


def test_check_for_new_responses_rereads_only_that_survey(vault):
    a = _seed(vault, course="A 1", n_teams=1)
    b = _seed(vault, course="B 2", n_teams=1)
    survey.all_responses(vault, a)
    survey.all_responses(vault, b)
    survey.load_survey(vault, b)
    vault.refresh(prefixes=(f"resp__{a}__", survey.key_roster(a),
                            survey.key_survey(a)))
    vault._backend = spy = _Counting(vault._backend)
    survey.all_responses(vault, b)
    survey.load_survey(vault, b)
    assert spy.gets == [], "the other survey was re-read"
    survey.all_responses(vault, a)
    assert len(spy.gets) == 4


def test_the_app_scopes_its_refresh_button():
    src = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
    block = src.split('"↻ Check for new responses", key="resp_refresh"', 1)[1][:600]
    assert "refresh(prefixes=" in block


def test_pdf_pages_are_drawn_once_per_report():
    pytest.importorskip("fitz")
    from peerparley import ui_helpers
    assert hasattr(ui_helpers._pdf_pages_png, "clear"), "not cached"
    import inspect
    assert "_pdf_pages_png(" in inspect.getsource(ui_helpers.render_pdf)
    import fitz
    doc = fitz.open()
    doc.new_page()
    pdf = doc.tobytes()
    ui_helpers._pdf_pages_png.clear()
    first = ui_helpers._pdf_pages_png(pdf)
    calls = []
    real = fitz.Page.get_pixmap
    fitz.Page.get_pixmap = lambda self, *a, **k: calls.append(1) or real(self, *a, **k)
    try:
        again = ui_helpers._pdf_pages_png(pdf)
    finally:
        fitz.Page.get_pixmap = real
    assert again == first and calls == []


def test_the_student_form_fetches_what_it_needs_together():
    import inspect
    src = inspect.getsource(survey.render_student_app)
    head = src.split("snap = roster_now(vault, slug)", 1)[0]
    assert "vault.prefetch(" in head and "key_response(slug" in head

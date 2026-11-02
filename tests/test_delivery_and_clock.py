"""The four changes in 2.11: default method, detected address, review
everywhere, and a clock the instructor sets.

Each test names the behaviour being fixed in place, because each of these is a
default or a piece of wording that someone will be tempted to "tidy" later
without knowing what it was for.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import datetime as dt
import io
import os
import re
import sys
import zipfile

from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from peerparley import emailpack, survey, webcontext  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. The default way to send
# --------------------------------------------------------------------------- #

def _method_lists() -> dict:
    """Read the two lists out of app.py without importing it.

    Importing app.py runs the whole Streamlit script, so the lists are read as
    source. Crude, but it is the actual default a new user meets, and the
    alternative is not testing it at all.
    """
    text = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
    out = {}
    for name in ("_LINK_METHODS", "_RESULT_METHODS"):
        block = text.split(f"{name} = [", 1)[1].split("]", 1)[0]
        out[name] = re.findall(r'"([^"]+)"', block)
    return out


def test_auto_send_scripts_are_the_default_for_both_kinds_of_email():
    """Streamlit selects the first option, so order is the default.

    Auto-send is the method that works for everybody: no app registration, no
    SMTP password, no decision about this app's own mail settings — it uses the
    Outlook or Mail already signed in on the instructor's computer, and the
    messages come from their own address.
    """
    lists = _method_lists()
    for name, methods in lists.items():
        assert methods, name
        assert methods[0].startswith("Download auto-send"), \
            f"{name} no longer defaults to the auto-send scripts: {methods[0]}"
    # And nothing was lost in the reordering.
    assert set(lists["_LINK_METHODS"]) == {
        "Download auto-send scripts (Mac / Windows)", "Microsoft 365 — send now",
        "SMTP — send now", "Open in my mail app (mailto)", "Download .eml files",
        "Download links CSV"}
    assert set(lists["_RESULT_METHODS"]) == {
        "Download auto-send scripts (Mac / Windows)", "Microsoft 365 — send now",
        "SMTP — send now", "Download .eml files", "Download recipients CSV"}


def test_every_sending_method_offers_a_review_step():
    """"Prepare them for me to look over first" has to be available wherever
    mail can leave, not only on the two that go through the app's own settings.
    """
    text = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
    # The two API transports, through _api_and_drafts, and both auto-send
    # branches, through _review_first — four offers in all.
    assert text.count("_review_first(") >= 3
    assert "REVIEW_LABEL" in text
    # The methods that cannot send anything say so where the checkbox would be.
    assert text.count("Nothing is sent by this method") == 3


# --------------------------------------------------------------------------- #
# 2. The auto-send pack's review mode is real
# --------------------------------------------------------------------------- #

PARTS = [{"to": "ann@example.edu", "name": "Ann Apple", "subject": "Your feedback",
          "body": "<p>Hello Ann</p>", "attachments": [("Ann_Apple.pdf", b"%PDF-1.4 x")]},
         {"to": "bob@example.edu", "name": "Bob Brown", "subject": "Your feedback",
          "body": "<p>Hello Bob</p>", "attachments": [("Bob_Brown.pdf", b"%PDF-1.4 y")]}]


def _pack(drafts: bool) -> dict:
    raw = emailpack.send_all_pack(PARTS, "Results", drafts=drafts)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return {n: z.read(n).decode("utf-8", "replace") if n.endswith(
            (".cmd", ".ps1", ".applescript", ".txt")) else z.read(n)
            for n in z.namelist()}


def test_the_send_pack_sends():
    files = _pack(drafts=False)
    assert "Send emails (Windows).cmd" in files
    assert "$mail.Send()" in files["send_all_windows.ps1"]
    assert "$mail.Save()" not in files["send_all_windows.ps1"]
    assert files["send_all_mac.applescript"].count("send newMsg") == 2
    assert "auto-send pack" in files["READ ME FIRST.txt"]


def test_the_review_pack_writes_drafts_and_sends_nothing():
    """The checkbox has to change what the scripts do, not just the wording.

    Outlook's COM object drafts with .Save() and both Mac mail apps take `save`
    in place of `send`, so this is a real review step rather than a label on an
    irreversible one.
    """
    files = _pack(drafts=True)
    ps = files["send_all_windows.ps1"]
    assert "$mail.Save()" in ps
    assert "$mail.Send()" not in ps, "the review pack would still send"
    applescript = files["send_all_mac.applescript"]
    assert applescript.count("save newMsg") == 2
    assert "send newMsg" not in applescript, "the review pack would still send"

    # The folder has to say what it does: the instructor opens it days later,
    # long after the checkbox is forgotten.
    assert "Prepare drafts (Windows).cmd" in files
    assert "Send emails (Windows).cmd" not in files
    readme = files["READ ME FIRST.txt"]
    assert "nothing is sent" in readme.lower()
    assert "Prepare drafts (Windows).cmd" in readme
    assert "Drafts folder" in readme
    assert "draft" in files["Prepare drafts (Windows).cmd"].lower()


def test_both_packs_still_carry_every_students_attachment():
    for drafts in (False, True):
        files = _pack(drafts)
        assert files["Ann_Apple.pdf"] == b"%PDF-1.4 x"
        assert files["Bob_Brown.pdf"] == b"%PDF-1.4 y"
        for name in ("send_all_windows.ps1", "send_all_mac.applescript"):
            assert "ann@example.edu" in files[name]
            assert "bob@example.edu" in files[name]


# --------------------------------------------------------------------------- #
# 3. The app's own address
# --------------------------------------------------------------------------- #

class _Ctx:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_the_address_comes_from_the_page_the_instructor_is_on():
    assert webcontext.app_url(_Ctx(url="https://peerparley.example.edu/")) == \
        "https://peerparley.example.edu"
    assert webcontext.app_url(_Ctx(url="http://localhost:8501")) == \
        "http://localhost:8501"


def test_the_students_token_is_never_carried_into_the_link_base():
    """A student's own address bar holds their token; the base must not."""
    url = webcontext.app_url(_Ctx(url="https://pp.example/?t=abc.def#x"))
    assert url == "https://pp.example"
    assert "t=" not in url


def test_the_address_falls_back_to_the_request_headers():
    """Older Streamlit exposes headers but not the URL."""
    assert webcontext.app_url(_Ctx(headers={"Host": "pp.example",
                                            "X-Forwarded-Proto": "https"})) == \
        "https://pp.example"
    assert webcontext.app_url(_Ctx(headers={"host": "localhost:8501"})) == \
        "http://localhost:8501"


def test_no_browsing_context_means_no_address_rather_than_a_wrong_one():
    class _Raises:
        @property
        def url(self):
            raise RuntimeError("no browsing context")

    assert webcontext.app_url(None) == ""
    assert webcontext.app_url(_Ctx()) == ""
    assert webcontext.app_url(_Raises()) == ""
    assert webcontext.app_url(_Ctx(url="not-a-url")) == ""


# --------------------------------------------------------------------------- #
# 4. A clock the instructor sets
# --------------------------------------------------------------------------- #

def test_the_browser_offset_is_read_with_the_right_sign():
    """getTimezoneOffset() counts the other way; getting this backwards would
    move every schedule by twice the offset."""
    utc_server = dt.datetime(2026, 11, 2, 18, 0, tzinfo=dt.timezone.utc)
    # Arizona is UTC-7, which the browser reports as +420.
    assert webcontext.clock_offset_minutes(_Ctx(timezone_offset=420),
                                           utc_server) == -420
    # Berlin in winter is UTC+1, reported as -60.
    assert webcontext.clock_offset_minutes(_Ctx(timezone_offset=-60),
                                           utc_server) == 60
    # A server already in Arizona: the same browser is then in step with it.
    az_server = utc_server.astimezone(dt.timezone(dt.timedelta(hours=-7)))
    assert webcontext.clock_offset_minutes(_Ctx(timezone_offset=420),
                                           az_server) == 0
    assert webcontext.clock_offset_minutes(_Ctx(), utc_server) is None
    assert webcontext.clock_offset_minutes(None, utc_server) is None


def test_stating_the_time_produces_the_offset_and_ignores_typing_lag():
    server = dt.datetime(2026, 11, 2, 18, 0, 37)
    stated = dt.datetime(2026, 11, 2, 11, 0)          # "it is 11am here"
    assert survey.offset_from_wall_clock(stated, server) == -420
    # Seconds of typing lag must not become a lopsided offset.
    assert survey.offset_from_wall_clock(
        dt.datetime(2026, 11, 2, 18, 0), server) == 0


def test_the_schedule_is_read_on_the_clock_the_instructor_set():
    """The Arizona case: "closes Friday 5pm" meant 5pm UTC, so the survey shut
    at 10am their time with nothing on screen having said so."""
    server_now = dt.datetime.now()                     # pretend: UTC
    local_now = server_now - dt.timedelta(hours=7)      # Arizona
    cfg = dict(survey.DEFAULT_SURVEY)
    cfg[survey.CLOCK_KEY] = -420
    # It is 11am local; the survey closes at 5pm local today.
    cfg["closes_at"] = dt.datetime.combine(
        local_now.date(), dt.time(local_now.hour + 1 if local_now.hour < 23 else 23)
    ).isoformat()
    assert survey.window_state(cfg) == "open"

    # On the server's clock that same time is already past, which is exactly
    # the bug: without the offset the survey reports itself closed.
    without = dict(cfg)
    without[survey.CLOCK_KEY] = 0
    assert survey.window_state(without) == "closed"
    assert "closes" in survey.window_message(cfg)


def test_an_opening_time_in_the_future_locally_is_still_in_the_future():
    cfg = dict(survey.DEFAULT_SURVEY)
    cfg[survey.CLOCK_KEY] = 180
    cfg["opens_at"] = (survey.local_now(cfg) + dt.timedelta(hours=2)).isoformat()
    assert survey.window_state(cfg) == "not_yet"
    assert "opens on" in survey.window_message(cfg)


def test_the_clock_is_part_of_the_saved_survey():
    """Otherwise it would be forgotten on reload and the schedule would shift."""
    assert survey.CLOCK_KEY in survey.DEFAULT_SURVEY
    # None means "nobody has decided", which is what lets the panel adopt the
    # browser's offset on first open; it still reads as the server's clock until
    # then, so behaviour is unchanged for anyone who never opens the panel.
    assert survey.DEFAULT_SURVEY[survey.CLOCK_KEY] is None
    assert survey.clock_offset(survey.DEFAULT_SURVEY) == 0
    assert survey.window_state(dict(survey.DEFAULT_SURVEY)) == "open"
    # A survey saved before this feature has no such key, and must keep
    # behaving exactly as it did.
    legacy = {k: v for k, v in survey.DEFAULT_SURVEY.items() if k != survey.CLOCK_KEY}
    assert survey.clock_offset(legacy) == 0
    assert survey.window_state(legacy) == "open"
    assert survey.clock_offset({survey.CLOCK_KEY: "nonsense"}) == 0


def test_the_clock_is_described_in_words_an_instructor_can_check():
    cfg = dict(survey.DEFAULT_SURVEY)
    assert "server" in survey.describe_clock(cfg)
    cfg[survey.CLOCK_KEY] = -420
    described = survey.describe_clock(cfg)
    assert "7 hour(s) behind" in described
    assert f"{survey.local_now(cfg):%I:%M %p}" in described


# --------------------------------------------------------------------------- #
# 5. Shipping no expired Streamlit arguments
# --------------------------------------------------------------------------- #

def test_full_width_is_spelled_the_way_this_streamlit_understands():
    """`use_container_width=True` is past its removal date; `width="stretch"`
    does not exist in the older versions requirements.txt still allows. One
    place decides, by asking the installed Streamlit."""
    from peerparley.ui_helpers import FULL_WIDTH, _detect_width_kwargs
    assert isinstance(FULL_WIDTH, dict) and len(FULL_WIDTH) == 1
    key = next(iter(FULL_WIDTH))
    assert key in ("width", "use_container_width")

    import inspect
    import streamlit as st
    expected = ("width" if "width" in inspect.signature(st.button).parameters
                else "use_container_width")
    assert key == expected
    assert _detect_width_kwargs() == FULL_WIDTH


def test_no_module_passes_the_retired_argument_directly():
    import pathlib
    for path in pathlib.Path(REPO).rglob("*.py"):
        if ".venv" in str(path) or "/tests/" in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "use_container_width=" in line and not line.lstrip().startswith("#"):
                assert "_detect_width_kwargs" in text, \
                    f"{path.name} passes use_container_width directly: {line.strip()}"


# --------------------------------------------------------------------------- #
# 6. What a rerun costs
# --------------------------------------------------------------------------- #

def test_one_rerun_does_not_hammer_the_storage_backend(tmp_path):
    """Reported: "still very slow when I am doing small tasks like changing the
    schedule."

    Streamlit re-executes the whole script on every click, and the script was
    asking storage for 52 objects each time — the survey, the roster, all forty
    responses, the account list. Invisible on a laptop; several seconds per
    click on Microsoft 365 or Dropbox, where every read is an HTTPS round trip.

    This measures round trips at the backend, below the read cache, with a full
    40-student section loaded. The budget is deliberately tight: a change that
    reintroduces a per-student read will break it.
    """
    import subprocess
    done = subprocess.run(
        [sys.executable, os.path.join(REPO, "tests", "_rerun_cost.py"),
         str(tmp_path / "cost"), "--budget", "4"],
        capture_output=True, text=True, timeout=900)
    assert done.returncode == 0, done.stdout[-3000:]
    assert "PASS - one rerun stays within" in done.stdout, done.stdout[-3000:]


def test_the_vault_remembers_reads_briefly_and_never_serves_a_stale_write(tmp_path,
                                                                         monkeypatch):
    """The cache that makes the above possible, and its two hard rules."""
    from peerparley import vault as vault_mod
    from peerparley.vault import Vault

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_BACKEND", "local")
    monkeypatch.setenv("VAULT_FOLDER", "cache-test")
    vault_mod.clear_cache()

    v = Vault()
    reads = {"n": 0}
    original = type(v.backend).get

    def counted(self, name):
        reads["n"] += 1
        return original(self, name)

    monkeypatch.setattr(type(v.backend), "get", counted)

    v.put_bytes("thing.ppj", b"first")
    # Rule one: a write is visible at once, and costs no read.
    assert v.get_bytes("thing.ppj") == b"first"
    assert reads["n"] == 0, "a value just written should not be fetched back"

    # Repeated reads inside the window do not reach the store.
    v.put_bytes("other.ppj", b"x")
    before = reads["n"]
    for _ in range(5):
        v.get_bytes("other.ppj")
    assert reads["n"] == before

    # Rule two: a later write of the same object replaces what is remembered.
    v.put_bytes("thing.ppj", b"second")
    assert v.get_bytes("thing.ppj") == b"second"

    # The listing stays correct across writes and deletes without re-listing.
    assert "thing.ppj" in v.list() and "other.ppj" in v.list()
    v.delete("other.ppj")
    assert "other.ppj" not in v.list()

    # And refresh() means "go and look again".
    v.refresh()
    reads["n"] = 0
    assert v.get_bytes("thing.ppj") == b"second"
    assert reads["n"] == 1


def test_two_stores_with_the_same_folder_name_do_not_share_a_cache(tmp_path,
                                                                   monkeypatch):
    """Found by the test suite itself: the cache key has to include the local
    store's absolute path, or one section's data is served to another."""
    from peerparley import vault as vault_mod
    from peerparley.vault import Vault

    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_BACKEND", "local")
    monkeypatch.setenv("VAULT_FOLDER", "same-name")
    vault_mod.clear_cache()

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.chdir(tmp_path / "a")
    Vault().put_bytes("shared.ppj", b"section A")
    monkeypatch.chdir(tmp_path / "b")
    second = Vault()
    second.put_bytes("shared.ppj", b"section B")
    assert second.get_bytes("shared.ppj") == b"section B"
    monkeypatch.chdir(tmp_path / "a")
    assert Vault().get_bytes("shared.ppj") == b"section A"


def test_counting_responses_does_not_read_them_all(tmp_path, monkeypatch):
    """The Start here checklist wanted this number on every rerun and used to
    get it by fetching and decrypting all forty submissions."""
    from peerparley import survey as svy
    from peerparley import vault as vault_mod
    from peerparley.vault import Vault

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_BACKEND", "local")
    monkeypatch.setenv("VAULT_FOLDER", "count-test")
    vault_mod.clear_cache()

    v = Vault()
    for pos in range(12):
        svy.save_response(v, "c-eval1", "1", pos, {"pos": pos, "team": "1"})
    v.refresh()

    reads = {"n": 0}
    original = type(v.backend).get
    monkeypatch.setattr(type(v.backend), "get",
                        lambda self, name: (reads.__setitem__("n", reads["n"] + 1),
                                            original(self, name))[1])
    assert svy.response_count(v, "c-eval1") == 12
    assert reads["n"] == 0, "counting should not fetch a single response"
    assert len(svy.all_responses(v, "c-eval1")) == 12   # this one does read them

"""The first five minutes of a new installation.

Before release, the one thing never tested was the state every new user starts
in: nothing configured. Streamlit's own test harness showed what that looked
like — a red box quoting a shell command, and **zero** buttons or fields on the
page. There was no way forward from inside the app.

These tests fix that state in place. They cover the gate that decides when the
setup screen appears (and, more importantly, when it must *not*), the values it
generates, its refusal to overwrite an existing secrets file, and the self-check
script a new user runs when something looks wrong.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from peerparley import setup_wizard as sw  # noqa: E402


@pytest.fixture()
def nothing_configured(tmp_path, monkeypatch):
    """A clean machine: no secrets, no accounts, no key."""
    monkeypatch.chdir(tmp_path)
    for var in ("FERNET_KEY", "APP_PASSWORD_SHA256", "TOKEN_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VAULT_BACKEND", "local")
    monkeypatch.setenv("VAULT_FOLDER", "firstrun")
    return tmp_path


# --------------------------------------------------------------------------- #
# When does the setup screen appear?
# --------------------------------------------------------------------------- #

def test_setup_is_needed_when_nothing_is_configured(nothing_configured):
    assert sw.needed() is True
    assert sw.has_credential() is False
    assert sw.key_is_usable() is False


def test_setup_is_needed_when_the_key_is_missing_but_sign_in_works(
        nothing_configured, monkeypatch):
    """The state that let someone type in forty students and *then* discover
    that nothing could be saved."""
    monkeypatch.setenv("APP_PASSWORD_SHA256", hashlib.sha256(b"pw").hexdigest())
    assert sw.has_credential() is True
    assert sw.key_is_usable() is False
    assert sw.needed() is True


def test_setup_is_needed_when_the_key_is_present_but_invalid(
        nothing_configured, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD_SHA256", hashlib.sha256(b"pw").hexdigest())
    monkeypatch.setenv("FERNET_KEY", "not-a-real-fernet-key")
    assert sw.key_is_usable() is False
    assert sw.needed() is True


def test_a_configured_deployment_never_sees_the_setup_screen(
        nothing_configured, monkeypatch):
    """The safety property. Anything else would be a back door into a working
    deployment, so it is asserted rather than assumed."""
    monkeypatch.setenv("APP_PASSWORD_SHA256", hashlib.sha256(b"pw").hexdigest())
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    assert sw.key_is_usable() is True
    assert sw.has_credential() is True
    assert sw.needed() is False


def test_a_deployment_with_only_stored_accounts_counts_as_configured(
        nothing_configured, monkeypatch):
    """No app password, but instructors exist — that is a working deployment and
    the setup screen must stay out of its way."""
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    from peerparley import accounts
    accounts.save_accounts({"chris": {"name": "Chris", "role": "instructor",
                                      "salt": "00", "hash": "00",
                                      "active": True}})
    assert sw.has_credential() is True
    assert sw.needed() is False


# --------------------------------------------------------------------------- #
# What it generates
# --------------------------------------------------------------------------- #

def test_the_generated_settings_are_valid_and_complete(nothing_configured):
    key = sw.new_fernet_key()
    Fernet(key.encode())                      # raises if it is not a real key
    block = sw.secrets_block(sw.password_hash("a-good-password"), key,
                             "https://pp.example")
    for needed in ("app_password_sha256", "fernet_key", "public_url",
                   "[vault]", 'backend = "local"'):
        assert needed in block
    # Valid TOML, and it parses to the values the app reads.
    import tomllib
    parsed = tomllib.loads(block)
    assert parsed["fernet_key"] == key
    assert parsed["app_password_sha256"] == \
        hashlib.sha256(b"a-good-password").hexdigest()
    assert parsed["vault"]["backend"] == "local"
    # The password itself is nowhere in the file.
    assert "a-good-password" not in block


def test_writing_the_secrets_file_works_and_then_refuses_to_clobber_it(
        nothing_configured):
    block = sw.secrets_block(sw.password_hash("a-good-password"),
                             sw.new_fernet_key())
    ok, path = sw.write_secrets(block)
    assert ok and os.path.exists(path)
    assert open(path, encoding="utf-8").read() == block

    # A second attempt must not overwrite it: on a real machine that file may
    # hold the key protecting existing student data.
    ok2, message = sw.write_secrets("something else")
    assert ok2 is False and "already exists" in message
    assert open(path, encoding="utf-8").read() == block

    writable, why_not = sw.can_write_secrets()
    assert writable is False and "already exists" in why_not


def test_writing_is_not_offered_on_a_hosted_server(nothing_configured,
                                                   monkeypatch):
    """On Streamlit Cloud the file would vanish at the next restart, so the
    offer would set someone up for one session and then look broken."""
    monkeypatch.setattr("peerparley.localmodels.is_hosted", lambda: True)
    writable, why_not = sw.can_write_secrets()
    assert writable is False
    assert "Secrets box" in why_not


# --------------------------------------------------------------------------- #
# The self-check script
# --------------------------------------------------------------------------- #

def _selfcheck(cwd, env_extra=None):
    env = dict(os.environ)
    env.pop("FERNET_KEY", None)
    env.pop("APP_PASSWORD_SHA256", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, os.path.join(REPO, "selfcheck.py")],
                          cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=180)


def test_selfcheck_reports_a_problem_on_an_unconfigured_install(tmp_path):
    done = _selfcheck(tmp_path)
    assert done.returncode == 1
    assert "no encryption key configured" in done.stdout
    assert "problem" in done.stdout.lower()
    # It must still tell them what to do next.
    assert "streamlit run app.py" in done.stdout


def test_selfcheck_passes_once_the_app_is_configured(tmp_path):
    done = _selfcheck(tmp_path, {
        "FERNET_KEY": Fernet.generate_key().decode(),
        "APP_PASSWORD_SHA256": hashlib.sha256(b"pw").hexdigest(),
        "VAULT_BACKEND": "local",
        "VAULT_FOLDER": "selfcheck-test",
    })
    assert done.returncode == 0, done.stdout
    assert "No problems" in done.stdout
    assert "encryption works" in done.stdout
    assert "written, read back and decrypted" in done.stdout
    # And it cleaned up after itself.
    assert not os.path.exists(
        tmp_path / "vault_cache" / "selfcheck-test" / "selfcheck.probe")


def test_selfcheck_never_claims_the_ai_writer_is_on(tmp_path):
    done = _selfcheck(tmp_path, {
        "FERNET_KEY": Fernet.generate_key().decode(),
        "VAULT_BACKEND": "local", "VAULT_FOLDER": "selfcheck-ai",
        "OPENROUTER_API_KEY": "sk-or-v1-not-a-real-key",
    })
    assert "AI writer" in done.stdout
    assert "stays switched off" in done.stdout
    # A configured key must never appear in a report someone may paste into an
    # email or a support ticket.
    assert "sk-or-v1-not-a-real-key" not in done.stdout


# --------------------------------------------------------------------------- #
# The release itself
# --------------------------------------------------------------------------- #

def test_the_python_floor_is_declared_and_enforced():
    import peerparley
    assert peerparley.MIN_PYTHON == (3, 10)
    assert sys.version_info >= peerparley.MIN_PYTHON

    # And it is documented where someone installing would look.
    readme = open(os.path.join(REPO, "README.md"), encoding="utf-8").read()
    assert "3.10" in readme
    requirements = open(os.path.join(REPO, "requirements.txt"),
                        encoding="utf-8").read()
    assert "3.10" in requirements


def test_every_core_requirement_has_both_bounds():
    """A floating upper bound means a fresh install breaks months later when a
    dependency releases a new major version, for a user who changed nothing."""
    text = open(os.path.join(REPO, "requirements.txt"), encoding="utf-8").read()
    lines = [l.split("#")[0].strip() for l in text.splitlines()]
    pinned = [l for l in lines if l]
    assert pinned, "requirements.txt has no requirements in it"
    for line in pinned:
        assert ">=" in line and "<" in line, f"{line} has no upper bound"


def test_the_repository_ships_no_secrets_and_no_student_data():
    """The repository is public. This is the check that keeps it publishable.

    Skipped when there is no git checkout — a new user running the tests from a
    downloaded zip has no repository to inspect, and a test that fails where it
    cannot apply is a false alarm on their first five minutes with the app.
    """
    import subprocess as sp
    try:
        done = sp.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                      text=True, timeout=60)
    except (OSError, sp.SubprocessError) as exc:  # no git installed
        pytest.skip(f"git is not available here ({exc})")
    listing = done.stdout.split()
    if done.returncode != 0 or not listing:
        pytest.skip("not a git checkout (a downloaded copy, most likely)")
    assert ".streamlit/secrets.toml" not in listing
    for name in listing:
        assert not name.endswith((".ppx", ".ppj")), name
        assert "vault_cache" not in name, name


# --------------------------------------------------------------------------- #
# The whole first-run path, through the real app
# --------------------------------------------------------------------------- #

def test_the_setup_screen_configures_the_app_and_sign_in_then_works(
        nothing_configured, monkeypatch):
    """Install → setup screen → written secrets → restart → sign in.

    The individual pieces are tested above; this is the join between them, and
    it is the one a new user walks. It was never exercised before release: the
    file the screen writes had no test that the app could actually read it.
    """
    from streamlit.testing.v1 import AppTest

    block = sw.secrets_block(sw.password_hash("a-release-test-password"),
                             sw.new_fernet_key(), "https://pp.example")
    ok, path = sw.write_secrets(block)
    assert ok, path

    # Streamlit reads .streamlit/secrets.toml relative to the working
    # directory, which the fixture has pointed at a temp folder.
    app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=120)
    app.run()
    assert not app.exception
    page = " ".join(str(getattr(e, "value", "")) for e in app.markdown)
    assert "needs two things before you start" not in page, \
        "the setup screen came back even though it had just written the settings"
    assert "Instructor sign-in" in page

    app.text_input[0].input("admin")
    app.text_input[1].input("a-release-test-password")
    app.button[0].click().run()
    assert not app.exception
    assert "pp_user" in app.session_state, "the chosen password did not sign in"

    wrong = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=120)
    wrong.run()
    wrong.text_input[0].input("admin")
    wrong.text_input[1].input("not-the-password")
    wrong.button[0].click().run()
    assert "pp_user" not in wrong.session_state
    assert any("Wrong username or password" in str(getattr(e, "value", ""))
               for e in wrong.error)

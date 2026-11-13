"""Instructor accounts, the team and comparison PDFs, and the Compare tab.

The 2.17.0 release audit mapped every feature the README advertises to the test
that proves it, and these had none at all.

**Accounts** turned up a security problem rather than a bug. Every new
instructor account, and every password reset, was given the same fixed
password — "PeerParley-Welcome" — and that string is in the source of a public
repository. `must_change` forces a new password only *after* signing in, so
anyone who knew an instructor's username could sign in first with the
published password, set their own, lock the instructor out and read their
students' records. Temporary passwords are now random and shown once.

**Compare** had a performance trap: the flag its button set was never cleared,
so after the first press the whole comparison — every response of every round,
read, decrypted and regraded — was rebuilt on every click anywhere in the app.
It also counted "Ann Apple" and "Apple, Ann" as two students.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pandas as pd
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from peerparley import accounts, pdfgen  # noqa: E402
from peerparley.grading import GradeSettings, compute  # noqa: E402
from peerparley.vault import Vault  # noqa: E402


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_BACKEND", "local")
    monkeypatch.setenv("VAULT_FOLDER", "accounts")
    return Vault()


class _Cfg:
    app_password_sha256 = hashlib.sha256(b"break-glass-pw").hexdigest()


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
def test_an_instructor_can_be_added_and_sign_in(vault):
    accounts.add_account("jdoe", "J Doe", "instructor", "correct horse", vault)
    who = accounts.authenticate("jdoe", "correct horse", _Cfg(), vault)
    assert who and who["user"] == "jdoe" and who["role"] == "instructor"
    assert who["must_change"] is True, "a temporary password must be replaced"


def test_a_wrong_password_signs_nobody_in(vault):
    accounts.add_account("jdoe", "J Doe", "instructor", "correct horse", vault)
    assert accounts.authenticate("jdoe", "wrong horse", _Cfg(), vault) is None
    assert accounts.authenticate("jdoe", "", _Cfg(), vault) is None


@pytest.mark.parametrize("pw", ["", "short", "1234567"])
def test_a_password_too_short_to_keep_is_refused(vault, pw):
    with pytest.raises(ValueError, match="at least"):
        accounts.add_account("jdoe", "J Doe", "instructor", pw, vault)
    assert accounts.load_accounts(vault) == {}


def test_a_reset_password_is_held_to_the_same_rule(vault):
    accounts.add_account("jdoe", "J Doe", "instructor", "correct horse", vault)
    with pytest.raises(ValueError, match="at least"):
        accounts.set_password("jdoe", "abc", vault)


def test_no_fixed_default_password_is_left_anywhere_in_the_app():
    """It was "PeerParley-Welcome", in every new account and every reset, and
    the repository is public."""
    import ast
    for path in ("app.py", "peerparley/accounts.py", "peerparley/auth.py",
                 "peerparley/setup_wizard.py"):
        tree = ast.parse(open(os.path.join(REPO, path), encoding="utf-8").read())
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef,
                                        ast.AsyncFunctionDef, ast.ClassDef))
                      and n.body and isinstance(n.body[0], ast.Expr)
                      and isinstance(n.body[0].value, ast.Constant)}
        # Mentioned in a docstring as history is fine; used as a value is not.
        used = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "PeerParley-Welcome" in n.value and id(n) not in docstrings]
        assert not used, f"{path} still uses the published password at {used}"


def test_temporary_passwords_are_random_and_long_enough():
    seen = {accounts.new_temp_password() for _ in range(200)}
    assert len(seen) == 200, "two accounts must never share a temporary password"
    assert all(len(p) >= accounts.MIN_PASSWORD for p in seen)


def test_a_reset_gives_a_working_password_that_must_be_changed(vault):
    accounts.add_account("jdoe", "J Doe", "instructor", "correct horse", vault)
    temp = accounts.new_temp_password()
    accounts.set_password("jdoe", temp, vault, must_change=True)
    who = accounts.authenticate("jdoe", temp, _Cfg(), vault)
    assert who and who["must_change"]
    assert accounts.authenticate("jdoe", "correct horse", _Cfg(), vault) is None


def test_a_deactivated_account_cannot_sign_in(vault):
    accounts.add_account("jdoe", "J Doe", "instructor", "correct horse", vault)
    accounts.set_active("jdoe", False, vault)
    assert accounts.authenticate("jdoe", "correct horse", _Cfg(), vault) is None
    accounts.set_active("jdoe", True, vault)
    assert accounts.authenticate("jdoe", "correct horse", _Cfg(), vault)


def test_a_removed_account_cannot_sign_in(vault):
    accounts.add_account("jdoe", "J Doe", "instructor", "correct horse", vault)
    accounts.remove_account("jdoe", vault)
    assert accounts.authenticate("jdoe", "correct horse", _Cfg(), vault) is None


def test_duplicate_and_reserved_usernames_are_refused(vault):
    accounts.add_account("jdoe", "J Doe", "instructor", "correct horse", vault)
    with pytest.raises(ValueError, match="already exists"):
        accounts.add_account("jdoe", "Other", "instructor", "correct horse", vault)
    with pytest.raises(ValueError, match="reserved"):
        accounts.add_account("admin", "Me", "admin", "correct horse", vault)
    with pytest.raises(ValueError, match="required"):
        accounts.add_account("   ", "Me", "admin", "correct horse", vault)


def test_the_break_glass_admin_needs_the_username_typed(vault):
    assert accounts.authenticate("admin", "break-glass-pw", _Cfg(), vault)
    assert accounts.authenticate("", "break-glass-pw", _Cfg(), vault) is None
    assert accounts.authenticate("jdoe", "break-glass-pw", _Cfg(), vault) is None


def test_passwords_are_stored_hashed_and_salted(vault):
    accounts.add_account("a1", "A", "instructor", "same password", vault)
    accounts.add_account("a2", "B", "instructor", "same password", vault)
    stored = accounts.load_accounts(vault)
    assert "same password" not in str(stored)
    assert stored["a1"]["hash"] != stored["a2"]["hash"], "salts must differ"


# --------------------------------------------------------------------------- #
# The team contribution PDF and the comparison PDF
# --------------------------------------------------------------------------- #
def _section(names_by_team, alloc_bias=0):
    rows = []
    for team, names in names_by_team.items():
        for me in names:
            for i, other in enumerate(n for n in names if n != me):
                rows.append({
                    "evaluator": me, "evaluator_key": me.lower(),
                    "evaluator_team": team, "evaluatee": other,
                    "evaluatee_key": other.lower(),
                    "points": 100 / (len(names) - 1) + (alloc_bias if i == 0 else 0),
                    "public_comment": f"{other} wrote the analysis.",
                    "contribution_text": f"{other} wrote the analysis.",
                    "improve_text": "", "confidential_comment": "",
                    "rank": float(1 + i % 3),
                    **{f"r{d}": 5.0 for d in range(4)}})
    return pd.DataFrame(rows)


def _text(pdf_bytes):
    fitz = pytest.importorskip("fitz")
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return "\n".join(p.get_text() for p in doc), doc.page_count


def test_the_team_contribution_pdf_lists_every_member():
    teams = compute(_section({"7": ["Zoë O'Brien", "Bob Brown", "Cid Cole"]}),
                    GradeSettings())
    data = pdfgen.build_team_contribution_pdf(teams[0], "2")
    assert data.startswith(b"%PDF")
    text, _ = _text(data)
    for name in ("Zoë O'Brien", "Bob Brown", "Cid Cole"):
        assert name in text, name
    assert "Team 7" in text


def test_the_team_contribution_pdf_paginates_a_large_team():
    names = [f"Student {i:02d}" for i in range(40)]
    teams = compute(_section({"1": names}), GradeSettings())
    _text_out, pages = _text(pdfgen.build_team_contribution_pdf(teams[0], "1"))
    assert pages >= 1
    assert "Student 39" in _text_out, "the last member must not fall off the page"


def test_the_comparison_pdf_covers_every_round_and_every_student():
    r1 = compute(_section({"1": ["Ann Apple", "Bob Brown", "Cid Cole"]}),
                 GradeSettings())
    r2 = compute(_section({"1": ["Ann Apple", "Bob Brown", "Cid Cole",
                                 "Gus Gold"]}, alloc_bias=10), GradeSettings())
    data = pdfgen.build_comparison_pdf("CMP101", [("1", r1), ("2", r2)])
    text, _ = _text(data)
    for name in ("Ann Apple", "Gus Gold"):
        assert name in text, name


def test_the_comparison_pdf_survives_a_single_round_and_an_empty_one():
    r1 = compute(_section({"1": ["Ann Apple", "Bob Brown"]}), GradeSettings())
    assert pdfgen.build_comparison_pdf("CMP101", [("1", r1)]).startswith(b"%PDF")
    assert pdfgen.build_comparison_pdf("CMP101", [("1", r1), ("2", [])]
                                       ).startswith(b"%PDF")


# --------------------------------------------------------------------------- #
# The Compare tab
# --------------------------------------------------------------------------- #
def test_compare_builds_once_and_matches_students_across_spellings(tmp_path):
    """Three real rounds: a student who changes team, one who joins in round 2,
    and one written "Last, First" in round 3. The driver is
    tests/_compare_app.py."""
    done = subprocess.run(
        [sys.executable, os.path.join(REPO, "tests", "_compare_app.py"),
         str(tmp_path / "cmp")],
        capture_output=True, text=True, timeout=900, cwd=REPO)
    assert "FAILURES: none" in done.stdout, done.stdout[-5000:]
    assert done.returncode == 0, done.stdout[-5000:]
    for line in ("PASS - pressing it reads each round once",
                 "PASS - clicking elsewhere afterwards does not rebuild it",
                 "PASS - one row per student, not per spelling",
                 "PASS - a stale table is not shown for a different choice of rounds"):
        assert line in done.stdout, done.stdout[-5000:]


# --------------------------------------------------------------------------- #
# The setup screen cannot cost anyone their data
# --------------------------------------------------------------------------- #
def test_a_storage_failure_is_not_read_as_nobody_can_sign_in(vault, monkeypatch):
    """On a deployment that signs in only with stored accounts, a failure to
    read them used to come back as "no accounts" — and the setup screen then
    stood in front of the sign-in form offering a fresh configuration."""
    from peerparley import setup_wizard
    from peerparley.config import load_config
    accounts.add_account("jdoe", "J Doe", "instructor", "correct horse", vault)

    cfg = load_config()
    monkeypatch.setattr(setup_wizard, "load_config",
                        lambda: type("C", (), {"app_password_sha256": ""})())
    assert setup_wizard.has_credential() is True, "readable accounts: yes"

    from peerparley import vault as vault_mod

    def unreachable(self):
        raise ConnectionError("provider timed out")

    monkeypatch.setattr(vault_mod.Vault, "list", unreachable)
    vault_mod.clear_cache()
    assert setup_wizard.has_credential() is True, (
        "storage that cannot answer must never be taken to mean nobody can "
        "sign in")
    assert cfg is not None


def test_with_a_working_key_the_setup_screen_never_offers_a_new_one():
    """If it is ever shown to a deployment that has stored data, the most it
    can hand out is a password line. A new key pasted over the old one makes
    every stored record unreadable, by anybody, for good."""
    from peerparley import setup_wizard
    block = setup_wizard.password_only_block(setup_wizard.password_hash("x" * 12))
    assert "app_password_sha256" in block
    assert "fernet_key =" not in block, "a working key must never be replaced"
    source = open(os.path.join(REPO, "peerparley", "setup_wizard.py"),
                  encoding="utf-8").read()
    render = source.split("def render()", 1)[1]
    # The only call that mints a key is on the branch where no key works.
    assert render.count("new_fernet_key()") == 1
    branch = render.split("new_fernet_key()", 1)[0][-900:]
    assert "elif not missing_key:" in branch, (
        "new_fernet_key() must only be reachable when there is no usable key")

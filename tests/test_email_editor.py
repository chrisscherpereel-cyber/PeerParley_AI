"""The email body: written as it looks, previewed as the student receives it.

Reported: *"The email messages shown in the application should be WYSIWYG not
HTML. And editable in WYSIWYG model."*

The instructor was shown the markup. Every email body was edited in a box
headed **Body (HTML)** holding
``Hi {first_name},<br><br>…<a href="{link}">Open my peer evaluation</a>``, and
the only rendered preview anywhere was one on the results tab — the invitation
and the reminder, the two messages a whole section actually receives, had none.

A WYSIWYG editor existed, but `streamlit-quill` sat in
``requirements-optional.txt`` and Streamlit Community Cloud installs only
``requirements.txt``, so every hosted deployment fell through to the raw
textarea. Third time for that mistake: the storage backends in 2.10.0, PyMuPDF
in 2.15.0, this.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pandas as pd
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from peerparley import email_ui  # noqa: E402
from peerparley import email_delivery as mail  # noqa: E402
from peerparley import survey  # noqa: E402
from peerparley.tokens import read_token  # noqa: E402
from peerparley.vault import Vault  # noqa: E402

BODY = ("Hi {first_name},<br><br>Your evaluation for {class} "
        '(Evaluation {eval_no}) is ready.<br><br>'
        '<a href="{link}">Open my peer evaluation</a><br><br>'
        "Thanks,<br>The teaching team")


# --------------------------------------------------------------------------- #
# The package has to ship
# --------------------------------------------------------------------------- #
def test_the_toolbar_package_is_a_core_requirement():
    """Cloud installs only requirements.txt. With the toolbar listed as
    optional, every hosted instructor was handed the raw HTML of the message
    instead of the message."""
    core = open(os.path.join(REPO, "requirements.txt"), encoding="utf-8").read()
    optional = open(os.path.join(REPO, "requirements-optional.txt"),
                    encoding="utf-8").read()
    assert "streamlit-quill" in core
    assert not any(ln.strip().startswith("streamlit-quill")
                   for ln in optional.splitlines()), \
        "it must not also be declared as optional"


def test_the_editor_is_the_only_one_in_the_app():
    """One component, so all three send paths get the same editor, the same
    preview and the same placeholder help."""
    source = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
    assert source.count("st_quill(") == 0, \
        "app.py should call email_ui.body_editor, not Quill directly"
    assert source.count("email_ui.body_editor") == 1


# --------------------------------------------------------------------------- #
# The preview
# --------------------------------------------------------------------------- #
def test_the_preview_uses_a_real_recipient_when_there_is_one():
    """A placeholder that never got filled in, or a link with no host in front
    of it, shows up here rather than in forty inboxes."""
    links = [{"name": "Ann Apple", "team": "3", "email": "ann@x.edu",
              "link": "https://pp.example/?t=abc"}]
    ctx = email_ui.sample_ctx("MGT 301", "2", links)
    assert ctx["first_name"] == "Ann" and ctx["last_name"] == "Apple"
    assert ctx["team"] == "3" and ctx["class"] == "MGT 301"
    assert ctx["eval_no"] == "2"
    assert ctx["link"] == "https://pp.example/?t=abc"


def test_the_preview_still_works_with_no_recipients_yet():
    ctx = email_ui.sample_ctx("", "", [])
    rendered = mail.render_template(BODY, ctx)
    assert "{" not in rendered, f"a placeholder was left unfilled: {rendered}"
    assert ctx["name"]


def test_every_placeholder_the_help_advertises_is_actually_filled_in():
    """The caption lists them, so each one has to work — otherwise the app is
    telling the instructor to write something it will not substitute."""
    ctx = email_ui.sample_ctx("MGT 301", "1", [])
    template = " ".join(email_ui.PLACEHOLDERS)
    rendered = mail.render_template(template, ctx)
    assert not re.search(r"\{[A-Za-z_]+\}", rendered), rendered


def test_a_mistyped_placeholder_is_caught_before_sending():
    assert email_ui.unfilled_placeholders(
        "Hi {firstname}", "your {teamname}") == ["{firstname}", "{teamname}"]
    assert email_ui.unfilled_placeholders(BODY) == []
    # Reported once each, however many times they appear.
    assert email_ui.unfilled_placeholders("{x} {x} {x}") == ["{x}"]


# --------------------------------------------------------------------------- #
# The plain-text version
# --------------------------------------------------------------------------- #
def test_the_plain_text_version_is_readable():
    text = email_ui.html_to_text(BODY)
    assert "<" not in text and ">" not in text
    assert "Hi {first_name}," in text
    assert "Open my peer evaluation" in text, \
        "the link text has to survive, or the message loses its instruction"
    assert "\n" in text, "<br> has to become a line break, not vanish"


def test_the_plain_text_version_decodes_the_entities_it_meets():
    assert email_ui.html_to_text("a&nbsp;b &amp; c &lt;d&gt;") == "a b & c <d>"


def test_paragraph_markup_from_the_toolbar_becomes_blank_lines():
    """Quill writes <p>…</p>, not <br>."""
    out = email_ui.html_to_text("<p>One</p><p>Two</p>")
    assert out.startswith("One") and "Two" in out
    assert "\n\n" in out


# --------------------------------------------------------------------------- #
# Reminders carry the same link as the invitation
# --------------------------------------------------------------------------- #
@pytest.fixture()
def vault(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_BACKEND", "local")
    monkeypatch.setenv("VAULT_FOLDER", "email-editor")
    monkeypatch.delenv("TOKEN_SECRET", raising=False)
    return Vault()


def test_the_reminder_path_does_not_mint_its_own_positional_tokens(vault):
    """It used to build `{s, t, p}` itself — the scheme 2.16.0 replaced — so a
    reminder sent after any roster edit pointed at the wrong student."""
    source = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
    reminder = source.split("Send a reminder to non-responders", 1)[1][:3000]
    assert "make_token(" not in reminder, \
        "reminders must reuse survey.student_links, which carries the id"
    assert "student_links(" in reminder


def test_a_reminder_link_is_the_students_own_link(vault):
    rows = [{"First Name": f, "Last Name": "X", "Email": f"{f}@x.edu",
             "Team": "1"} for f in ("Ann", "Bob", "Cid")]
    slug, _ = survey.save_setup("EM101", "1", pd.DataFrame(rows),
                                dict(survey.DEFAULT_SURVEY), vault=vault,
                                owner="admin")
    secret = survey.token_secret()
    first = {l["name"]: l["link"] for l in survey.student_links(
        "https://pp.example", slug, survey.saved_teams(vault, slug), secret)}

    # A student is added, which under the old scheme renumbered the team.
    survey.add_student(vault, slug, "Abe X", "1", "abe@x.edu")
    again = {l["name"]: l["link"] for l in survey.student_links(
        "https://pp.example", slug, survey.saved_teams(vault, slug), secret)}
    for name, link in first.items():
        assert again[name] == link, f"{name}'s reminder link must not change"
    snap = survey.roster_now(vault, slug)
    for name, link in again.items():
        payload = read_token(link.split("t=", 1)[1], secret)
        resolved = survey.resolve_token(payload, snap)
        assert survey.member_by_id(snap, resolved)["name"] == name


# --------------------------------------------------------------------------- #
# The whole page
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("quill", [True, False],
                         ids=["with-toolbar", "without-toolbar"])
def test_the_editor_and_previews_work_in_the_real_app(tmp_path, quill):
    """The complaint is about the page, so it is checked on the page — twice,
    because the broken case was the one with no toolbar package, which is how
    every Streamlit Cloud deployment was configured. The driver is
    tests/_email_editor_app.py.
    """
    args = [sys.executable, os.path.join(REPO, "tests", "_email_editor_app.py"),
            str(tmp_path / ("quill" if quill else "noquill"))]
    if not quill:
        args.append("--no-quill")
    done = subprocess.run(args, capture_output=True, text=True, timeout=900,
                          cwd=REPO)
    assert "FAILURES: none" in done.stdout, done.stdout[-5000:]
    assert done.returncode == 0, done.stdout[-5000:]
    for line in ("PASS - the invitation has a rendered preview",
                 "PASS - the preview is rendered, not shown as markup",
                 "PASS - the preview fills in a real student's name",
                 "PASS - an HTML escape hatch is offered, not forced",
                 "PASS - a placeholder nothing fills in is flagged before sending"):
        assert line in done.stdout, done.stdout[-5000:]
    if quill:
        assert ("PASS - with the package the body is not edited as HTML"
                in done.stdout), done.stdout[-5000:]
    else:
        assert ("PASS - and it says which package would give the toolbar"
                in done.stdout), done.stdout[-5000:]

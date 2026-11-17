"""Web mail (Outlook on the web, Gmail) and the Start-here panel.

Requested: *"when a user logs into peer parley they should not see progress in
the start here since no survey has been selected. Is there a way to setup a
send option that will use a web based mail tool?"*

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from peerparley import email_ui, webmail  # noqa: E402

BODY = ("<p>Hi Ann,</p><p>Your evaluation for <strong>MGT 301</strong> is "
        'ready: <a href="https://pp.example/?t=abc&amp;x=1">Open my peer '
        "evaluation</a></p><ul><li>Takes 5 minutes</li><li>Due Friday</li></ul>"
        "<p>Thanks &amp; good luck,<br>The teaching team</p>")


def _q(url: str) -> dict:
    return {k: v[0] for k, v in
            urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


def test_plain_text_keeps_every_link_address():
    """Stripping tags used to drop the student's link with the <a> tag."""
    text = webmail.plain_text(BODY)
    assert "Open my peer evaluation: https://pp.example/?t=abc&x=1" in text
    assert "<" not in text and "&amp;" not in text
    assert "• Takes 5 minutes" in text and "• Due Friday" in text
    assert "Thanks & good luck,\nThe teaching team" in text


def test_a_link_whose_text_is_its_address_is_written_once():
    t = webmail.plain_text('<a href="https://x.example/a">https://x.example/a</a>')
    assert t == "https://x.example/a"
    t = webmail.plain_text('<a href="mailto:me@x.edu">me@x.edu</a>')
    assert t == "me@x.edu"


def test_the_preview_plain_text_uses_the_same_rendering():
    assert email_ui.html_to_text(BODY) == webmail.plain_text(BODY)


def test_outlook_compose_url():
    u = webmail.compose_url("Outlook on the web", "ann@example.edu",
                            "Your evaluation — Eval 1", BODY)
    assert u.startswith("https://outlook.office.com/mail/deeplink/compose?")
    q = _q(u)
    assert q["to"] == "ann@example.edu"
    assert q["subject"] == "Your evaluation — Eval 1"
    assert q["body"] == webmail.plain_text(BODY)


def test_gmail_compose_url():
    u = webmail.compose_url("Gmail", "ann@example.edu", "S & T", BODY)
    assert u.startswith("https://mail.google.com/mail/?")
    q = _q(u)
    assert q["view"] == "cm" and q["fs"] == "1"
    assert q["to"] == "ann@example.edu" and q["su"] == "S & T"
    assert q["body"] == webmail.plain_text(BODY)


def test_every_listed_provider_builds_a_url_and_others_are_refused():
    for p in webmail.PROVIDERS:
        assert webmail.compose_url(p, "a@b.c", "s", "b").startswith("https://")
    with pytest.raises(ValueError):
        webmail.compose_url("Hotmail", "a@b.c", "s", "b")


def test_the_page(tmp_path):
    """Start here before and after a survey is chosen, and the web-mail
    buttons on the invitation; the driver is tests/_webmail_app.py."""
    done = subprocess.run(
        [sys.executable, os.path.join(REPO, "tests", "_webmail_app.py"),
         str(tmp_path / "wm")], capture_output=True, text=True, timeout=900,
        cwd=REPO)
    assert "FAILURES: none" in done.stdout, done.stdout[-5000:]
    assert done.returncode == 0, done.stdout[-5000:]
    for line in ("PASS - Start here asks for a survey",
                 "PASS - and shows no progress",
                 "PASS - one button per student",
                 "PASS - the student's own link is in the body",
                 "PASS - Gmail opens Gmail's composer"):
        assert line in done.stdout, done.stdout[-5000:]

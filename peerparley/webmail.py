"""Compose links for web mail: Outlook on the web and Gmail.

Requested: *"Is there a way to setup a send option that will use a web based
mail tool?"*

The app could already send through Microsoft 365, but only with an app
registration in the university's Azure tenant — a request to central IT, not
something an instructor sets up on a Tuesday. Everything else ran through a
mail program on the instructor's own computer. An instructor who lives in the
browser had no way in.

Both services accept a "compose" address that opens a new message with the
recipient, subject and body already filled in. Nothing is sent until the
instructor presses Send in their own mailbox, so this is a review step by
construction, and the message comes from their own address. The costs are the
ones the method cannot avoid, and the page says them up front: one click per
student, plain text only (the student's link still works — it is written out),
and no attachments.

Kept free of Streamlit so the URLs can be tested directly.
"""
from __future__ import annotations

import html as _html
import re
import urllib.parse
from typing import Dict

PROVIDERS: Dict[str, str] = {
    "Outlook on the web": "Microsoft 365 work or school mailbox (outlook.office.com)",
    "Gmail": "Gmail or Google Workspace (mail.google.com)",
}


def plain_text(body_html: str) -> str:
    """The message as plain text, keeping every link's address.

    A compose link — like a mailto link — cannot carry HTML. Stripping the tags
    naively turned ``<a href="{link}">Open my peer evaluation</a>`` into the
    words "Open my peer evaluation" and threw the student's link away. The
    address is now written out after the words, unless the words already are
    the address.
    """
    s = body_html or ""

    def _anchor(m: "re.Match") -> str:
        href = _html.unescape(m.group(1)).strip()
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not href or href.lower().startswith("javascript:"):
            return text
        shown = href[7:] if href.lower().startswith("mailto:") else href
        if not text or _html.unescape(text) in (href, shown):
            return shown
        return f"{text}: {shown}"

    s = re.sub(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']*)[\"'][^>]*>(.*?)</a\s*>",
               _anchor, s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<li\b[^>]*>", "\n• ", s, flags=re.I)
    s = re.sub(r"</(p|div|ul|ol|h[1-6])\s*>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def compose_url(provider: str, to: str, subject: str, body_html: str) -> str:
    """A link that opens a pre-filled new message in the chosen web mail."""
    body = plain_text(body_html)
    if provider == "Gmail":
        q = {"view": "cm", "fs": "1", "to": to or "", "su": subject or "",
             "body": body}
        return ("https://mail.google.com/mail/?"
                + urllib.parse.urlencode(q, quote_via=urllib.parse.quote))
    if provider == "Outlook on the web":
        q = {"to": to or "", "subject": subject or "", "body": body}
        return ("https://outlook.office.com/mail/deeplink/compose?"
                + urllib.parse.urlencode(q, quote_via=urllib.parse.quote))
    raise ValueError(f"unknown web mail provider: {provider!r}")

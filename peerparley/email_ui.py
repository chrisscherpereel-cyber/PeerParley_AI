"""Writing and previewing the emails students receive, as they will see them.

The problem this solves
-----------------------
The instructor was shown the markup. The body of every email was edited in a
box headed **Body (HTML)** containing

    Hi {first_name},<br><br>It's time for the peer evaluation for {class}…
    <a href="{link}">Open my peer evaluation</a><br><br>

and the only place anything was *rendered* was one preview on the results tab.
Invitations and reminders — the two messages a whole section actually
receives — had no preview at all, so the first time anyone saw the result was
in a student's inbox.

A WYSIWYG editor existed but almost nobody got it: `streamlit-quill` was listed
in ``requirements-optional.txt``, and Streamlit Community Cloud installs only
``requirements.txt``. So every hosted deployment fell through to the raw
textarea. That is the same mistake as the storage backends in 2.10.0 and
PyMuPDF in 2.15.0, and it has the same fix.

What this module gives every email in the app
---------------------------------------------
* `body_editor` — a formatting toolbar, editing what the message looks like
  rather than what it is made of. The stored and sent body is still HTML,
  because that is what an email is; the instructor simply no longer writes it.
* `preview` — the message as one real student receives it, with their name,
  team and link filled in. Offered on every send path, not one of them.
* an **Edit the HTML directly** escape hatch, for pasting in markup from
  somewhere else. Opt-in, and the toolbar is the default.
* `placeholder_help` — what the braces mean, said once.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

import streamlit as st

from . import email_delivery as mail

PLACEHOLDERS = {
    "{first_name}": "the student's first name",
    "{last_name}": "their surname",
    "{name}": "their full name",
    "{team}": "their team",
    "{class}": "the course label you typed on tab ①",
    "{eval_no}": "the evaluation number",
    "{link}": "their own personal evaluation link",
}

# Quill's own toolbar, trimmed to what an instructor writing to a class needs.
# Fonts, colours and embedded video are the parts that render badly in Outlook
# or get stripped by a mail client, so they are left out rather than offered
# and then silently lost.
TOOLBAR = [
    ["bold", "italic", "underline"],
    [{"list": "ordered"}, {"list": "bullet"}],
    ["link"],
    ["clean"],
]


def html_to_text(html: str) -> str:
    """A plain-text rendering, for a mail client that will not show HTML."""
    text = re.sub(r"<br\s*/?>", "\n", html or "")
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").strip())


def sample_ctx(course: str, eval_no: str,
               recipients: Optional[List[dict]] = None) -> Dict[str, str]:
    """The values to preview with: a real recipient's where there is one.

    Previewing against a real student is the point — a placeholder that never
    got filled in, or a link that is a bare "?t=…" because the app's address
    was left blank, shows up immediately rather than in forty inboxes.
    """
    first = next((r for r in (recipients or []) if r.get("name")), None)
    name = (first or {}).get("name", "Jordan Reyes")
    parts = name.split()
    return {
        "first_name": parts[0] if parts else name,
        "last_name": parts[-1] if len(parts) > 1 else "",
        "name": name,
        "team": str((first or {}).get("team", "1")),
        "class": course or "your course",
        "eval_no": str(eval_no or "1"),
        "link": (first or {}).get("link", "https://your-peerparley-address/?t=…"),
    }


def placeholder_help() -> None:
    st.caption(
        "Type as you want the message to read — the toolbar handles the "
        "formatting. These placeholders are filled in per student when it is "
        "sent: " + ", ".join(f"`{k}`" for k in PLACEHOLDERS))


def body_editor(default_html: str, key: str, height: int = 220) -> str:
    """The message body, edited as it will look. Returns HTML.

    `key` must be unique on the page. The body lives in session state under
    ``<key>_html`` so it survives the reruns that every other control on the
    page causes.
    """
    state_key = f"{key}_html"
    if state_key not in st.session_state:
        st.session_state[state_key] = default_html

    placeholder_help()
    as_html = st.checkbox(
        "Edit the HTML directly", key=f"{key}_raw",
        help="For pasting in markup you already have. Leave it off to write "
             "with the toolbar.")

    if as_html:
        edited = st.text_area("Body (HTML)", st.session_state[state_key],
                              height=height, key=f"{key}_ta")
        st.session_state[state_key] = edited
        return edited

    try:
        from streamlit_quill import st_quill
    except Exception:  # noqa: BLE001
        # Not reachable on a normal install: streamlit-quill is in
        # requirements.txt precisely so this cannot happen on Streamlit Cloud,
        # where it used to happen to everybody. Said plainly if it ever does.
        st.info("The formatting toolbar needs the **streamlit-quill** package "
                "(`pip install streamlit-quill`, or add `streamlit-quill` to "
                "requirements.txt on Streamlit Cloud). Until then the body is "
                "edited as HTML — the preview below still shows what the "
                "student will see.", icon="🖊️")
        edited = st.text_area("Body (HTML)", st.session_state[state_key],
                              height=height, key=f"{key}_ta_fallback")
        st.session_state[state_key] = edited
        return edited

    edited = st_quill(value=st.session_state[state_key], html=True,
                      toolbar=TOOLBAR, key=f"{key}_q")
    # The component returns None before it has mounted, and an empty paragraph
    # when the instructor clears the box. The first means "nothing yet, keep
    # what we have"; the second is a deliberate edit, and treating it as the
    # first is why the body used to spring back to the default text when you
    # tried to empty it.
    if edited is not None:
        st.session_state[state_key] = edited
    return st.session_state[state_key]


def preview(subject_t: str, body_t: str, ctx: Dict[str, str],
            label: str = "Preview — what one student receives") -> None:
    """Render one message the way its recipient will see it."""
    with st.expander(label, expanded=False):
        st.caption(f"Shown for **{ctx.get('name', 'a student')}**"
                   + (f" (team {ctx['team']})" if ctx.get("team") else "")
                   + ". Every other student gets the same message with their "
                     "own details.")
        st.markdown(f"**Subject:** {mail.render_template(subject_t, ctx)}")
        st.divider()
        # The body is HTML because an email is HTML. Rendered here, not shown
        # as markup — being handed the markup was the complaint.
        st.markdown(mail.render_template(body_t, ctx), unsafe_allow_html=True)
        if "{link}" in (body_t or "") and "…" in ctx.get("link", ""):
            st.caption("The link is a placeholder here because this app's web "
                       "address is not filled in yet.")
        with st.expander("The plain-text version (for mail clients that "
                         "don't show formatting)"):
            st.text(html_to_text(mail.render_template(body_t, ctx)))


def unfilled_placeholders(*templates: str) -> List[str]:
    """Braced names in a template that nothing will ever fill in.

    A typo like ``{firstname}`` is silently sent as-is to every student, which
    is the kind of thing an instructor only hears about afterwards.
    """
    known = set(PLACEHOLDERS)
    found: List[str] = []
    for text in templates:
        for token in re.findall(r"\{[A-Za-z0-9_]+\}", text or ""):
            if token not in known and token not in found:
                found.append(token)
    return found


def warn_unfilled(subject_t: str, body_t: str) -> None:
    bad = unfilled_placeholders(subject_t, body_t)
    if bad:
        st.warning(
            "This message contains "
            + ", ".join(f"`{b}`" for b in bad)
            + ", which nothing fills in — students would receive it exactly as "
              "written. The ones that work are "
            + ", ".join(f"`{k}`" for k in PLACEHOLDERS) + ".", icon="⚠️")

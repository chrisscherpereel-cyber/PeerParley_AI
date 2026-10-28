"""PeerParley — single instructor/administrator application.

Runs on Streamlit Cloud. All student PII is encrypted at rest and persisted
only to the university-controlled vault (M365 / Dropbox / pCloud). The public
host never stores plaintext PII.

Workflow (one app):
    Set up & send survey  →  Collect responses  →  (or Upload a Qualtrics export)
    →  Configure grading   →  Review + PDFs      →  Deliver / draft emails
    plus: save/load encrypted vault bundles.

v2 adds an optional AI step between grading and delivery: each student's written
peer comments are rewritten into a readable narrative, checked against those same
comments, and shown to the instructor for approval. Nothing generated reaches a
student without that approval. See peerparley/feedback_ai.py and docs/AI_FEEDBACK.md.

Students never sign in: a personal ?t=<token> link opens their evaluation form
directly (see peerparley/survey.py), bypassing the instructor password gate.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import io
import os
import re
import urllib.parse
import zipfile

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from peerparley import __version__
from peerparley import accounts
from peerparley.auth import logout, require_login
from peerparley.config import load_config
from peerparley import ingest
from peerparley import survey
from peerparley import emailpack
from peerparley import ai_ui
from peerparley import workspace
from peerparley.grading import (
    GradeSettings,
    compute,
    data_warnings as grading_warnings,
    results_to_frame,
)
from peerparley import pdfgen
from peerparley import email_delivery as mail
from peerparley.vault import Vault
from peerparley.ui_helpers import (
    render_pdf as _render_pdf,
    student_ctx as _ctx,
    build_messages as _build_messages,
    make_mailer as _make_mailer,
)

# --------------------------------------------------------------------------- #
# Brand assets (Parley logo). page_icon falls back to an emoji if the asset
# file wasn't uploaded, so the app never crashes on a missing image.
# --------------------------------------------------------------------------- #
_ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_ICON_PNG = os.path.join(_ASSET_DIR, "peerparley_mark.png")

st.set_page_config(
    page_title="PeerParley",
    page_icon=_ICON_PNG if os.path.exists(_ICON_PNG) else "✅",
    layout="wide",
)

# --------------------------------------------------------------------------- #
# Brand header — the Parley mark (two speech bubbles) + wordmark + tagline.
# --------------------------------------------------------------------------- #
st.markdown(
    """
    <div style="display:flex;align-items:center;gap:14px;padding:6px 0 2px">
      <svg width="290" height="60" viewBox="0 0 300 78"
           xmlns="http://www.w3.org/2000/svg" role="img" aria-label="PeerParley">
        <rect x="2" y="10" width="40" height="27" rx="11" fill="#0E2A3B"/>
        <path d="M11 36 L11 48 L24 36 Z" fill="#0E2A3B"/>
        <rect x="22" y="28" width="40" height="27" rx="11" fill="#3FB07A"/>
        <path d="M50 54 L50 66 L38 54 Z" fill="#3FB07A"/>
        <text x="80" y="41" font-family="Helvetica,Arial,sans-serif" font-size="30"
              font-weight="800" fill="#0E2A3B">Peer<tspan fill="#0B7A4B">Parley</tspan></text>
        <text x="82" y="62" font-family="Helvetica,Arial,sans-serif" font-size="13"
              font-style="italic" fill="#6B7A80">evaluation, made clear</text>
      </svg>
    </div>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Student form — the only public surface. A valid ?t=<token> link renders the
# student's personal evaluation and records their submission, BEFORE (and
# instead of) the instructor password gate.
# --------------------------------------------------------------------------- #
_token = None
try:
    _token = st.query_params.get("t")
except Exception:
    _qp = st.experimental_get_query_params()
    _token = (_qp.get("t") or [None])[0]
if _token:
    survey.render_student_app(_token)
    st.stop()

if not require_login():
    st.stop()

cfg = load_config()
S = st.session_state


# --------------------------------------------------------------------------- #
# Two things Streamlit makes easy to get wrong, in one place
# --------------------------------------------------------------------------- #
# Streamlit re-runs this whole file on every click, keystroke and checkbox. Two
# consequences were costing real time on every interaction:
#
#   * `Vault()` builds a backend and, on Microsoft 365 or pCloud, acquires a
#     fresh auth token per instance — and this file used to construct ten per
#     run. One cached instance per session instead.
#   * `compute()` regraded all forty students on every rerun: measured at 1.1
#     to 10 seconds for teams of five and 44 seconds for teams of ten, mostly
#     in the comment-similarity comparisons. Cached on the inputs, a rerun that
#     changes nothing costs milliseconds, and changing any grading setting
#     recomputes exactly as before.
#
# The cache key is the data plus every grading setting, so this changes *when*
# grades are computed and never *what* they are.

@st.cache_resource(show_spinner=False)
def _session_vault() -> Vault:
    return Vault()


def vault_now() -> Vault:
    """The session's vault. Cheap to call as often as the code reads naturally."""
    try:
        return _session_vault()
    except Exception:  # noqa: BLE001 - never let caching break the page
        return Vault()


def _results_sig(teams) -> str:
    """A digest of everything the instructor PDFs put on a page.

    Used as the cache key for those PDFs: a list of dataclasses is not reliably
    hashable, so the signature stands in for it. It has to cover every field the
    PDFs render — change a grading setting or a comment and the key changes, so
    a stale PDF cannot be served.
    """
    import hashlib
    bits = []
    for t in teams:
        for m in t.members:
            bits.append("|".join(str(x) for x in (
                t.team, m.key, m.name, m.team_score, m.individual_score,
                m.signed_pct, m.multiplier, m.A, m.Q, m.pay_grade, m.performance,
                m.comment_points, m.response_points, m.forced_rank_mean,
                m.team_player, m.quantity, m.quality, m.effect,
                m.self_responded, m.no_peer_input,
                len(m.confidential_comments),
                sum(len(c) for c in m.confidential_comments))))
    return hashlib.sha256("\n".join(bits).encode("utf-8")).hexdigest()


@st.cache_data(show_spinner=False, max_entries=4)
def _instructor_pdf(which: str, sig: str, course: str, eval_no: str,
                    _teams=None) -> bytes:
    """One of the two instructor PDFs, built once per set of results.

    `_teams` is deliberately outside the cache key — Streamlit skips
    underscore-prefixed arguments — and `sig` stands in for it.
    """
    if which == "confidential":
        return pdfgen.build_confidential_pdf(_teams, course, eval_no)
    return pdfgen.build_section_summary_pdf(_teams, course, eval_no)


@st.cache_data(ttl=30, show_spinner=False, max_entries=8)
def _response_status(slug: str) -> list:
    """Who has answered, read at most once every 30 seconds.

    Every call decrypts every stored submission for the section — forty files
    on a full course — and it used to happen on each rerun, so watching the
    progress table cost forty decryptions per click. The refresh button clears
    this, so "has anyone answered in the last minute?" is still one click away.
    """
    return survey.response_status(vault_now(), slug)


def _report_settings(course: str, eval_no: str) -> dict:
    """Which sections a student's report shows.

    Tabs ④ and ⑤ used to read this only from storage, so the "What students see
    in their feedback report" choice on tab ① did nothing until the survey was
    saved — and after resuming a session, with no contact list in the uploader,
    the save button was disabled and the choice could not be applied at all.
    What the instructor has on screen for *this* survey wins; the saved copy is
    the fallback and still governs what students see in their own form.
    """
    cfg_now = S.get("survey_cfg") or {}
    if (str(cfg_now.get("course", course) or course) == course
            and (cfg_now.get("report") or {})):
        return dict(cfg_now["report"])
    try:
        return dict(survey.load_survey(
            vault_now(), survey.slugify(course, eval_no)).get("report") or {})
    except Exception:  # noqa: BLE001
        return {}


@st.cache_data(show_spinner=False, max_entries=8)
def _compute_cached(long_df: pd.DataFrame, settings_tuple: tuple,
                    _self_sig: str, _self_evals: dict):
    return compute(long_df, GradeSettings(*settings_tuple),
                   self_evals=_self_evals or {})


def graded(long_df: pd.DataFrame, settings: GradeSettings, self_evals=None):
    """compute(), but only when something that affects the answer has changed."""
    import hashlib
    sig = hashlib.sha256(
        repr(sorted((str(k), str(v)) for k, v in (self_evals or {}).items()))
        .encode("utf-8")).hexdigest()
    try:
        return _compute_cached(long_df, dataclasses.astuple(settings), sig,
                               self_evals or {})
    except Exception:  # noqa: BLE001 - an uncacheable input must still grade
        return compute(long_df, settings, self_evals=self_evals or {})
user = st.session_state.get("pp_user") or {"user": "admin", "name": "Administrator",
                                           "role": "admin", "source": "secrets"}
is_admin = accounts.is_admin(user)

with st.sidebar:
    st.markdown(f"**{user.get('name', 'User')}**")
    st.caption(f"`{user.get('user')}` · "
               + ("administrator — every section" if is_admin
                  else "instructor — your sections only"))
    ok, msg = vault_now().healthcheck()
    (st.success if ok else st.warning)(msg)
    st.caption(f"Storage: **{cfg.vault.backend}** · v{__version__}")
    if st.button("Sign out"):
        logout(); st.rerun()

    # ---- change my own password (vault accounts only) --------------------
    if user.get("source") == "vault":
        with st.expander("Change my password"):
            p1 = st.text_input("New password", type="password", key="own_pw1")
            p2 = st.text_input("Confirm", type="password", key="own_pw2")
            if st.button("Save password", key="own_pw_save"):
                if p1 != p2:
                    st.error("Those don't match.")
                elif len(p1) < 8:
                    st.error("Use at least 8 characters.")
                else:
                    try:
                        accounts.set_password(user["user"], p1, must_change=False)
                        st.success("Changed.")
                    except Exception as exc:  # noqa: BLE001
                        st.error(str(exc))

    st.divider()
    st.markdown("### Working on")

    # ---- Resume an earlier session --------------------------------------
    # The responses, the self-evaluations and the roster all used to live in
    # session state alone, so signing out discarded an evening's work — and
    # because the AI drafts are keyed by the course name, losing the course box
    # made the saved drafts unreachable too. Restoring the workspace as a unit
    # brings the course name back with it, which is what reconnects them.
    _uname = str(user.get("user") or "default")
    if "_ws_checked" not in S:
        S["_ws_checked"] = True
        _st, _err = workspace.load(vault_now(), _uname)
        S["_ws_saved"] = _st
        S["_ws_unreadable"] = bool(_err)
    if S.get("_ws_unreadable"):
        # Autosaving over a session that exists but cannot be decrypted would
        # destroy the only copy. Say so, and hold the autosave off for the rest
        # of this session.
        st.error(
            "**There is a saved session here that cannot be opened**, almost "
            "always because the app's encryption key changed since it was "
            "saved. Automatic saving is paused so that it is not overwritten. "
            "Put the original key back to recover it, or use the Vault tab to "
            "save this session's work under a new name.",
            icon="🔑",
        )
    _saved = S.get("_ws_saved")
    if _saved and "long_df" not in S:
        st.info("**Earlier session found**\n\n" + workspace.describe(_saved),
                icon="💾")
        r1, r2 = st.columns([1.4, 1])
        if r1.button("↩ Resume it", type="primary", use_container_width=True,
                     key="ws_resume"):
            S["long_df"] = _saved["long_df"]
            S["self_evals"] = _saved["self_evals"]
            S["roster"] = _saved["roster"]
            # The grading rules come back too. Resuming into the defaults meant
            # the same students quietly scored differently than the day before.
            if _saved.get("settings"):
                try:
                    S["settings"] = GradeSettings(**_saved["settings"])
                    S["_settings_restored"] = True
                except Exception:  # noqa: BLE001 - an older save without them
                    pass
            # Written to the widget keys, not just to S, so the text boxes below
            # actually show the course again — that is what rebuilds the slug
            # the AI drafts were saved under.
            S["new_course"] = _saved["course"]
            S["new_eval"] = _saved["eval_no"] or "1"
            # Resuming moves the course box, which moves the slug. The
            # cross-survey guard further down reads that as a survey switch and
            # would wipe the data just restored, so the slug is pre-set here to
            # the one being resumed into: a resume is not a switch.
            S["_active_slug"] = survey.slugify(
                _saved["course"], _saved["eval_no"] or "1")
            S["_ws_resumed"] = True
            st.rerun()
        if r2.button("Discard", use_container_width=True, key="ws_discard",
                     help="Deletes the saved session permanently."):
            S["ws_confirm_discard"] = True
        if S.get("ws_confirm_discard"):
            st.warning("Delete this saved session? It holds the responses, the "
                       "self-evaluations and the contact details listed above, and "
                       "this cannot be undone.", icon="🗑️")
            d1, d2 = st.columns(2)
            if d1.button("Yes, delete it", key="ws_discard_yes", type="primary"):
                workspace.clear(vault_now(), _uname)
                S["_ws_saved"] = None
                S["ws_confirm_discard"] = False
                st.rerun()
            if d2.button("Keep it", key="ws_discard_no"):
                S["ws_confirm_discard"] = False
                st.rerun()
    elif S.pop("_ws_resumed", False):
        st.success("Session resumed.", icon="↩")

    _all = survey.list_surveys()
    _mine = survey.visible_surveys(_all, user)
    _labels = [f"{(s['course'] or '(no course)')} · Eval {s['eval_no']}"
               + (f" — {s['owner'] or 'unowned'}" if is_admin else "")
               for s in _mine]
    choice = st.selectbox("Survey", ["➕ New survey…"] + _labels, key="survey_pick")
    if choice == "➕ New survey…":
        course = st.text_input("Course / section", S.get("course", ""), key="new_course")
        eval_no = st.text_input("Evaluation #", S.get("eval_no", "1"), key="new_eval")
    else:
        _s = _mine[_labels.index(choice)]
        course, eval_no = _s["course"], str(_s["eval_no"])
        st.caption(f"Editing **{course} · Eval {eval_no}**"
                   + (f" · owner `{_s['owner'] or 'unowned'}`" if is_admin else ""))
    S["course"], S["eval_no"] = course, eval_no

    # ---- Guard against cross-survey contamination ------------------------
    # Switching the selected survey (course + eval) must not leave another
    # survey's data behind, or a downstream tab could pair one survey's
    # responses with another survey's report/config. When the active survey
    # changes, drop the carried-over data/results and bump a navigation token
    # (used by the scroll-to-top component to remount on the switch).
    _active_slug = survey.slugify(course, eval_no)
    if S.get("_active_slug") not in (None, _active_slug):
        for _k in ("survey_contact_df", "long_df", "self_evals", "roster",
                   "teams", "pdf_zip", "inv_autozip", "inv_emlzip",
                   "rem_autozip", "rem_emlzip", "res_autozip", "res_emlzip",
                   # v2: AI drafts are per-survey. Carrying them across a
                   # survey switch would attach one cohort's narratives to
                   # another cohort's students — the same leak the guard above
                   # exists to prevent, with worse consequences.
                   ai_ui.STATE_KEY, "ai_narratives",
                   # Reset the persistence bookkeeping too, so the new survey's
                   # saved drafts are restored rather than the old survey's
                   # fingerprint suppressing the load.
                   ai_ui.FP_KEY, ai_ui.LOADED_KEY, "ai_reports"):
            S.pop(_k, None)
        S["_nav_token"] = S.get("_nav_token", 0) + 1
    S["_active_slug"] = _active_slug

    # ---- admin: manage instructor accounts -------------------------------
    if is_admin:
        st.divider()
        with st.expander("👥 Manage instructors"):
            accts = accounts.load_accounts()
            st.caption(f"{len(accts)} stored account(s), plus the built-in `admin`.")
            st.markdown("**Add an instructor**")
            nu = st.text_input("Username", key="acct_new_user")
            nn = st.text_input("Display name", key="acct_new_name")
            nr = st.selectbox("Role", ["instructor", "admin"], key="acct_new_role")
            npw = st.text_input("Temporary password", value="PeerParley-Welcome",
                                key="acct_new_pw")
            if st.button("Add instructor", key="acct_add"):
                try:
                    accounts.add_account(nu, nn, nr, npw or "PeerParley-Welcome")
                    st.success(f"Added `{nu}`. Give them username `{nu}` and password "
                               f"`{npw or 'PeerParley-Welcome'}` — they'll set their own "
                               "on first sign-in.")
                except Exception as exc:  # noqa: BLE001
                    st.error(str(exc))
            if accts:
                st.markdown("**Manage an account**")
                who = st.selectbox("Account", list(accts), key="acct_pick")
                a1, a2 = st.columns(2)
                if a1.button("Reset password", key="acct_reset"):
                    accounts.set_password(who, "PeerParley-Welcome", must_change=True)
                    st.success("Reset to `PeerParley-Welcome` (must change on next sign-in).")
                if a2.button("Toggle admin/instructor", key="acct_role"):
                    accounts.set_role(who, "instructor"
                                      if accts[who].get("role") == "admin" else "admin")
                    st.success("Role changed.")
                a3, a4 = st.columns(2)
                if a3.button(("Deactivate" if accts[who].get("active", True) else "Activate"),
                             key="acct_active"):
                    accounts.set_active(who, not accts[who].get("active", True))
                    st.success("Updated.")
                if a4.button("Remove", key="acct_remove"):
                    accounts.remove_account(who)
                    st.success(f"Removed `{who}`.")

    # ---- v2: AI feedback writer (optional) -------------------------------
    # Rendered last so the sidebar still reads top-to-bottom as identity →
    # storage → survey → admin. Returns a disabled settings object when the
    # instructor has not switched it on, so every call site downstream can treat
    # "off" and "not configured" identically.
    ai_settings = ai_ui.sidebar_settings(
        vault=vault_now(), username=str(user.get("user") or "default"))

DEFAULT_INVITE_BODY = (
    "Hi {first_name},<br><br>"
    "It's time for the peer evaluation for {class} (Evaluation {eval_no}). "
    "Please open your personal link below and split your points across your "
    "teammates. It only takes a few minutes, and you can revise until it "
    "closes.<br><br>"
    '<a href="{link}">Open my peer evaluation</a><br><br>'
    "If the button doesn't work, copy this address into your browser:<br>"
    "{link}<br><br>Thanks,<br>The teaching team"
)


GRADING_EXPLAINER = """
Every number a student sees is produced by one of these steps. Defaults are set so
you can grade without changing anything.

**1. Team score** — the grade the whole team starts with (default **100**). Peer
input then nudges each member up or down from this.

**2. Pay grade — the core signal.** Each student splits **$100** across teammates.
A student's *pay grade* is the average dollars they **received** ÷ the **team
average**. So **100% = exactly the team average**. Above 100% means teammates
valued them more than average; below 100% means less. Because it's measured
against the team's own average, self-allocations or blank answers can never push a
whole team into the negative.

**3. Grade adjustment (the ± %).** This is what moves the grade. You choose *which*
peer signal drives it under **Peer-adjustment method** below — the $100 allocation
(default, the WebPA/CATME factor), the four rating statements, the forced ranking,
or a combination. Whichever you pick, it works the same way:

> adjustment ≈ **sensitivity** × (peer factor − 100%)

So a student **above** the team average gets a **bonus (+%)**, one **below** gets a
**deduction (−%)**, and one exactly average gets **0%**. The bonus and the
deduction are capped **separately** — by default a top student can rise at most
**+5%**, while a low student can fall at most **−50%**. Two optional weights can
only *shrink* the adjustment, never flip its sign:

- **Agreement (A)** — if evaluators strongly *disagree* about a student, their
  adjustment is softened (they got mixed signals).
- **Comment support (Q)** — if the written comments about a student don't back up
  the dollars, the adjustment is softened. With no comments, Q is neutral (no
  effect).

**4. Feedback points** — each student also earns points (default up to **5**) for
the **quality of the feedback *they* wrote** about teammates — length, specifics,
and whether it looks copy-pasted. This rewards taking the evaluation seriously.

**5. Dimension letter grades** — the four rating statements (team player, quantity,
quality, effect) each average to a 0–100% score → a letter grade, shown to the
student so they know *where* they're strong or weak.

**6. Performance (High / Adequate / Low)** — from the **forced ranking** when the
survey collected it; otherwise from the pay grade (above/below the team average by
a set band).

**7. Self-evaluation** — the student's own ratings appear next to their peers' on
their feedback sheet, so they can see the gap.
"""


def _mailto(to, subject, body_html):
    """A mailto: URL that opens the computer's default mail app pre-filled.
    Body is converted to plain text (mailto can't carry HTML or attachments)."""
    text = re.sub(r"<br\s*/?>", "\n", body_html or "")
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text).replace("&nbsp;", " ")
    q = urllib.parse.urlencode({"subject": subject or "", "body": text},
                               quote_via=urllib.parse.quote)
    return f"mailto:{to}?{q}"


def _mail_app_links(recipients, subject_t, body_t, course, eval_no, limit=80):
    """Render clickable 'open in your mail app' links for link-only emails."""
    st.caption("Opens your computer's default email app (Outlook, Apple Mail, …) with the "
               "message pre-filled — you just press Send. No attachments this way; for the "
               "results emails with PDFs, use the .eml pack.")
    shown = 0
    for r in recipients:
        if not r.get("email"):
            continue
        ctx = {"first_name": (r.get("name", "").split(" ") or [""])[0],
               "last_name": r.get("name", "").split(" ")[-1], "name": r.get("name", ""),
               "team": r.get("team", ""), "eval_no": eval_no, "class": course,
               "link": r.get("link", "")}
        url = _mailto(r["email"], mail.render_template(subject_t, ctx),
                      mail.render_template(body_t, ctx))
        st.markdown(f"- [✉️ {r['name']} &lt;{r['email']}&gt;]({url})")
        shown += 1
        if shown >= limit:
            st.caption(f"Showing the first {limit}.")
            break
    if shown == 0:
        st.info("No recipients with an email address.")


_LINK_METHODS = ["Microsoft 365 — send now", "SMTP — send now",
                 "Open in my mail app (mailto)", "Download .eml files",
                 "Download auto-send scripts (Mac / Windows)", "Download links CSV"]
_RESULT_METHODS = ["Microsoft 365 — send now", "SMTP — send now",
                   "Download .eml files", "Download auto-send scripts (Mac / Windows)",
                   "Download recipients CSV"]


def _api_and_drafts(method_label, key):
    """Pick the transport, and offer the safety net on both of them.

    Microsoft 365 defaulted to writing drafts for review; SMTP had no
    equivalent, so one click there sent forty real emails with no way to look
    first. SMTP cannot make drafts in a mailbox it does not own, so ticking the
    box hands back a .eml pack instead — same promise ("nothing has gone out
    yet"), different mechanism.
    """
    api = "graph" if method_label.startswith("Microsoft") else "smtp"
    drafts = st.checkbox(
        "Prepare them for me to look over first — don't send yet",
        value=True, key=f"{key}_d",
        help=("Creates them as drafts in your Outlook, ready to send."
              if api == "graph" else
              "Builds a zip of ready-to-send email files you can open and check "
              "before sending, because SMTP has no drafts folder."))
    return api, drafts


def _link_delivery(key, recipients, subj, body, course, eval_no, base_ok, label):
    """One dropdown for how to send an invitation/reminder (link-only) email."""
    if not base_ok:
        # Four of the six paths used to skip this check entirely, so the links
        # went out as bare "?t=..." query strings with no host in front of them.
        st.warning("Paste this app's web address above before sending anything — "
                   "without it, the links in the email go nowhere.", icon="🔗")
        return
    m = st.selectbox("How do you want to send?", _LINK_METHODS, key=f"{key}_m")
    if m.startswith(("Microsoft", "SMTP")):
        api, drafts = _api_and_drafts(m, key)
        _n = sum(1 for r in recipients if r.get("email"))
        if st.button(("Prepare " if drafts else "Send ") + f"{_n} email(s) to students",
                     type="primary", key=f"{key}_go"):
            if not base_ok:
                st.error("Enter this app's web address above first — the links need "
                         "a destination.")
            else:
                msgs = []
                for r in recipients:
                    if not r.get("email"):
                        continue
                    ctx = {"first_name": r["name"].split(" ")[0], "last_name": r["name"].split(" ")[-1],
                           "name": r["name"], "team": r["team"], "eval_no": eval_no,
                           "class": course, "link": r.get("link", "")}
                    msgs.append(mail.Message(
                        to_email=r["email"], to_name=r["name"], team=r["team"],
                        subject=mail.render_template(subj, ctx),
                        body=mail.render_template(body, ctx), attachments=[]))
                _deliver(msgs, api, drafts, ok_label=f"{label} delivered")
    elif m.startswith("Open in my mail app"):
        _mail_app_links(recipients, subj, body, course, eval_no)
    elif m.startswith("Download .eml"):
        if st.button("Build .eml files", key=f"{key}_eml"):
            st.session_state[f"{key}_emlzip"] = emailpack.zip_folders(
                {label: emailpack.invite_items(recipients, subj, body, course, eval_no)})
        if st.session_state.get(f"{key}_emlzip"):
            st.download_button("⬇ Download .eml zip", st.session_state[f"{key}_emlzip"],
                               f"{label.lower()}_eml.zip", "application/zip")
    elif m.startswith("Download auto-send"):
        if st.button("Build auto-send scripts", key=f"{key}_auto"):
            st.session_state[f"{key}_autozip"] = emailpack.send_all_pack(
                emailpack.invite_parts(recipients, subj, body, course, eval_no), label)
        if st.session_state.get(f"{key}_autozip"):
            st.download_button("⬇ Download auto-send zip", st.session_state[f"{key}_autozip"],
                               f"{label.lower()}_autosend.zip", "application/zip")
            st.caption("Unzip, then just **double-click** — `Send emails (Windows).cmd` on "
                       "Windows, or `send_all_mac.applescript` on Mac. Sends from your own "
                       "Outlook / Apple Mail.")
    else:  # links CSV
        df = pd.DataFrame([{"Team": r["team"], "Name": r["name"],
                            "Email": r.get("email", ""), "Link": r.get("link", "")}
                           for r in recipients])
        st.download_button("⬇ Download links CSV", df.to_csv(index=False),
                           f"{label.lower()}_links.csv", "text/csv")


def _results_delivery(key, teams, roster, subj, body, attach_team, course, eval_no,
                      report, narratives=None, reports=None):
    """One dropdown for how to send the feedback (results, with PDF attachments).

    `narratives` carries the approved AI summaries (v2) into every delivery path,
    so switching send method cannot change what a student receives.
    """
    narratives = narratives or {}
    reports = reports or {}
    m = st.selectbox("How do you want to send the feedback?", _RESULT_METHODS, key=f"{key}_m")
    if m.startswith(("Microsoft", "SMTP")):
        api, drafts = _api_and_drafts(m, key)
        _n = sum(1 for tm in teams for _m in tm.members
                 if (roster.by_key.get(_m.key, {}) or {}).get("email"))
        if st.button(("Prepare " if drafts else "Send ")
                     + f"{_n} feedback report(s) to students",
                     type="primary", key=f"{key}_go"):
            msgs = _build_messages(teams, roster, subj, body, attach_team, course, eval_no,
                                   report=report, narratives=narratives,
                                   reports=reports)
            _deliver(msgs, api, drafts, ok_label="Feedback delivered")
    elif m.startswith("Download .eml"):
        if st.button("Build .eml files", key=f"{key}_eml"):
            st.session_state[f"{key}_emlzip"] = emailpack.zip_folders({"Results":
                emailpack.results_items(teams, roster, subj, body, attach_team,
                                        course, eval_no, report=report,
                                        narratives=narratives,
                                        reports=reports)})
        if st.session_state.get(f"{key}_emlzip"):
            st.download_button("⬇ Download .eml zip", st.session_state[f"{key}_emlzip"],
                               "results_eml.zip", "application/zip")
    elif m.startswith("Download auto-send"):
        if st.button("Build auto-send scripts", key=f"{key}_auto"):
            st.session_state[f"{key}_autozip"] = emailpack.send_all_pack(
                emailpack.results_parts(teams, roster, subj, body, attach_team,
                                        course, eval_no, report=report,
                                        narratives=narratives,
                                        reports=reports), "Results")
        if st.session_state.get(f"{key}_autozip"):
            st.download_button("⬇ Download auto-send zip", st.session_state[f"{key}_autozip"],
                               "results_autosend.zip", "application/zip")
            st.caption("Unzip, then just **double-click** — `Send emails (Windows).cmd` on "
                       "Windows, or `send_all_mac.applescript` on Mac. Each student's PDF is "
                       "attached automatically; keep the files together in the unzipped folder.")
    else:  # recipients CSV
        rows = []
        for t in teams:
            for mm in t.members:
                cx = _ctx(mm, course, eval_no)
                rec = roster.match(mm.name) if roster else None
                rows.append({"Team": t.team, "Name": mm.name, "Email": (rec or {}).get("email", ""),
                             "Subject": mail.render_template(subj, cx),
                             "Body": mail.render_template(body, cx)})
        st.download_button("⬇ Download recipients CSV", pd.DataFrame(rows).to_csv(index=False),
                           "results_recipients.csv", "text/csv")


def _email_body(default_html, key, height=220):
    """WYSIWYG email-body editor (Quill) that returns HTML. Falls back to a plain
    HTML text box if the streamlit-quill component isn't available."""
    st.caption("Formatting toolbar below. Placeholders like {first_name}, {team}, "
               "{eval_no}, {class} and {link} are filled in per student when sent.")
    try:
        from streamlit_quill import st_quill
        html = st_quill(value=st.session_state.get(f"{key}_html", default_html),
                        html=True, key=key)
        html = html if html else default_html
        st.session_state[f"{key}_html"] = html
        return html
    except Exception:
        return st.text_area("Body (HTML)", default_html, height=height, key=f"{key}_ta")


def _mailer_for(method):
    """Build a mailer for the chosen method, overriding the secrets default."""
    c = dataclasses.replace(cfg, email=dataclasses.replace(cfg.email, mode=method))
    return _make_mailer(c)


def _deliver(messages, method, drafts_only, ok_label="Done"):
    """Shared send routine for invite/reminder emails (reuses the app mailer)."""
    if not messages:
        st.warning("No recipients with an email address.")
        return
    st.write(f"Prepared {len(messages)} message(s).")
    if drafts_only and method != "graph":
        # SMTP cannot put a draft in the instructor's mailbox, and batch_send
        # silently *sent* when asked to draft over SMTP — so the checkbox used
        # to promise a review step it did not provide. A zip of .eml files is
        # the version of that promise SMTP can keep.
        items = emailpack.message_items(messages)
        st.download_button(
            f"⬇ Download {len(items)} ready-to-send email(s)",
            emailpack.zip_folders({ok_label: items}),
            f"{ok_label.lower().replace(' ', '_')}_to_review.zip", "application/zip",
            type="primary",
        )
        st.success(f"Nothing has been sent. {len(items)} email(s) are in the zip — "
                   "open one to check it, then send them from your own mail "
                   "program. Untick the box above to send directly instead.")
        return
    mailer = _mailer_for(method)
    if mailer is None:
        return
    prog = st.progress(0.0)
    log = st.empty()
    lines = []

    def _cb(i, total, status):
        prog.progress(i / total)
        lines.append(status)
        log.code("\n".join(lines[-12:]))

    result = mail.batch_send(messages, mailer, drafts_only=drafts_only, progress=_cb)
    st.success(f"{ok_label} — sent {result['sent']}, drafted {result['drafted']}, "
               f"failed {len(result['failed'])} of {result['total']}.")
    if result["failed"]:
        st.dataframe(pd.DataFrame(result["failed"]))


def _scroll_to_top():
    """Send the viewport to the top on navigation.

    Two triggers are covered. (1) Server-side navigation (e.g. switching the
    selected survey) bumps `_nav_token`; embedding that token makes the injected
    HTML unique each time, so Streamlit remounts this iframe and re-runs the
    scroll once. (2) Tab switches are purely client-side (no rerun), so a single
    delegated click listener on the tab bar scrolls to top when a tab changes.
    All DOM work is best-effort inside the component iframe and can never raise
    into the Python app.
    """
    token = S.get("_nav_token", 0)
    components.html(
        f"""
        <script>
        /* nav-token:{token} — a changed token forces a remount + re-scroll */
        (function() {{
          var doc = window.parent.document;
          function toTop() {{
            try {{
              window.parent.scrollTo(0, 0);
              var sels = ['section.main', '[data-testid="stMain"]',
                          '[data-testid="stAppViewContainer"]'];
              for (var i = 0; i < sels.length; i++) {{
                var el = doc.querySelector(sels[i]);
                if (el) el.scrollTo(0, 0);
              }}
            }} catch (e) {{}}
          }}
          toTop();
          if (!window.parent.__ppTabScrollHooked) {{
            window.parent.__ppTabScrollHooked = true;
            doc.addEventListener('click', function(e) {{
              if (e.target.closest('button[role="tab"]')) setTimeout(toTop, 40);
            }}, true);
          }}
        }})();
        </script>
        """,
        height=0,
    )


_scroll_to_top()

# ---- Start here: the four steps, ticked off from what is actually true ----
# A prose route map told a first-time instructor the order but not where they
# were in it, and nothing in the app said which step was unfinished. Each line
# below is driven by real state and names the one thing that completes it, so
# the sequence is visible without reading anything.
def _start_here(course: str, eval_no: str) -> None:
    slug_now = survey.slugify(course, eval_no)
    try:
        snap = survey.load_roster_snapshot(vault_now(), slug_now)
    except Exception:  # noqa: BLE001
        snap = None
    have_list = S.get("survey_contact_df") is not None or bool(snap)
    saved = bool(snap)
    have_url = bool((S.get("inv_url") or getattr(cfg, "public_url", "") or "").strip())
    try:
        responses = len(survey.all_responses(vault_now(), slug_now)) if saved else 0
    except Exception:  # noqa: BLE001
        responses = 0
    loaded = "long_df" in S

    steps = [
        (have_list, "Upload your contact list",
         "tab ① — names, emails and teams, one row per student"),
        (saved, "Save the survey",
         "tab ① — the button under the questions; this is what makes the "
         "student links work"),
        (have_url, "Enter this app's web address",
         "tab ① — copy it from your browser's address bar, then email the links"),
        (responses > 0, f"Collect responses ({responses} in so far)"
         if saved else "Collect responses",
         "tab ② — watch who has answered and remind the rest"),
        (loaded, "Grade and send the reports",
         "tab ② to load the answers, then ④ for grades and ⑤ to send feedback"),
    ]
    done = sum(1 for ok, *_ in steps if ok)
    with st.expander(f"**Start here — step {min(done + 1, len(steps))} of "
                     f"{len(steps)}**", expanded=done < len(steps)):
        for ok, label, where in steps:
            st.markdown(f"{'✅' if ok else '⬜️'} **{label}** — {where}"
                        if not ok else f"✅ **{label}**")
        st.caption("*Compare* and *Vault* are optional. So is the AI feedback "
                   "writer in the sidebar: it turns each student's peer comments "
                   "into a short summary that you read and approve before anyone "
                   "sees it. It stays off until you switch it on.")


_start_here(course, eval_no)

tabs = st.tabs([
    "① Set up survey", "② Collect responses", "③ Grading rules",
    "④ Results & reports", "⑤ Send feedback", "📈 Compare", "🔒 Vault",
])

# =========================================================================== #
# TAB 1 — Survey setup (upload contact list, configure, send links)
# =========================================================================== #
with tabs[0]:
    st.subheader("Set up & send the survey")
    st.caption("Step 1 of 5. Upload the **contact list** (names, emails, teams) — the only "
               "input needed. PeerParley builds a personal evaluation link for every "
               "student; their answers become the grading input directly. When you've "
               "sent the links, move to **② Collect responses**.")
    slug = survey.slugify(course, eval_no)
    st.caption(f"Everything on this page is filed under **{course or '(no course yet)'} "
               f"· Evaluation {eval_no}**. Change either one in the sidebar to work on a "
               f"different section or round.")

    # ---- Downloadable starter template --------------------------------------
    _tpl_path = os.path.join(_ASSET_DIR, "ContactList_Template.xlsx")
    if os.path.exists(_tpl_path):
        with open(_tpl_path, "rb") as _f:
            _tpl_bytes = _f.read()
        c_tpl, c_note = st.columns([1, 2])
        with c_tpl:
            st.download_button(
                "⬇️ Download contact list template",
                _tpl_bytes,
                file_name="ContactList_Template.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_contact_template",
            )
        with c_note:
            st.caption("🟡 **Fill in the yellow cells only** — first name, last name, "
                       "email, class, section, and team. The green header and the grey "
                       "columns fill in automatically; don't edit them. Then upload the "
                       "saved file below.")
    else:
        st.caption("🟡 Tip: your contact list needs first name, last name, email, class, "
                   "section, and team for every student.")

    contact_file = st.file_uploader("Contact list (CSV/XLSX)",
                                    type=["csv", "xlsx", "xls"], key="survey_roster")
    if contact_file is not None:
        S["survey_contact_df"] = ingest.read_table(contact_file, contact_file.name)
    cdf = S.get("survey_contact_df")

    if cdf is not None:
        teams_preview = survey.build_teams(cdf)
        nstud = sum(len(v) for v in teams_preview.values())
        st.success(f"{len(teams_preview)} team(s), {nstud} student(s) with teammates.")
        with st.expander("Teams preview"):
            for t, members in teams_preview.items():
                st.write(f"**Team {t}** — " + ", ".join(m["name"] for m in members))
        no_email = [m["name"] for members in teams_preview.values()
                    for m in members if not m.get("email")]
        if no_email:
            st.warning(f"{len(no_email)} student(s) have no email and can't be sent a "
                       "link: " + ", ".join(no_email[:8]) + ("…" if len(no_email) > 8 else ""))

    # ---- Already collected in Qualtrics? Upload the raw export instead -------
    with st.expander("Already have responses? Upload a Qualtrics / raw export - Optional - RARELY USED"):
        st.caption("Use this if you ran the evaluation in Qualtrics instead of the built-in "
                   "survey. Upload the raw export (CSV or XLSX) — PeerParley reads the "
                   "standard peer-evaluation layout (ratings, ranking, $ allocation, "
                   "comments) and grades it exactly like collected responses. No column "
                   "mapping needed.")
        qfile = st.file_uploader("Qualtrics raw export (CSV/XLSX)",
                                 type=["csv", "xlsx", "xls"], key="qualtrics_upload")
        # Streamlit hands this uploader's file back on every rerun, so without a
        # guard the export was re-parsed and re-installed on every click —
        # silently overwriting responses loaded on tab ② or a bundle opened from
        # the vault, and then autosaving the clobbered copy. Parse once per file.
        _qsig = (f"{getattr(qfile, 'name', '')}:{getattr(qfile, 'size', 0)}"
                 if qfile is not None else "")
        if qfile is not None and S.get("qualtrics_sig") != _qsig:
            try:
                q_long, q_self, q_roster, q_rep = ingest.parse_qualtrics_export(
                    qfile, qfile.name)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Couldn't read that export: {exc}")
                q_long = None
            if q_long is not None and not q_long.empty:
                S["qualtrics_sig"] = _qsig
                S["qualtrics_report"] = q_rep
                S["long_df"] = q_long
                S["self_evals"] = q_self
                S["roster"] = q_roster
                m1, m2, m3 = st.columns(3)
                m1.metric("Evaluation rows", q_rep["rows"])
                m2.metric("Respondents", q_rep["evaluators"])
                m3.metric("Teams", q_rep["teams"])
                st.success("Parsed the export. Open **④ Results & reports** to see the "
                           "grades, then **⑤ Send feedback**.")
                with st.expander("Preview parsed rows"):
                    st.dataframe(q_long.head(30), use_container_width=True)
            elif q_long is not None:
                st.warning("No evaluation rows found in that file. Use the file "
                           "Qualtrics gives you under Export → CSV, with its "
                           "original first row of question codes still in place.")
        elif qfile is not None and S.get("qualtrics_report"):
            _r = S["qualtrics_report"]
            m1, m2, m3 = st.columns(3)
            m1.metric("Evaluation rows", _r["rows"])
            m2.metric("Respondents", _r["evaluators"])
            m3.metric("Teams", _r["teams"])
            st.caption("Already loaded. Open **④ Results & reports** for the grades. "
                       "To load a different file, remove this one first.")

    cur = {**survey.DEFAULT_SURVEY, **(S.get("survey_cfg") or {})}

    with st.expander("Questions — turn each on or off"):
        st.caption("Modelled on the MGT 301 peer evaluation. Switch any block off and "
                   "students won't see it.")
        cur["ask_ratings"] = st.checkbox("Rating matrix — 4 statements, 7-point agree/disagree",
                                         value=cur["ask_ratings"])
        cur["ask_improve"] = st.checkbox("Qualitative — how to increase their contribution",
                                         value=cur["ask_improve"])
        cur["ask_contribution"] = st.checkbox("Qualitative — their most significant contributions",
                                              value=cur["ask_contribution"])
        cur["ask_ranking"] = st.checkbox("Forced ranking — High / Adequate / Low performer",
                                         value=cur["ask_ranking"])
        cur["ask_allocation"] = st.checkbox(
            "Pay allocation — split $100 among teammates (not themselves)",
            value=cur["ask_allocation"])
        cur["ask_self_contribution"] = st.checkbox("Your own contribution — free text",
                                                   value=cur["ask_self_contribution"])
        cur["ask_confidential"] = st.checkbox("Confidential note to the instructor",
                                              value=cur["ask_confidential"])
        cur["show_header"] = st.checkbox("Header — name, class, section, team-member list",
                                         value=cur["show_header"])
        if not cur["ask_allocation"]:
            st.warning("With pay allocation off, the $ peer-ratio can't be computed — grade "
                       "adjustments will be flat. Leave it on unless you have a reason.")
        if not (cur["ask_improve"] or cur["ask_contribution"]):
            st.warning("With both written questions off, students write nothing, so "
                       "there is no comment quality to measure — written feedback "
                       "will not affect anyone's grade.")

    with st.expander("Wording"):
        cur["title"] = st.text_input("Title", cur["title"])
        cur["intro"] = st.text_area("Intro (markdown)", cur["intro"], height=80)
        if cur["ask_ratings"]:
            cur["ratings_prompt"] = st.text_input("Rating-matrix prompt", cur["ratings_prompt"])
            _sts = list(cur.get("rating_statements") or survey.RATING_STATEMENTS)
            _sts += [""] * (4 - len(_sts))
            for _k in range(4):
                _sts[_k] = st.text_input(f"Statement {_k + 1}", _sts[_k], key=f"stmt_{_k}")
            cur["rating_statements"] = [s for s in _sts if s.strip()]
        if cur["ask_improve"]:
            cur["improve_prompt"] = st.text_input(
                "‘Increase contribution’ prompt (use {member} for the name)", cur["improve_prompt"])
        if cur["ask_contribution"]:
            cur["contribution_prompt"] = st.text_input(
                "‘Most significant contributions’ prompt (use {member})", cur["contribution_prompt"])
        if cur["ask_ranking"]:
            cur["ranking_prompt"] = st.text_input("Forced-ranking prompt", cur["ranking_prompt"])
        if cur["ask_allocation"]:
            cur["allocation_prompt"] = st.text_input("Pay-allocation prompt", cur["allocation_prompt"])
            cur["points_total"] = int(st.number_input("Dollars to allocate", 10, 1000,
                                                       int(cur["points_total"]), 10))
        if cur["ask_self_contribution"]:
            cur["self_contribution_prompt"] = st.text_input("Your-contribution prompt",
                                                           cur["self_contribution_prompt"])
        if cur["ask_confidential"]:
            cur["confidential_prompt"] = st.text_input("Confidential-note prompt",
                                                       cur["confidential_prompt"])

    cur.setdefault("report", dict(getattr(survey, "REPORT_DEFAULTS", {
        "performance": True, "grade_adjustment": True, "dimensions": True,
        "pay_grade": True, "valued": True, "focus": True, "response_quality": True})))
    with st.expander("What students see in their feedback report"):
        st.caption("Choose which sections appear on each student's feedback PDF. "
                   "Grading is unaffected — this only changes what's shown to students.")
        rp = dict(cur["report"])
        rp["performance"] = st.checkbox("Performance label (High / Adequate / Low)",
                                        value=rp.get("performance", True))
        rp["grade_adjustment"] = st.checkbox("Grade-adjustment meter (± vs team score)",
                                             value=rp.get("grade_adjustment", True))
        rp["dimensions"] = st.checkbox("Rating meters + dimension letter grades",
                                       value=rp.get("dimensions", True))
        rp["pay_grade"] = st.checkbox("Pay grade", value=rp.get("pay_grade", True))
        rp["response_quality"] = st.checkbox("“The feedback you gave” (your Q + points)",
                                             value=rp.get("response_quality", True))

        # ---- written feedback: narrative, raw comments, or both ----------
        st.markdown("###### Written feedback")
        st.caption("What each student receives in words. The AI narrative only "
                   "appears for students whose draft you approved on the "
                   "④ Results tab.")
        _WF_BOTH = "Summary **and** their teammates' comments"
        _WF_SUMMARY = "Summary only"
        _WF_RAW = "Their teammates' comments only"
        _WF_NONE = "No written feedback"

        def _written_mode(flags):
            narr = flags.get("narrative", True)
            raw = flags.get("valued", True) or flags.get("focus", True)
            if narr and raw:
                return _WF_BOTH
            if narr:
                return _WF_SUMMARY
            if raw:
                return _WF_RAW
            return _WF_NONE

        _wf = st.radio(
            "Written feedback", [_WF_BOTH, _WF_SUMMARY, _WF_RAW, _WF_NONE],
            index=[_WF_BOTH, _WF_SUMMARY, _WF_RAW, _WF_NONE].index(
                _written_mode(rp)),
            label_visibility="collapsed", key="report_written_mode")

        if _wf == _WF_BOTH:
            rp["narrative"], rp["valued"], rp["focus"] = True, True, True
            st.caption("The summary sits above the bullets it was written from, "
                       "so a student can check it against what was actually said.")
        elif _wf == _WF_SUMMARY:
            rp["narrative"], rp["valued"], rp["focus"] = True, False, False
            st.caption(
                "Cleaner to read, and it spares students the blunter phrasings. "
                "Worth knowing the trade: they cannot check the summary against "
                "the source, so it rests entirely on your review of it — and a "
                "student who received no approved draft gets no written "
                "feedback at all.")
        elif _wf == _WF_RAW:
            rp["narrative"], rp["valued"], rp["focus"] = False, True, True
            st.caption("Their teammates' own words, unedited. This is what "
                       "PeerParley sent before the AI writer existed.")
        else:
            rp["narrative"], rp["valued"], rp["focus"] = False, False, False
            st.caption("Numbers only — no comments and no summary.")

        with st.expander("Fine-grained control"):
            rp["valued"] = st.checkbox(
                "“What your teammates valued” (contributions)",
                value=rp.get("valued", True), key="report_valued")
            rp["focus"] = st.checkbox(
                "“Where to focus next” (improvements)",
                value=rp.get("focus", True), key="report_focus")
            rp["narrative"] = st.checkbox(
                "“Summary of your peer feedback” (AI narrative, when approved)",
                value=rp.get("narrative", True), key="report_narrative")

        cur["report"] = rp

    cur["is_open"] = st.toggle("Accept submissions (master switch)", value=cur["is_open"],
                               help="Turn off to close the survey immediately, regardless "
                                    "of the dates below.")

    with st.expander("Schedule — open / close dates (optional)"):
        st.caption(f"App server clock right now: **{dt.datetime.now():%b %d, %Y %I:%M %p}**. "
                   "Dates below use this clock — set them relative to it.")
        use_open = st.checkbox("Set an OPEN date", value=bool(cur.get("opens_at")))
        if use_open:
            _o = survey.parse_dt(cur.get("opens_at")) or dt.datetime.now()
            co1, co2 = st.columns(2)
            od = co1.date_input("Opens on", _o.date(), key="open_d")
            ot = co2.time_input("Opens at", _o.time().replace(microsecond=0), key="open_t")
            cur["opens_at"] = dt.datetime.combine(od, ot).isoformat()
        else:
            cur["opens_at"] = ""
        use_close = st.checkbox("Set a CLOSE date", value=bool(cur.get("closes_at")))
        if use_close:
            _c = survey.parse_dt(cur.get("closes_at")) or (dt.datetime.now() + dt.timedelta(days=7))
            cc1, cc2 = st.columns(2)
            cd = cc1.date_input("Closes on", _c.date(), key="close_d")
            ct = cc2.time_input("Closes at", _c.time().replace(microsecond=0), key="close_t")
            cur["closes_at"] = dt.datetime.combine(cd, ct).isoformat()
        else:
            cur["closes_at"] = ""
    S["survey_cfg"] = cur

    _state = survey.window_state(cur)
    _badge = {"open": "🟢 Open", "not_yet": "🟡 Scheduled — not open yet",
              "closed": "🔴 Closed (past the close date)",
              "disabled": "🔴 Closed (master switch off)"}[_state]
    st.caption(f"Current status for students: **{_badge}** · {survey.window_message(cur)}")

    _own = survey.survey_owner(vault_now(), slug)
    if _own not in (None, "") and is_admin is False and not survey.can_access(_own, user):
        st.warning(f"A survey with this course/eval already exists and belongs to "
                   f"`{_own}`. Choose a different course or evaluation number.")
    # Is this survey stored, and does it match what is on screen? Tabs ④ and ⑤
    # read the saved copy, and students open the saved copy, so "saved" is the
    # difference between settings that apply and settings that only look set.
    _saved_snap = survey.load_roster_snapshot(vault_now(), slug)
    _is_saved = _saved_snap is not None
    _shift = (survey.roster_shift(vault_now(), slug, cdf)
              if (cdf is not None and _is_saved) else
              {"teams": [], "responses": 0, "safe": True})

    if cdf is None and not _is_saved:
        st.info("Upload a contact list above, then save — that is what makes the "
                "student links work.", icon="⬆️")
    if not _shift["safe"]:
        st.error(
            f"**Saving now would attach answers to the wrong students.** "
            f"{_shift['responses']} submission(s) are already in for "
            f"{'team' if len(_shift['teams']) == 1 else 'teams'} "
            f"{', '.join(_shift['teams'])}, and this contact list changes who is "
            f"on them or the order they are listed in. Each student's link points "
            f"at their place in the saved list, so re-saving would hand their "
            f"answers to a teammate. Use a new evaluation number for a changed "
            f"roster, or put the list back the way it was.",
            icon="🛑",
        )
    if st.button("💾 Save — and make the student links live", type="primary",
                 disabled=(cdf is None or not _shift["safe"]),
                 help="Writes the questions, the schedule, the report settings and "
                      "the team list to your storage. Until you do this, the links "
                      "do not open for students and tabs ④ and ⑤ use the last "
                      "saved version."):
        if _own not in (None, "") and not survey.can_access(_own, user):
            st.error(f"Can't save — this course/eval belongs to `{_own}`.")
        else:
            try:
                stamp = user["user"] if _own in (None, "") else None
                sl, teams = survey.save_setup(course, eval_no, cdf, cur, owner=stamp)
                nstud = sum(len(v) for v in teams.values())
                st.success(f"Saved: {len(teams)} team(s), {nstud} student(s). The "
                           f"student links are now live."
                           + ("" if cfg.vault.backend != "local" else
                              " ⚠️ Storage is set to 'local', which means this "
                              "server's own disk — fine for trying things out, but "
                              "it can be wiped at any time. Use your university "
                              "storage before collecting real responses."))
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not save: {exc}")

    st.divider()
    st.markdown("##### Send personal links")
    base_url = st.text_input(
        "This app's web address", getattr(cfg, "public_url", "") or "",
        key="inv_url",
        placeholder="https://your-peerparley-address",
        help="Copy it from your browser's address bar while using this app. Each "
             "student's link is this address plus a private code that opens only "
             "their own form.")
    _base_ok = bool(base_url.strip())
    # Two ways to email forty students a link that cannot work, both of which
    # used to pass without a word: sending before the survey is saved (the link
    # opens "this evaluation isn't available"), and sending with no address (the
    # link is a bare query string that points nowhere). Neither reports an error
    # to the instructor, and both are only discovered by a student.
    if not _is_saved:
        st.warning("Save the survey first (the button above). Until it is saved, "
                   "a student opening their link is told the evaluation is not "
                   "available.", icon="🛑")
    if not _base_ok:
        st.warning("Paste this app's web address above. Without it the links have "
                   "no destination.", icon="🔗")
    if cdf is not None and _is_saved and _base_ok:
        links = survey.student_links(base_url, slug, survey.build_teams(cdf),
                                     survey.token_secret(cfg))
        with st.expander(f"Preview {len(links)} links"):
            st.dataframe(pd.DataFrame(links), use_container_width=True, height=280)
        subj = st.text_input("Email subject",
                             "Your peer evaluation for {class} (Eval {eval_no})", key="inv_subj")
        body = _email_body(DEFAULT_INVITE_BODY, key="inv_body")
        _link_delivery("inv", links, subj, body, course, eval_no, bool(base_url.strip()),
                       "Invitations")

# =========================================================================== #
# TAB 2 — Responses (monitor + load into grading)
# =========================================================================== #
with tabs[1]:
    st.subheader("Collect responses")
    st.caption("Step 2 of 5. Watch who has responded, send reminders to those who "
               "haven't, and when you're ready click **Load responses into grading** at "
               "the bottom — then go to **④ Results & reports**.")
    slug = survey.slugify(course, eval_no)
    vault = vault_now()
    _owner = survey.survey_owner(vault, slug)
    _blocked = _owner is not None and not survey.can_access(_owner, user)
    status = [] if _blocked else _response_status(slug)
    if not _blocked:
        if st.button("↻ Check for new responses", key="resp_refresh"):
            _response_status.clear()
            st.rerun()
    if _blocked:
        st.error(f"This section belongs to another instructor (`{_owner or 'unowned'}`). "
                 "Only its owner or an administrator can see its responses.")
    elif not status:
        st.info("No saved survey for this course/eval yet — set one up in step 1.")
    else:
        got = sum(1 for r in status if r["responded"])
        m1, m2 = st.columns(2)
        m1.metric("Responded", f"{got} / {len(status)}")
        m2.metric("Outstanding", len(status) - got)

        _svy = survey.load_survey(vault, slug)
        _st = survey.window_state(_svy)
        _badge = {"open": "🟢 Open", "not_yet": "🟡 Not open yet",
                  "closed": "🔴 Closed", "disabled": "🔴 Closed (switch off)"}[_st]
        st.caption(f"Survey status: **{_badge}** · {survey.window_message(_svy)}")

        sdf = pd.DataFrame(status)
        by_team = (sdf.groupby("team")["responded"]
                   .agg(["sum", "count"]).reset_index()
                   .rename(columns={"sum": "responded", "count": "students"}))
        st.dataframe(by_team, use_container_width=True)
        with st.expander("Per-student status"):
            st.dataframe(sdf, use_container_width=True, height=320)

        nonresp = [r for r in status if not r["responded"]]
        if nonresp:
            st.download_button("⬇ non-responders.csv",
                               pd.DataFrame(nonresp).to_csv(index=False),
                               f"{slug}_nonresponders.csv", "text/csv")
            with st.expander("Send a reminder to non-responders"):
                # Reuse the URL entered on the ① Set up tab (single source of truth);
                # fall back to the configured public_url secret.
                _url_default = (st.session_state.get("inv_url")
                                or getattr(cfg, "public_url", "") or "")
                base_url = st.text_input("Public app URL", _url_default, key="rem_url")
                subj = st.text_input("Subject",
                                     "Reminder: your peer evaluation for {class}", key="rem_subj")
                body = _email_body(DEFAULT_INVITE_BODY, key="rem_body", height=150)
                from peerparley.tokens import make_token
                _secret = survey.token_secret(cfg)
                _recips = []
                for r in nonresp:
                    if not r["email"]:
                        continue
                    _tok = make_token({"s": slug, "t": r["team"], "p": r["pos"]}, _secret)
                    _sep = "&" if "?" in base_url else "?"
                    _recips.append({"name": r["name"], "email": r["email"], "team": r["team"],
                                    "link": f"{base_url}{_sep}t={_tok}" if base_url else f"?t={_tok}"})
                _link_delivery("rem", _recips, subj, body, course, eval_no,
                               bool(base_url.strip()), "Reminders")

        st.divider()
        st.markdown("##### Grade the collected responses")
        st.caption("Pulls every submission, turns it into the grading input, and hands "
                   "it to the Review / PDF / Email tabs — no Qualtrics upload needed.")
        if st.button("⬇ Load responses into grading", type="primary"):
            long_df = survey.responses_to_long(vault, slug)
            if long_df.empty:
                st.warning("No responses collected yet.")
            else:
                S["long_df"] = long_df
                S["self_evals"] = survey.self_evaluations(vault, slug)
                snap = survey.load_roster_snapshot(vault, slug)
                S["roster"] = survey.roster_for_matching(snap)
                # Remembered so a later submission does not go unnoticed:
                # loading is a snapshot, and nothing used to say when the
                # snapshot had fallen behind.
                S["loaded_submissions"] = got
                S["loaded_slug"] = slug
                st.success(f"Loaded {len(long_df)} evaluation rows from {got} submission(s). "
                           "Open **④ Results & reports** to grade, then **⑤ Send feedback**.")
        elif (S.get("loaded_slug") == slug
              and got > int(S.get("loaded_submissions") or 0)):
            _new = got - int(S.get("loaded_submissions") or 0)
            st.info(f"**{_new} new submission(s)** have arrived since you loaded "
                    f"these responses. The grades on tab ④ do not include them yet "
                    f"— click the button above again to bring them in.", icon="🆕")

# =========================================================================== #
# TAB 3 — Configure grading
# =========================================================================== #
with tabs[2]:
    st.subheader("Grading settings")
    st.caption("Step 3 of 5. The defaults are sensible — you can skip straight to "
               "**Results** without changing anything. The panel below explains exactly "
               "how each grade is produced.")

    with st.expander("📖 How grading works (plain English)", expanded=True):
        st.markdown(GRADING_EXPLAINER)

    # Every control below starts from the settings in play — which, after a
    # resumed session, are the ones that were saved with it rather than the
    # factory defaults.
    _gs0 = S.get("settings") or GradeSettings()
    if S.pop("_settings_restored", False):
        st.info("These are the grading rules saved with the session you resumed.",
                icon="↩")

    st.markdown("##### Peer-adjustment method")
    _sources = ["allocation", "rating", "ranking", "combined"]
    adj_source = st.selectbox(
        "How to measure each student's peer contribution",
        _sources,
        index=_sources.index(_gs0.adjustment_source)
        if _gs0.adjustment_source in _sources else 0,
        format_func=lambda x: {
            "allocation": "Points shared — the $100 allocation (WebPA / CATME family) · default",
            "rating": "Average of the four rating statements (CATME-style)",
            "ranking": "Forced ranking tiers — High / Adequate / Low",
            "combined": "Combined — average of the points and rating measures",
        }[x],
        help="Each one compares a student with their own team's average, so 100% "
             "means exactly average and the adjustment moves up or down from there. "
             "The explainer above and GRADING.md cover the research behind each.")

    st.markdown("##### Main settings")
    c1, c2 = st.columns(2)
    with c1:
        B = st.slider("Sensitivity (how strongly peers move the grade)",
                      0.0, 1.5, float(_gs0.sensitivity_B), 0.05,
                      help="0 means peers do not move the grade at all; 1 means the "
                           "full effect of their ratings. 0.5 is the usual starting "
                           "point.")
    with c2:
        maxpts = st.number_input("Points for feedback quality", 1, 40,
                                 int(_gs0.max_comment_points),
                                 help="Points a student earns for the quality of the "
                                      "feedback THEY wrote about teammates. The "
                                      "explainer above uses this number.")
    c3, c4 = st.columns(2)
    with c3:
        max_inc = st.slider("Maximum INCREASE (bonus cap, %)", 0, 50,
                            int(round((_gs0.max_multiplier - 1) * 100)), 1,
                            help="The most a top student's grade can rise above the team "
                                 "score. Default 5%.")
    with c4:
        max_dec = st.slider("Maximum DECREASE (deduction cap, %)", 0, 100,
                            int(round((1 - _gs0.min_multiplier) * 100)), 1,
                            help="The most a low student's grade can fall below the team "
                                 "score. Default 50%.")

    with st.expander("Advanced options (most instructors leave these alone)"):
        team_default = st.number_input("Default team score (before peer adjustment)",
                                       0.0, 200.0, float(_gs0.team_score_default))
        normalize = st.checkbox("Correct for easy and harsh graders",
                                value=bool(_gs0.normalize_raters),
                                help="Some students spread their $100 evenly and others "
                                     "swing it wildly. This puts every rater on the same "
                                     "scale before comparing students, so one generous or "
                                     "one severe teammate counts no more than the rest. "
                                     "Applies to the $100 and combined methods.")
        guard = st.checkbox("Agreement guard — soften a forced 'Low' when evaluators "
                            "strongly disagree", value=bool(_gs0.agreement_guard))
        _perf_methods = ["allocation_ratio", "rank_linear", "rank_one_mean"]
        perf_method = st.selectbox(
            "Performance label when there's NO forced ranking",
            _perf_methods,
            index=_perf_methods.index(_gs0.performance_method)
            if _gs0.performance_method in _perf_methods else 0,
            format_func=lambda x: {
                "allocation_ratio": "Above / below the team average (± band)",
                "rank_linear": "Top third High · bottom third Low",
                "rank_one_mean": "Only the single top contributor is High",
            }[x],
            help="Forced ranking is used automatically when the survey collected it; "
                 "this only applies otherwise.")
        band = st.slider("Performance band ± (for the average-based label)",
                         0.0, 0.25, float(_gs0.performance_band), 0.01)
        rstep = st.selectbox("Round adjustments to", [1, 5],
                             index=1 if int(_gs0.rounding_step) == 5 else 0,
                             format_func=lambda x: f"{x}%",
                             help="This is applied to the grade, not just to the "
                                  "number shown: at 5%, an adjustment of +4.4% "
                                  "becomes +5% in the gradebook as well as in the "
                                  "report.")
        _modes = ["nearest", "up", "down"]
        rmode = st.selectbox("Rounding direction", _modes,
                             index=_modes.index(_gs0.rounding_mode)
                             if _gs0.rounding_mode in _modes else 0)

    S["settings"] = GradeSettings(
        team_score_default=team_default, sensitivity_B=B,
        min_multiplier=1 - max_dec / 100.0, max_multiplier=1 + max_inc / 100.0,
        max_comment_points=int(maxpts), rounding_step=int(rstep), rounding_mode=rmode,
        performance_method=perf_method, performance_band=band, agreement_guard=guard,
        adjustment_source=adj_source, normalize_raters=normalize,
    )
    _method_words = {
        "allocation": "the $100 each student shares out",
        "rating": "the four rating statements",
        "ranking": "the High / Adequate / Low ranking",
        "combined": "the $100 allocation and the ratings together",
    }[adj_source]
    st.success(f"Grades will be adjusted using **{_method_words}**. A top student can "
               f"rise at most **+{max_inc}%** and a struggling one can fall at most "
               f"**−{max_dec}%** from the team score, in steps of {rstep}%.")
    if int(rstep) == 5:
        # Rounding now decides the grade rather than only the label on it, so
        # the step size has a consequence worth stating before anyone sees the
        # results: at 5%, anything under ±2.5% becomes no change at all.
        st.caption("In 5% steps, an adjustment smaller than ±2.5% rounds to no "
                   "change — which is most students in a typical section. Choose "
                   "1% steps if you want the smaller differences to show up in "
                   "the grade.")

# =========================================================================== #
# TAB 4 — Review + PDFs
# =========================================================================== #
with tabs[3]:
    st.subheader("Results & reports")
    st.caption("Step 4 of 5. Review the grades, download the instructor summary and the "
               "student feedback PDFs, then move to **⑤ Send feedback**.")
    if "long_df" not in S:
        st.info("No responses loaded yet. Go to **② Collect responses** and click "
                "*Load responses into grading* — or upload a Qualtrics export on the "
                "**① Set up survey** tab.")
    else:
        settings = S.get("settings", GradeSettings())
        teams = graded(S["long_df"], settings, S.get("self_evals"))
        S["teams"] = teams
        if not teams:
            st.warning("No teams were found in the responses. Every student needs a "
                       "value in the Team column, and a team needs at least two "
                       "students in it.")
        else:
            frame = results_to_frame(teams)
            # Problems that change the grades but raise no exception: a name
            # spelled two ways becomes two students, somebody on two teams
            # merges them, and a student nobody evaluated has no basis for an
            # adjustment. Each is handled conservatively, and the instructor is
            # the only one who can fix the cause — so it is said here, above the
            # numbers, rather than nowhere.
            _roster_names = [
                str(v.get("name") or "") for v in
                (getattr(S.get("roster"), "by_key", None) or {}).values()
            ]
            for _w in grading_warnings(S["long_df"], teams,
                                       _roster_names or None):
                st.warning(_w, icon="⚠️")
            st.dataframe(frame, use_container_width=True, height=380)
            st.download_button("⬇ Results CSV", frame.to_csv(index=False),
                               "peerparley_results.csv", "text/csv")

            report = _report_settings(course, eval_no)

            # ---- v2: AI narrative drafting + instructor review ------------
            # Sits above the downloads on purpose: the PDFs built below embed
            # whatever is approved here, so the review has to happen first for
            # the buttons underneath to include it.
            st.markdown("##### AI feedback narrative (optional)")
            narratives = ai_ui.render_review_panel(
                teams, ai_settings, course, eval_no,
                vault=vault_now(), slug=survey.slugify(course, eval_no))
            # Tab 5 builds its own PDFs for the email attachments and needs the
            # same approvals. Streamlit renders tabs in order within one run, so
            # this is always set before tab 5 reads it; it is kept in session
            # state rather than a module global so the survey-switch guard above
            # can clear it.
            S["ai_narratives"] = narratives
            # Per-student delivery choices made during review. Computed here so
            # every path below — zip, preview, email, .eml, auto-send — reads
            # the same decisions.
            from peerparley import feedback_ai as _fai
            ai_reports = _fai.report_overrides(S.get(ai_ui.STATE_KEY) or {}, report)
            S["ai_reports"] = ai_reports
            if narratives:
                st.caption(f"{len(narratives)} approved narrative(s) will be "
                           "included in the PDFs and emails below.")
            st.divider()

            st.markdown("##### Instructor reports")
            st.caption("The section summary lists every student's dimension grades, pay "
                       "grade, grade adjustment, the points they earned for the quality of "
                       "the feedback they wrote, and the confidential comments.")
            # Streamlit needs a download button's bytes up front, so both of
            # these were rebuilt on every rerun. Cached on the results they are
            # built from instead.
            _sig = _results_sig(teams)
            _summary_pdf = _instructor_pdf("summary", _sig, course, eval_no,
                                           _teams=teams)
            _conf_pdf = _instructor_pdf("confidential", _sig, course, eval_no,
                                        _teams=teams)
            r1, r2 = st.columns(2)
            r1.download_button(
                "⬇ Instructor summary (PDF)", _summary_pdf,
                f"peerparley_{course or 'section'}_eval{eval_no}_summary.pdf",
                "application/pdf", type="primary")
            r2.download_button(
                "⬇ Confidential feedback (PDF)", _conf_pdf,
                f"peerparley_{course or 'section'}_eval{eval_no}_confidential.pdf",
                "application/pdf")
            # Rendering a preview rasterises every page and pushes well over a
            # megabyte of images to the browser; inside an expander it happened
            # whether or not the expander was open. Now it is opt-in.
            if st.checkbox("Preview the instructor reports on screen",
                           key="prev_instructor",
                           help="Only needed if you want to look without "
                                "downloading. The PDFs above are the same thing."):
                with st.expander("Instructor summary", expanded=True):
                    _render_pdf(_summary_pdf)
                with st.expander("Confidential feedback", expanded=False):
                    _render_pdf(_conf_pdf)

            st.markdown("##### Student & team PDFs")
            colA, colB = st.columns(2)
            if colA.button("Build all PDFs (zip)"):
                zbuf = io.BytesIO()
                with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr("section_summary.pdf",
                               pdfgen.build_section_summary_pdf(teams, course, eval_no))
                    z.writestr("confidential_feedback.pdf",
                               pdfgen.build_confidential_pdf(teams, course, eval_no))
                    for t in teams:
                        z.writestr(f"team_{t.team}/team_contribution.pdf",
                                   pdfgen.build_team_contribution_pdf(t, eval_no))
                        for m in t.members:
                            safe = m.name.replace(" ", "_").replace("/", "-")
                            z.writestr(
                                f"team_{t.team}/{safe}_feedback.pdf",
                                pdfgen.build_individual_pdf(
                                    m, eval_no, course,
                                    report=ai_reports.get(m.key, report),
                                    narrative=narratives.get(m.key, "")))
                S["pdf_zip"] = zbuf.getvalue()
                st.success("PDFs built.")
            if S.get("pdf_zip"):
                colB.download_button("⬇ Download PDF bundle (zip)", S["pdf_zip"],
                                     f"peerparley_{course or 'section'}_eval{eval_no}.zip",
                                     "application/zip")

            with st.expander("Preview a student PDF"):
                names = [f"{t.team} · {m.name}" for t in teams for m in t.members]
                pick = st.selectbox("Student", names) if names else None
                if pick:
                    ti, ni = pick.split(" · ", 1)
                    m = next(m for t in teams for m in t.members
                             if t.team == ti and m.name == ni)
                    pdf = pdfgen.build_individual_pdf(
                        m, eval_no, course,
                        report=ai_reports.get(m.key, report),
                        narrative=narratives.get(m.key, ""))
                    if narratives.get(m.key):
                        st.caption("This preview includes the approved AI narrative.")
                    _render_pdf(pdf)

# =========================================================================== #
# TAB 5 — Email
# =========================================================================== #
with tabs[4]:
    st.subheader("Send feedback")
    st.caption("Step 5 of 5. Email each student their feedback PDF, or download a ready-to-"
               "send email pack. Prefer to send yourself? Use the CSV or the email pack.")
    if not S.get("teams"):
        st.info("No results yet — open **④ Results & reports** first so the grades are "
                "computed.")
    else:
        default_subject = "Your peer evaluation feedback — Eval {eval_no}"
        default_body = (
            "Hi {first_name},<br><br>"
            "Your peer feedback for {class} (Evaluation {eval_no}) is attached. "
            "It reflects anonymous input from your teammates.<br><br>"
            "Best,<br>The teaching team"
        )
        subject_t = st.text_input("Subject template", default_subject)
        body_t = _email_body(default_body, key="results_body", height=200)
        attach_team = st.checkbox("Attach team-contribution PDF", value=True)

        # live preview
        sample = S["teams"][0].members[0]
        ctx = _ctx(sample, course, eval_no)
        with st.expander("Live preview"):
            st.write("**Subject:** " + mail.render_template(subject_t, ctx))
            st.markdown(mail.render_template(body_t, ctx), unsafe_allow_html=True)

        report = _report_settings(course, eval_no)
        _results_delivery("res", S["teams"], S.get("roster"), subject_t, body_t,
                          attach_team, course, eval_no, report,
                          S.get("ai_narratives") or {},
                          S.get("ai_reports") or {})

# =========================================================================== #
# TAB 6 — Compare rounds (eval 1 vs 2 vs 3 for the same students)
# =========================================================================== #
with tabs[5]:
    st.subheader("Compare evaluations")
    st.caption("See how the same students' peer-evaluation results change across "
               "evaluation rounds for this course.")
    if not course:
        st.info("Pick or enter a course in the sidebar first.")
    else:
        vault = vault_now()
        _mine = [s for s in survey.visible_surveys(survey.list_surveys(vault), user)
                 if s["course"] == course]
        eval_nos = sorted({str(s["eval_no"]) for s in _mine}, key=lambda x: (len(x), x))
        if not eval_nos:
            st.info("No saved evaluations for this course yet.")
        else:
            picks = st.multiselect("Evaluations to compare", eval_nos, default=eval_nos)
            settings = S.get("settings", GradeSettings())
            # Behind a button on purpose. This reads and decrypts every response
            # for every round and regrades each one — measured at twelve seconds
            # for three rounds — and it used to do that on every rerun anywhere
            # in the app, because the multiselect defaults to all rounds.
            if not S.get("cmp_built"):
                st.caption(f"Reads and regrades {len(picks)} round(s). This takes a "
                           f"few seconds for a large course.")
            if st.button("Build the comparison", key="cmp_go", type="primary"):
                S["cmp_built"] = True
            evals_data = []
            for en in (picks if S.get("cmp_built") else []):
                ld = survey.responses_to_long(vault, survey.slugify(course, en))
                if ld.empty:
                    continue
                se = survey.self_evaluations(vault, survey.slugify(course, en))
                evals_data.append((en, graded(ld, settings, se)))
            if not evals_data and S.get("cmp_built"):
                st.warning("None of the selected evaluations have responses yet.")
            elif not evals_data:
                pass
            else:
                by_eval, team_of, names = {}, {}, set()
                for en, tr in evals_data:
                    by_eval[en] = {m.name: m for t in tr for m in t.members}
                    for t in tr:
                        for m in t.members:
                            names.add(m.name); team_of[m.name] = t.team
                rows = []
                for nm in sorted(names, key=lambda n: (str(team_of.get(n, "")), n.lower())):
                    row = {"Team": team_of.get(nm, ""), "Student": nm}
                    for en, _ in evals_data:
                        m = by_eval[en].get(nm)
                        row[f"E{en} Score"] = m.individual_score if m else None
                        row[f"E{en} Δ%"] = m.signed_pct if m else None
                        row[f"E{en} Perf"] = m.performance if m else ""
                    rows.append(row)
                cmp_df = pd.DataFrame(rows)
                st.dataframe(cmp_df, use_container_width=True, height=360)
                st.download_button("⬇ comparison.csv", cmp_df.to_csv(index=False),
                                   f"peerparley_{course}_comparison.csv", "text/csv")

                teams_list = sorted({team_of[n] for n in names})
                if teams_list:
                    tsel = st.selectbox("Chart a team's individual score across rounds",
                                        teams_list)
                    chart = []
                    for en, _ in evals_data:
                        d = {"Eval": f"Eval {en}"}
                        for nm in names:
                            if team_of.get(nm) == tsel:
                                m = by_eval[en].get(nm)
                                d[nm] = m.individual_score if m else None
                        chart.append(d)
                    st.line_chart(pd.DataFrame(chart).set_index("Eval"))

                st.download_button(
                    "⬇ Comparison report (PDF)",
                    pdfgen.build_comparison_pdf(course, evals_data),
                    f"peerparley_{course}_comparison.pdf", "application/pdf", type="primary")


# =========================================================================== #
# TAB 7 — Vault (encrypted save / load behind the firewall)
# =========================================================================== #
with tabs[6]:
    st.subheader("Encrypted vault — behind the firewall")
    st.caption("Bundles are AES-encrypted locally, then written to "
               f"**{cfg.vault.backend}**. The cloud host never stores plaintext PII.")
    vault = vault_now()

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Save current dataset**")
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
        default_key = f"{(course or 'section').replace(' ', '')}_eval{eval_no}_{stamp}.ppx"
        key = st.text_input("Bundle name", default_key)
        st.caption("A bundle now holds the responses **and** the self-evaluations, "
                   "the roster, the course name, and any AI drafts with your "
                   "approvals — everything needed to pick up where you left off. "
                   "Older bundles held the responses only; they still load.")
        if st.button("🔒 Encrypt & save to vault"):
            if "long_df" not in S:
                st.warning("Nothing to save yet.")
            else:
                try:
                    _drafts = {
                        k: d.to_dict()
                        for k, d in (S.get(ai_ui.STATE_KEY) or {}).items()
                    }
                    vault.put_bytes(key, workspace.bundle_bytes(
                        S["long_df"], S.get("self_evals"), S.get("roster"),
                        course, eval_no, _drafts))
                    _extra = (f" · {len(_drafts)} AI draft(s)" if _drafts else "")
                    st.success(f"Saved `{key}` to {cfg.vault.backend} — "
                               f"{len(S['long_df'])} rows{_extra}.")
                except Exception as exc:
                    st.error(f"Save failed: {exc}")
    with c2:
        st.markdown("**Load a saved bundle**")
        try:
            items = [x for x in vault.list() if x.endswith(".ppx")]
        except Exception as exc:
            items = []
            st.error(f"Vault list failed: {exc}")
        pick = st.selectbox("Bundle", items) if items else None
        if pick and st.button("🔓 Load & decrypt"):
            try:
                state, note = workspace.read_bundle(vault.get_bytes(pick))
                S["long_df"] = state["long_df"]
                if state.get("self_evals"):
                    S["self_evals"] = state["self_evals"]
                if state.get("roster") is not None:
                    S["roster"] = state["roster"]
                if state.get("course"):
                    # Restoring the course name is what reconnects the AI drafts,
                    # which are keyed by it. Set on the widget keys and pre-set
                    # the slug so the cross-survey guard doesn't read the change
                    # as a switch and wipe what was just loaded.
                    S["new_course"] = state["course"]
                    S["new_eval"] = state.get("eval_no") or "1"
                    S["_active_slug"] = survey.slugify(
                        state["course"], state.get("eval_no") or "1")
                if state.get("drafts"):
                    from peerparley import feedback_ai as _fai
                    S[ai_ui.STATE_KEY] = {
                        k: _fai.Draft.from_dict(v)
                        for k, v in state["drafts"].items()
                    }
                    S.pop(ai_ui.FP_KEY, None)
                _bits = [f"{len(state['long_df'])} rows"]
                if state.get("self_evals"):
                    _bits.append(f"{len(state['self_evals'])} self-evals")
                if state.get("drafts"):
                    _bits.append(f"{len(state['drafts'])} AI draft(s)")
                st.success(f"Loaded `{pick}` — " + " · ".join(_bits)
                           + ". Open **④ Results & reports**.")
                if note:
                    st.info(note, icon="ℹ️")
                st.rerun()
            except Exception as exc:
                st.error(f"Load failed: {exc}")

    st.divider()
    st.markdown("**Danger zone**")
    if items:
        dele = st.selectbox("Delete bundle", ["(choose)"] + items)
        if dele != "(choose)" and st.button("Delete permanently"):
            vault.delete(dele)
            st.warning(f"Deleted `{dele}`.")


# =========================================================================== #
# Autosave the working dataset
# =========================================================================== #
# Runs last, after every tab has had its chance to change things. Guarded by a
# fingerprint so an idle rerun does not re-upload the frame, and wrapped so a
# storage failure can never take down a page the instructor is working in — a
# best-effort autosave that crashes the app would be worse than none.
#
# This is what makes the sidebar's "Resume it" possible: the responses, the
# self-evaluations, the roster and the course name are saved together, and the
# course name is what rebuilds the slug the AI drafts live under.
# One exception: when the stored session exists but cannot be decrypted, saving
# over it would destroy the only copy. That case holds the autosave off (the
# sidebar says so) rather than trading the instructor's old work for their new.
if "long_df" in S and not S.get("_ws_unreadable"):
    _gs = S.get("settings") or GradeSettings()
    _ws_fp = workspace.fingerprint(S.get("long_df"), course, eval_no) + "|" + \
        str(dataclasses.astuple(_gs))
    if _ws_fp and S.get("_ws_fingerprint") != _ws_fp:
        _ws_ok, _ws_err = workspace.save(
            vault_now(), str(user.get("user") or "default"),
            long_df=S.get("long_df"), self_evals=S.get("self_evals"),
            roster=S.get("roster"), course=course, eval_no=eval_no,
            settings=dataclasses.asdict(_gs),
        )
        if _ws_ok:
            S["_ws_fingerprint"] = _ws_fp
            S["_ws_saved"] = None          # the live session is now the truth
        elif not S.get("_ws_warned"):
            # Once per session. Repeating it on every rerun would train the
            # instructor to ignore it.
            S["_ws_warned"] = True
            st.sidebar.warning(
                f"Could not autosave this session, so it will not survive "
                f"signing out: {_ws_err}", icon="⚠️")

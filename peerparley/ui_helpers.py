"""UI-side helpers shared by app.py (kept out of app.py to avoid ordering issues)."""
from __future__ import annotations

import base64
from typing import Dict, List, Optional

import streamlit as st

from . import email_delivery as mail
from . import pdfgen
from .config import AppConfig
from .grading import StudentResult, TeamResult
from .ingest import Roster


# Streamlit renamed `use_container_width=True` to `width="stretch"` and set a
# removal date for the old spelling that has now passed, while the versions
# requirements.txt still allows only understand the old one. So the spelling is
# decided once, here, by asking the installed Streamlit which argument it takes
# — rather than shipping a call that a routine upgrade turns into a traceback,
# or dropping support for the older versions.
def _detect_width_kwargs() -> dict:
    try:
        import inspect
        if "width" in inspect.signature(st.button).parameters:
            return {"width": "stretch"}
    except Exception:  # noqa: BLE001
        pass
    return {"use_container_width": True}


FULL_WIDTH = _detect_width_kwargs()


def render_pdf(pdf_bytes: bytes, key: str = "pdf", label: str = "") -> None:
    """Show a PDF on screen, page by page.

    Browsers block PDFs in an iframe, so the pages are rasterised with PyMuPDF
    and shown as images. Without that package there is no inline preview, and
    the fallback is a download button.

    `key` must be unique on the page. It used to be absent, and the fallback
    button therefore had an auto-generated id derived from its label and
    arguments — identical for every call. Two previews on one page (the
    instructor summary and the confidential report sit together) collided, and
    Streamlit's duplicate-id error is raised *while the page is being drawn*,
    so everything after the first preview — including the confidential report —
    simply never appeared. The bug only showed where PyMuPDF was missing, which
    is every Streamlit Community Cloud deployment, because the package used to
    be listed in requirements-optional.txt.
    """
    try:
        import fitz  # PyMuPDF
    except Exception:  # noqa: BLE001
        st.info("Showing a PDF inside the app needs the **PyMuPDF** package "
                "(`pip install PyMuPDF`, or add `PyMuPDF` to requirements.txt "
                "on Streamlit Cloud). The report itself is complete — download "
                "it below.", icon="📄")
        st.download_button(f"⬇ Download {label or 'PDF'}", pdf_bytes,
                           f"{key}.pdf", "application/pdf", key=f"dl_{key}")
        return
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                pix = page.get_pixmap(dpi=120)
                st.image(pix.tobytes("png"), **FULL_WIDTH)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"That report could not be drawn on screen ({exc}). "
                   "Downloading it works regardless.", icon="⚠️")
        st.download_button(f"⬇ Download {label or 'PDF'}", pdf_bytes,
                           f"{key}.pdf", "application/pdf", key=f"dl_{key}")


def student_ctx(m: StudentResult, course: str, eval_no: str) -> Dict[str, str]:
    first = m.name.split(" ")[0]
    last = m.name.split(" ")[-1]
    return {
        "first_name": first, "last_name": last, "name": m.name,
        "team": m.team, "eval_no": eval_no, "class": course,
    }


def _email_for(m: StudentResult, roster: Optional[Roster]) -> str:
    if roster:
        rec = roster.match(m.name)
        if rec and rec.get("email"):
            return rec["email"]
    return ""


def build_messages(teams: List[TeamResult], roster: Optional[Roster],
                   subject_t: str, body_t: str, attach_team: bool,
                   course: str, eval_no: str, report: dict = None,
                   narratives: Optional[Dict[str, str]] = None,
                   reports: Optional[Dict[str, dict]] = None) -> List[mail.Message]:
    """Build one email per student, attaching their feedback PDF (built with the
    instructor's `report` display settings) and optionally the team PDF.

    `narratives` maps student key -> approved AI summary, and should come from
    ``feedback_ai.approved_narratives``. A key that isn't in the map gets a PDF
    with no narrative section, which is the correct default: silence, not a
    draft nobody signed off on.
    """
    narratives = narratives or {}
    # `reports` holds per-student overrides of what the report shows, set during
    # review. A student who isn't in it gets the survey-wide `report`.
    reports = reports or {}
    messages: List[mail.Message] = []
    team_pdf_cache: Dict[str, bytes] = {}
    for t in teams:
        if attach_team and t.team not in team_pdf_cache:
            team_pdf_cache[t.team] = pdfgen.build_team_contribution_pdf(t, eval_no)
        for m in t.members:
            ctx = student_ctx(m, course, eval_no)
            safe = m.name.replace(" ", "_").replace("/", "-")
            atts = [mail.Attachment(
                f"{safe}_feedback.pdf",
                pdfgen.build_individual_pdf(
                    m, eval_no, course, report=reports.get(m.key, report),
                    narrative=narratives.get(m.key, "")))]
            if attach_team:
                atts.append(mail.Attachment(
                    f"team_{t.team}_contribution.pdf", team_pdf_cache[t.team]))
            messages.append(mail.Message(
                to_email=_email_for(m, roster),
                to_name=m.name, team=t.team,
                subject=mail.render_template(subject_t, ctx),
                body=mail.render_template(body_t, ctx),
                attachments=atts,
            ))
    return messages


def make_mailer(cfg: AppConfig):
    """Return a ready mailer, driving the Graph device-code flow in the UI."""
    if cfg.email.mode == "smtp":
        if not cfg.email.smtp_password:
            st.error("SMTP selected but no smtp_password in secrets.")
            return None
        return mail.SmtpMailer(cfg.email)

    m365 = cfg.m365
    tenant = m365.get("tenant_id", "")
    client = m365.get("client_id", "")
    if not tenant or not client:
        st.error("Graph mode needs vault.m365.tenant_id and client_id in secrets.")
        return None

    if "graph_mailer" not in st.session_state:
        st.session_state["graph_mailer"] = mail.GraphMailer(
            tenant, client, cfg.email.sender)
    gm: mail.GraphMailer = st.session_state["graph_mailer"]
    if gm.ready:
        return gm

    if "graph_flow" not in st.session_state:
        st.session_state["graph_flow"] = gm.begin_device_flow()
    flow = st.session_state["graph_flow"]
    st.info(flow.get("message", "Complete sign-in in your browser."))
    st.code(f"{flow.get('verification_uri')}\nCode: {flow.get('user_code')}")
    if st.button("I've completed sign-in"):
        if gm.complete_device_flow(flow):
            st.success("Microsoft 365 authenticated.")
            del st.session_state["graph_flow"]
            return gm
        st.error("Sign-in not completed yet — try again.")
    return None

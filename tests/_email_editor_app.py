"""Driver for the email editor and previews, in the real app.

Reported: *"The email messages shown in the application should be WYSIWYG not
HTML. And editable in WYSIWYG model."*

Two things were wrong. The editor was a box headed **Body (HTML)** containing
`Hi {first_name},<br><br>…<a href="{link}">…</a>` — the WYSIWYG editor existed
but `streamlit-quill` sat in requirements-optional.txt, and Streamlit Community
Cloud installs only requirements.txt, so every hosted deployment fell through
to the raw textarea. And the two messages a whole section actually receives,
the invitation and the reminder, had no preview at all.

Driven rather than unit-tested because the complaint is about the page. Run
twice: with the toolbar package and without it, which is how every Cloud
deployment was configured.

Not named test_* on purpose: pytest must not collect it. It runs in a fresh
process, because Streamlit's secrets are a process-wide singleton.

Usage:  python tests/_email_editor_app.py <working-dir> [--no-quill]
Prints one PASS/FAIL line per check and exits non-zero on any failure.
"""
from __future__ import annotations

import builtins
import hashlib
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FAILURES: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS" if ok else "FAIL"), "-", name, ("| " + detail) if detail else "")
    if not ok:
        FAILURES.append(name)


def hide_quill() -> None:
    """Make the process look like a Streamlit Community Cloud deployment."""
    real = builtins.__import__

    def guard(name, *a, **kw):
        if name.split(".")[0] == "streamlit_quill":
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real(name, *a, **kw)

    builtins.__import__ = guard


def main(workdir: str, no_quill: bool = False) -> int:
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)
    if no_quill:
        hide_quill()

    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ.update(FERNET_KEY=key, VAULT_BACKEND="local",
                      VAULT_FOLDER="email-editor")

    import pandas as pd
    from peerparley import survey
    from peerparley.vault import Vault

    rows = [{"First Name": f, "Last Name": l,
             "Email": f"{f.lower()}@example.edu", "Team": "1"}
            for f, l in [("Ann", "Apple"), ("Bob", "Brown"), ("Cid", "Cole")]]
    vault = Vault()
    slug, _ = survey.save_setup("EM101", "1", pd.DataFrame(rows),
                                dict(survey.DEFAULT_SURVEY),
                                vault=vault, owner="admin")

    # A second survey with a schedule, for {open_date} / {close_date}.
    _sched = dict(survey.DEFAULT_SURVEY, opens_at="2026-11-16T08:00:00",
                  closes_at="2026-11-20T17:00:00")
    slug2, _ = survey.save_setup("EM101", "2", pd.DataFrame(rows), _sched,
                                 vault=vault, owner="admin")

    from streamlit.testing.v1 import AppTest

    def boot(**state):
        app = AppTest.from_file(os.path.join(REPO, "app.py"),
                                default_timeout=600)
        app.secrets["fernet_key"] = key
        app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
        app.secrets["vault"] = {"backend": "local", "folder": "email-editor"}
        app.session_state["pp_user"] = {"user": "admin", "name": "Admin",
                                        "role": "admin", "source": "secrets"}
        app.session_state["new_course"] = "EM101"
        app.session_state["new_eval"] = "1"
        app.session_state["inv_url"] = "https://pp.example"
        for k, v in state.items():
            app.session_state[k] = v
        app.run()
        return app

    label = "without the toolbar package" if no_quill else "with the toolbar"
    app = boot()
    check(f"the setup tab renders ({label})", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    if app.exception:
        print("FAILURES:", FAILURES)
        return 1

    # ---- the editor ------------------------------------------------------
    areas = {str(getattr(t, "label", "")) for t in app.text_area}
    boxes = {str(getattr(c, "label", "")) for c in app.checkbox}
    check("an HTML escape hatch is offered, not forced",
          "Edit the HTML directly" in boxes, str(sorted(boxes))[:220])
    if no_quill:
        check("without the package the body falls back to an HTML box",
              "Body (HTML)" in areas, str(sorted(areas)))
        infos = " ".join(str(getattr(i, "value", "")) for i in app.info)
        check("and it says which package would give the toolbar",
              "streamlit-quill" in infos, infos[-220:])
    else:
        check("with the package the body is not edited as HTML",
              "Body (HTML)" not in areas, str(sorted(areas)))

    # ---- the preview -----------------------------------------------------
    labels = [str(getattr(e, "label", "")) for e in app.expander]
    previews = [s for s in labels if "what one student receives" in s]
    check("the invitation has a rendered preview", bool(previews), str(labels))
    check("the plain-text version is offered too",
          any("plain-text version" in s for s in labels), str(labels))

    shown = " ".join(str(getattr(m, "value", "")) for m in app.markdown)
    check("the preview fills in a real student's name", "Ann" in shown)
    check("the preview is rendered, not shown as markup",
          "&lt;br&gt;" not in shown and "Body (HTML)" not in shown)
    check("the subject is previewed", "**Subject:**" in shown, shown[:120])

    # ---- a typo in a placeholder is caught -------------------------------
    app = boot(inv_body_html="Hi {firstname}, your link is {link}.")
    warned = " ".join(str(getattr(w, "value", "")) for w in app.warning)
    check("a placeholder nothing fills in is flagged before sending",
          "{firstname}" in warned, warned[:300])

    # ---- editing as HTML round-trips -------------------------------------
    app = boot(inv_body_raw=True,
               inv_body_html="<b>Hi {first_name}</b> — see {link}")
    # Both the invitation and the reminder have a body box, and when neither
    # has the toolbar they share the same label — so look at all of them.
    html_boxes = [str(getattr(t, "value", "")) for t in app.text_area
                  if str(getattr(t, "label", "")) == "Body (HTML)"]
    check("the HTML view shows the markup it was given",
          any("<b>Hi {first_name}</b>" in v for v in html_boxes),
          f"{len(html_boxes)} body box(es): {[v[:40] for v in html_boxes]}")
    shown = " ".join(str(getattr(m, "value", "")) for m in app.markdown)
    check("and the preview still renders it", "Hi Ann" in shown, shown[:200])

    # ---- the reminder path shares all of it ------------------------------
    app = boot()
    labels = [str(getattr(e, "label", "")) for e in app.expander]
    check("the reminder panel exists",
          any("reminder to non-responders" in s for s in labels), str(labels))

    # ---- {open_date} / {close_date} (2.18.0) -----------------------------
    dated = "Opens {open_date}. Please finish by {close_date}. {link}"
    app = boot(new_eval="2", inv_body_html=dated)
    check("a scheduled survey renders with dates", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))
    shown = " ".join(str(getattr(m, "value", "")) for m in app.markdown)
    check("the preview shows the open date",
          "Monday, November 16, 2026 at 8:00 AM" in shown, shown[:300])
    check("the preview shows the close date",
          "Friday, November 20, 2026 at 5:00 PM" in shown, shown[:300])
    check("no placeholder is left in the preview",
          "{open_date}" not in shown and "{close_date}" not in shown)
    warned = " ".join(str(getattr(w, "value", "")) for w in app.warning)
    check("the date placeholders are not flagged as unknown",
          "{close_date}" not in warned, warned[:300])
    help_text = " ".join(str(getattr(c, "value", "")) for c in app.caption)
    check("the placeholder help lists them",
          "{open_date}" in help_text and "{close_date}" in help_text)

    # A survey with no schedule: say so before "TBA" reaches forty inboxes.
    app = boot(inv_body_html=dated)
    warned = " ".join(str(getattr(w, "value", "")) for w in app.warning)
    check("a date the survey does not have is warned about",
          "no close date" in warned and "no open date" in warned, warned[:300])

    # Changed on screen but not saved: the email quotes the saved date, and
    # says so — the saved one is when the form really closes.
    _edited = dict(_sched, closes_at="2026-11-22T17:00:00")
    app = boot(new_eval="2", inv_body_html=dated,
               _cfg_loaded_for=slug2, survey_cfg=_edited,
               close_d=__import__("datetime").date(2026, 11, 22))
    warned = " ".join(str(getattr(w, "value", "")) for w in app.warning)
    check("an unsaved date change is pointed out",
          "changed but not saved" in warned, warned[:300])
    shown = " ".join(str(getattr(m, "value", "")) for m in app.markdown)
    check("and the message keeps the saved date",
          "Friday, November 20, 2026 at 5:00 PM" in shown, shown[:300])

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    raise SystemExit(main(args[0] if args else ".",
                          no_quill="--no-quill" in sys.argv))

"""Driver for "Restore defaults" on the Grading rules tab. Run as a script.

Asked for: *"for the grading rules allow the instructor to restore defaults."*
Eleven controls, several behind an expander, and no way to tell which had been
nudged — an instructor who had experimented could only get back by re-reading
the documentation and setting each one by hand.

Worth driving rather than unit-testing, because the mechanism is Streamlit's:
none of those widgets carries a `key`, so each one's identity is derived from
its own arguments, and putting the defaults back into session state is what
makes every widget re-render at its default value. That is a property of the
framework, so it has to be exercised through the framework.

Not named test_* on purpose: pytest must not collect it. It runs in a fresh
process, because Streamlit's secrets are a process-wide singleton.

Usage:  python tests/_restore_defaults_app.py <working-dir>
Prints one PASS/FAIL line per check and exits non-zero on any failure.
"""
from __future__ import annotations

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


def main(workdir: str) -> int:
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)

    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ.update(FERNET_KEY=key, VAULT_BACKEND="local",
                      VAULT_FOLDER="restore-defaults")

    from peerparley.grading import GradeSettings
    defaults = GradeSettings()

    # What an instructor who has been experimenting looks like: a different
    # method, a different sensitivity, wider caps, coarse rounding, and two of
    # the advanced switches flipped.
    meddled = GradeSettings(
        sensitivity_B=1.25, rounding_step=5, rounding_mode="up",
        min_multiplier=0.20, max_multiplier=1.30, max_comment_points=25,
        adjustment_source="ranking", normalize_raters=True,
        agreement_guard=False, performance_band=0.20,
        performance_method="rank_linear", team_score_default=90.0,
    )

    from streamlit.testing.v1 import AppTest

    def boot(settings):
        app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=300)
        app.secrets["fernet_key"] = key
        app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
        app.secrets["vault"] = {"backend": "local", "folder": "restore-defaults"}
        app.session_state["pp_user"] = {"user": "admin", "name": "Admin",
                                        "role": "admin", "source": "secrets"}
        app.session_state["new_course"] = "QC101"
        app.session_state["new_eval"] = "1"
        app.session_state["settings"] = settings
        app.run()
        return app

    # ---- with defaults in play there is nothing to restore ---------------
    app = boot(GradeSettings())
    check("the page renders on defaults", not app.exception,
          str([str(e.value)[:160] for e in app.exception]))
    buttons = [b for b in app.button
               if "Restore defaults" in str(getattr(b, "label", ""))]
    check("no restore button is offered when nothing differs", not buttons,
          f"{len(buttons)} button(s)")

    # ---- with settings changed, the button appears and says what differs -
    app = boot(meddled)
    check("the page renders on changed settings", not app.exception,
          str([str(e.value)[:160] for e in app.exception]))
    buttons = [b for b in app.button
               if "Restore defaults" in str(getattr(b, "label", ""))]
    check("the restore button is offered", bool(buttons))
    captions = " ".join(str(getattr(c, "value", "")) for c in app.caption)
    check("it says how many settings differ", "setting(s) differ" in captions,
          captions[:200])

    # The controls are showing the changed values, not the defaults — which is
    # the state the button has to get out of.
    sliders = {str(getattr(s, "label", "")): s.value for s in app.slider}
    sens = next((v for k, v in sliders.items() if k.startswith("Sensitivity")),
                None)
    check("the sensitivity slider shows the changed value", sens == 1.25,
          str(sens))

    if not buttons:
        print("FAILURES:", FAILURES)
        return 1

    # ---- press it -------------------------------------------------------
    buttons[0].click().run()
    check("pressing it raises nothing", not app.exception,
          str([str(e.value)[:200] for e in app.exception]))

    now = app.session_state["settings"]
    differing = [f for f in defaults.__dataclass_fields__
                 if getattr(now, f) != getattr(defaults, f)]
    check("every grading rule is back to its default", not differing,
          str([(f, getattr(now, f), getattr(defaults, f)) for f in differing]))

    sliders = {str(getattr(s, "label", "")): s.value for s in app.slider}
    sens = next((v for k, v in sliders.items() if k.startswith("Sensitivity")),
                None)
    check("the controls themselves show the defaults, not just the state",
          sens == defaults.sensitivity_B, str(sens))

    check("it says so", any("back to its default" in str(getattr(s, "value", ""))
                            for s in app.success))
    buttons = [b for b in app.button
               if "Restore defaults" in str(getattr(b, "label", ""))]
    check("and the button steps aside again", not buttons,
          f"{len(buttons)} button(s)")

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))

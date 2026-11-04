"""The reported crash: storage set to a backend whose package is not installed.

    ModuleNotFoundError ... File "peerparley/vault.py", line 148, in __init__
        import dropbox

That reached the instructor as a raw traceback on the sign-in screen, because
2.10.0 moved `dropbox` out of requirements.txt and Streamlit Community Cloud
installs only that file. The package is back where it belongs, and this driver
holds the second half of the fix: when a backend's package is missing for any
reason, the app must say so in words an instructor can act on, and a student
opening their link must not see any of it.

Run as a script, in a fresh process, because it hides a module from the import
system for the whole run.

Usage:  python tests/_missing_backend_app.py <working-dir>
"""
from __future__ import annotations

import builtins
import hashlib
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FAILURES: list = []
HIDDEN = "dropbox"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS" if ok else "FAIL"), "-", name, ("| " + detail) if detail else "")
    if not ok:
        FAILURES.append(name)


def hide_the_package() -> None:
    """Make `import dropbox` fail exactly as it does on a deployment without it."""
    sys.modules.pop(HIDDEN, None)
    real_import = builtins.__import__

    def guarded(name, *a, **kw):
        if name == HIDDEN or name.startswith(HIDDEN + "."):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real_import(name, *a, **kw)

    builtins.__import__ = guarded


def main(workdir: str) -> int:
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)

    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ.update(FERNET_KEY=key, VAULT_BACKEND="dropbox",
                      VAULT_FOLDER="PeerParley")

    hide_the_package()

    # ---- the library layer ---------------------------------------------
    from peerparley.vault import Vault, VaultUnavailable
    vault = Vault()
    check("building a vault does not raise", True)
    check("it reports itself unavailable", not vault.available)
    message = vault.error
    check("the message names the package to install",
          "dropbox" in message and "requirements.txt" in message, message[:140])
    check("and names the setting that changes it",
          "backend" in message, message[:140])
    check("it is not a bare ModuleNotFoundError",
          "ModuleNotFoundError" not in message and "Traceback" not in message)

    ok, health = vault.healthcheck()
    check("the healthcheck reports it rather than raising", not ok and bool(health))

    raised = ""
    try:
        vault.get_bytes("anything.ppj")
    except VaultUnavailable as exc:
        raised = str(exc)
    except Exception as exc:  # noqa: BLE001
        raised = f"WRONG TYPE: {type(exc).__name__}"
    check("a read raises the readable error, not an import error",
          raised.startswith("Storage is set to"), raised[:90])

    # ---- the instructor's screen ----------------------------------------
    from peerparley import setup_wizard
    problem = setup_wizard.storage_problem()
    check("the app detects the problem before sign-in", bool(problem), problem[:90])

    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=180)
    app.secrets["fernet_key"] = key
    app.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
    app.secrets["vault"] = {"backend": "dropbox", "folder": "PeerParley"}
    app.run()
    check("the page renders without an exception", not app.exception,
          str([str(e.value)[:160] for e in app.exception]))

    page = " ".join(str(getattr(e, "value", "")) for e in app.markdown) + " " + \
        " ".join(str(getattr(e, "value", "")) for e in app.error)
    check("it explains the storage problem instead of showing a sign-in box",
          "cannot reach its storage" in page, page[:160])
    check("the explanation names the package", "dropbox" in page)
    check("it reassures that nothing was lost", "Nothing is lost" in page)
    check("no password box is offered into a broken app",
          not [w for w in app.text_input
               if "assword" in str(getattr(w, "label", ""))])

    # ---- the student's screen -------------------------------------------
    # A student can do nothing about the institution's storage, and must not be
    # shown its configuration.
    from peerparley import survey
    from peerparley.tokens import make_token
    token = make_token({"s": "QA101-eval1", "t": "1", "p": 0},
                       survey.token_secret())
    student = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=180)
    student.secrets["fernet_key"] = key
    student.secrets["app_password_sha256"] = hashlib.sha256(b"pw").hexdigest()
    student.secrets["vault"] = {"backend": "dropbox", "folder": "PeerParley"}
    student.query_params["t"] = token if hasattr(student, "query_params") else None
    if hasattr(student, "query_params"):
        student.run()
        errors = " ".join(str(getattr(e, "value", "")) for e in student.error)
        check("a student sees no traceback", not student.exception,
              str([str(e.value)[:120] for e in student.exception]))
        check("a student is told it is unavailable, not why",
              "available right now" in errors and "dropbox" not in errors.lower(),
              errors[:120])
    else:
        # Older Streamlit test harness: exercise the renderer's guard directly.
        check("a student sees no traceback (checked at the library level)",
              Vault().available is False)

    print("FAILURES:", FAILURES or "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))

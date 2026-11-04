#!/usr/bin/env python3
"""Is this PeerParley installation ready to use?  →  python3 selfcheck.py

Run this after installing, after changing secrets, or any time the app behaves
oddly. It touches nothing: no student data is read, no email is sent, no AI
request is made, nothing is written except one temporary probe file in the
storage folder, which is removed again.

Every line is either OK, a WARNING (works, but you should know) or a PROBLEM
(this will stop you). The exit status is 0 when nothing is a problem, so it can
also be used in a deployment check.

It is deliberately dependency-light: if the install is broken enough that
Streamlit will not import, this script still runs and says so.
"""
from __future__ import annotations

import importlib
import os
import platform
import sys
import warnings

# This report is read by someone deciding whether their install works. A
# deprecation notice from a third-party library in the middle of it is noise
# that reads like a fault.
warnings.filterwarnings("ignore")
os.environ.setdefault("STREAMLIT_GLOBAL_SUPPRESS_DEPRECATION_WARNINGS", "true")

OK, WARN, BAD = "OK     ", "WARNING", "PROBLEM"
_results: list = []


def say(level: str, line: str, detail: str = "") -> None:
    _results.append((level, line))
    icon = {OK: "✓", WARN: "!", BAD: "✗"}[level]
    print(f"  {icon} {level}  {line}")
    if detail:
        for part in detail.split("\n"):
            print(f"            {part}")


def heading(text: str) -> None:
    print(f"\n{text}\n" + "-" * len(text))


# --------------------------------------------------------------------------- #
# 1. Python and packages
# --------------------------------------------------------------------------- #
MIN_PYTHON = (3, 10)

REQUIRED = [
    ("streamlit", "the app itself"),
    ("pandas", "reading contact lists and exports"),
    ("numpy", "the grading arithmetic"),
    ("openpyxl", "Excel contact lists"),
    ("reportlab", "the PDF reports"),
    ("cryptography", "encrypting student data"),
    ("requests", "talking to your storage provider"),
]
OPTIONAL = [
    ("fitz", "PyMuPDF — previewing a PDF inside the app"),
    ("msal", "Microsoft 365 storage and email"),
    ("dropbox", "Dropbox storage"),
    ("tenacity", "retrying an AI request that was rate-limited"),
    ("openai", "OpenAI / xAI / OpenRouter / a local model, for the AI writer"),
    ("anthropic", "Claude, for the AI writer"),
    ("google.genai", "Gemini, for the AI writer"),
    ("streamlit_quill", "the rich email editor (a plain box is used without it)"),
]


def check_python() -> None:
    heading("Python")
    version = ".".join(str(p) for p in sys.version_info[:3])
    if sys.version_info >= MIN_PYTHON:
        say(OK, f"Python {version} on {platform.system()}")
    else:
        say(BAD, f"Python {version} is too old",
            f"PeerParley needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer. "
            "Install a newer Python and run the install again.")


def check_packages() -> None:
    heading("Packages")
    for module, why in REQUIRED:
        try:
            mod = importlib.import_module(module)
            version = getattr(mod, "__version__", "")
            say(OK, f"{module} {version}".strip() + f" — {why}")
        except Exception as exc:  # noqa: BLE001
            say(BAD, f"{module} is missing — {why}",
                f"Fix with:  pip install -r requirements.txt\n({exc})")
    missing_optional = []
    for module, why in OPTIONAL:
        try:
            importlib.import_module(module)
        except Exception:  # noqa: BLE001
            missing_optional.append((module, why))
    if missing_optional:
        say(OK, f"{len(OPTIONAL) - len(missing_optional)} of {len(OPTIONAL)} "
                "optional packages installed")
        for module, why in missing_optional:
            print(f"            - not installed: {module} ({why})")
        print("            Install only what you use: "
              "see requirements-optional.txt")
    else:
        say(OK, "every optional package is installed too")


# --------------------------------------------------------------------------- #
# 2. Configuration
# --------------------------------------------------------------------------- #

def check_config() -> None:
    heading("Configuration")
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    secrets_file = os.path.join(here, ".streamlit", "secrets.toml")
    if os.path.exists(secrets_file):
        say(OK, "found .streamlit/secrets.toml")
    else:
        say(WARN, "no .streamlit/secrets.toml on disk",
            "Normal on Streamlit Cloud, where secrets are pasted into the app's\n"
            "Settings instead. On your own computer, start the app and its\n"
            "first screen will create this file for you.")

    try:
        from peerparley.config import load_config
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        say(BAD, f"the configuration could not be read: {exc}")
        return

    if (cfg.app_password_sha256 or "").strip():
        say(OK, "an administrator password is configured")
    else:
        say(WARN, "no administrator password configured",
            "Nobody can sign in yet. The app's first screen will set this up.")

    if (cfg.fernet_key or "").strip():
        say(OK, "an encryption key is configured")
    else:
        say(BAD, "no encryption key configured",
            "Student data cannot be stored without it. The app's first screen\n"
            "will generate one; keep a copy in a password manager.")

    if (cfg.public_url or "").strip():
        say(OK, f"app address: {cfg.public_url}")
    else:
        say(WARN, "no app address configured",
            "Student links need one. You can type it on the first tab instead;\n"
            "PeerParley will not let you email links without it.")

    backend = cfg.vault.backend
    # The package the chosen backend needs. This is the check that would have
    # caught a deployment whose secrets said "dropbox" while requirements.txt
    # did not: it failed at sign-in with a bare ModuleNotFoundError instead.
    needs = {"dropbox": "dropbox", "m365": "msal"}.get(backend)
    if needs:
        try:
            importlib.import_module(needs)
            say(OK, f"the {backend} backend's package ({needs}) is installed")
        except Exception:  # noqa: BLE001
            say(BAD, f"storage is set to '{backend}' but `{needs}` is not installed",
                f"The app cannot open your storage. Add `{needs}` to\n"
                "requirements.txt and redeploy — on Streamlit Community Cloud\n"
                "that is the only file it installs — or change [vault] backend.")
    if backend == "local":
        say(WARN, "storage is set to 'local'",
            "Encrypted files are kept beside the app. Fine for trying it out,\n"
            "but on a hosted server they can be wiped at any restart — move to\n"
            "m365, dropbox or pcloud before collecting real responses\n"
            "(DEPLOYMENT.md).")
    else:
        say(OK, f"storage is set to '{backend}'")


# --------------------------------------------------------------------------- #
# 3. Does encryption and storage actually work?
# --------------------------------------------------------------------------- #

def check_storage() -> None:
    heading("Encryption and storage")
    try:
        from peerparley.config import load_config
        if not (load_config().fernet_key or "").strip():
            # Already reported above as the one problem it is. Saying it twice
            # makes a two-problem report out of one fact.
            say(WARN, "storage was not tested, because there is no key yet")
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        from peerparley.security import decrypt_bytes, encrypt_bytes
        probe = b"peerparley-selfcheck"
        if decrypt_bytes(encrypt_bytes(probe)) == probe:
            say(OK, "encryption works (a value encrypted and read back intact)")
        else:
            say(BAD, "encryption did not round-trip")
            return
    except Exception as exc:  # noqa: BLE001
        say(BAD, f"encryption is not usable: {exc}")
        return

    try:
        from peerparley.vault import Vault
        vault = Vault()
        reachable, message = vault.healthcheck()
        say(OK if reachable else BAD, message)
        if not reachable:
            return
    except Exception as exc:  # noqa: BLE001
        say(BAD, f"storage could not be opened: {exc}")
        return

    name = "selfcheck.probe"
    try:
        vault.put_bytes(name, b"peerparley-selfcheck")
        got = vault.get_bytes(name)
        say(OK if got == b"peerparley-selfcheck" else BAD,
            "a test file was written, read back and decrypted")
    except Exception as exc:  # noqa: BLE001
        say(BAD, f"writing to storage failed: {exc}")
    finally:
        try:
            vault.delete(name)
        except Exception:  # noqa: BLE001
            print("            (could not remove the test file "
                  f"'{name}' — delete it by hand)")


# --------------------------------------------------------------------------- #
# 4. The parts that reach the outside world
# --------------------------------------------------------------------------- #

def check_delivery() -> None:
    heading("Email and AI (both optional)")
    try:
        from peerparley.config import load_config
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return

    mode = cfg.email.mode
    if mode == "smtp" and not cfg.email.smtp_password:
        say(WARN, "email is set to SMTP but no password is configured",
            "Sending directly will fail. The app can still build ready-to-send\n"
            "email files you send from your own mail program.")
    elif mode == "graph" and not (cfg.m365.get("tenant_id")
                                 and cfg.m365.get("client_id")):
        say(WARN, "email is set to Microsoft 365 but it is not configured",
            "Same fallback applies: PeerParley can build the emails for you to\n"
            "send yourself.")
    else:
        say(OK, f"email is configured ({mode})")

    try:
        from peerparley.aiconfig import PROVIDERS, get_secret
        configured = [p.label for p in PROVIDERS.values()
                      if getattr(p, "env_var", "") and get_secret(p.env_var)]
        if configured:
            say(OK, "AI writer: a key is configured for " + ", ".join(configured),
                "It stays switched off until an instructor turns it on, every\n"
                "session. Nothing is sent anywhere until then.")
        else:
            say(OK, "AI writer: no key configured, so the feature is off",
                "Everything else works without it. An instructor can also paste\n"
                "a key in the sidebar for a single session.")
    except Exception as exc:  # noqa: BLE001
        say(WARN, f"could not check the AI settings: {exc}")


# --------------------------------------------------------------------------- #
# 5. Is the app itself intact?
# --------------------------------------------------------------------------- #

def check_app() -> None:
    heading("The app")
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("app.py", "requirements.txt",
                 os.path.join("assets", "ContactList_Template.xlsx")):
        path = os.path.join(here, name)
        if os.path.exists(path):
            say(OK, f"{name} is present")
        else:
            say(BAD, f"{name} is missing",
                "The download is incomplete — unzip it again, or re-clone it.")
    try:
        import peerparley
        say(OK, f"PeerParley version {peerparley.__version__}")
    except Exception as exc:  # noqa: BLE001
        say(BAD, f"the peerparley package could not be imported: {exc}")


def main() -> int:
    print("PeerParley self-check")
    print("=====================")
    print("Nothing here changes your data. Run it any time.")
    check_python()
    check_packages()
    check_app()
    check_config()
    check_storage()
    check_delivery()

    problems = [line for level, line in _results if level == BAD]
    warnings = [line for level, line in _results if level == WARN]
    print("\nSummary\n-------")
    if problems:
        print(f"  {len(problems)} problem(s) will stop you:")
        for line in problems:
            print(f"    • {line}")
        print("\n  Start the app anyway (streamlit run app.py) — its first "
              "screen\n  walks through the password and the encryption key.")
    else:
        print("  No problems. PeerParley is ready to use.")
    if warnings:
        print(f"\n  {len(warnings)} thing(s) worth knowing:")
        for line in warnings:
            print(f"    • {line}")
    print()
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

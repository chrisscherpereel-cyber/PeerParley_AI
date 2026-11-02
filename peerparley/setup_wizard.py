"""First-run setup, for someone who has just been handed this app.

The problem this solves
-----------------------
Before release, a brand-new deployment showed this and stopped:

    No accounts and no app password configured. Set `app_password_sha256` in
    secrets, sign in as admin, then add instructors.
    python -c "import hashlib,getpass;print(hashlib.sha256(...).hexdigest())"

Four unfamiliar nouns, a shell one-liner, and — verified with Streamlit's own
test harness — **zero buttons or fields on the page**. There was no way forward
from inside the app at all. For an instructor who was handed a link by a
colleague, that is the end of the road, and it is the first thing they see.

A second state was nearly as bad: an admin password set but no encryption key.
That let someone sign in, fill in forty students, press Save, and *then* learn
that nothing can be stored.

What this module does
---------------------
It generates the two values the app needs, shows them once, and hands over the
exact text to paste — into Streamlit Cloud's **Secrets** box, or into a local
``.streamlit/secrets.toml`` it can write itself when the folder is writable.
Nothing here asks the user to open a terminal.

Why it is safe to offer
-----------------------
It renders **only** in the two states where the app cannot work at all: no
credential of any kind configured, or no usable encryption key. In the first,
there is nothing to protect — nobody can sign in, and no student data can
exist, because every durable byte goes through the key that is missing. In the
second, the data that exists is unreadable without the original key, and this
screen cannot replace a key that is already configured and valid: it refuses to
overwrite a secrets file, and it never touches an existing account list.

So this is not a back door into a configured deployment. A deployment with a
password or an account list configured never reaches this code — the sign-in
form does, exactly as before.
"""
from __future__ import annotations

import hashlib
import os
from typing import Tuple

import streamlit as st

from . import accounts
from .config import load_config

SECRETS_DIR = ".streamlit"
SECRETS_PATH = os.path.join(SECRETS_DIR, "secrets.toml")


# --------------------------------------------------------------------------- #
# What state is this deployment in?
# --------------------------------------------------------------------------- #

def key_is_usable() -> bool:
    """Can this app encrypt and decrypt? Everything durable depends on it."""
    try:
        from .security import decrypt_bytes, encrypt_bytes
        probe = b"peerparley-setup-probe"
        return decrypt_bytes(encrypt_bytes(probe)) == probe
    except Exception:  # noqa: BLE001
        return False


def has_credential() -> bool:
    """Is there any way to sign in — an app password or a stored account?"""
    try:
        cfg = load_config()
        if (cfg.app_password_sha256 or "").strip():
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        return bool(accounts.load_accounts())
    except Exception:  # noqa: BLE001
        return False


def needed() -> bool:
    """True when the app cannot be used at all until something is configured."""
    return not has_credential() or not key_is_usable()


# --------------------------------------------------------------------------- #
# The values, and the file
# --------------------------------------------------------------------------- #

def password_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def new_fernet_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def secrets_block(pw_hash: str, fernet_key: str, public_url: str = "") -> str:
    """The minimum that makes PeerParley work, as pasteable TOML.

    Deliberately short. The full annotated template is
    ``.streamlit/secrets.toml.example``; everything omitted here has a working
    default, and a new user should not have to read sixty lines of options to
    get started.
    """
    return (
        "# PeerParley — minimum working configuration.\n"
        "# Keep this private: it is the password hash and the key that encrypts\n"
        "# your students' data. Store a copy of fernet_key somewhere safe (a\n"
        "# password manager): without it, everything already saved is\n"
        "# unreadable, including by you.\n\n"
        f'app_password_sha256 = "{pw_hash}"\n'
        f'fernet_key = "{fernet_key}"\n'
        f'public_url = "{public_url.strip()}"\n\n'
        "[vault]\n"
        '# "local" keeps encrypted files beside the app — fine for trying it\n'
        "# out. Move to m365, dropbox or pcloud before collecting real\n"
        "# responses; see DEPLOYMENT.md.\n"
        'backend = "local"\n'
        'folder = "PeerParley"\n'
    )


def can_write_secrets() -> Tuple[bool, str]:
    """Is writing a local secrets file possible, and should it be offered?

    Not on Streamlit Community Cloud: the filesystem there is ephemeral, so a
    file written now is gone at the next restart and the user would be set up
    for one session only. Offering it would be worse than not offering it.
    """
    from . import localmodels
    if localmodels.is_hosted():
        return False, ("This app is running on Streamlit's servers, where files "
                       "do not survive a restart — paste the text below into "
                       "the app's Secrets box instead.")
    if os.path.exists(SECRETS_PATH):
        return False, (f"`{SECRETS_PATH}` already exists, so it will not be "
                       f"overwritten. Open it and add the lines below.")
    try:
        os.makedirs(SECRETS_DIR, exist_ok=True)
        probe = os.path.join(SECRETS_DIR, ".write-probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"This folder is not writable ({exc}), so paste it instead."


def write_secrets(text: str) -> Tuple[bool, str]:
    """Write the secrets file, refusing to clobber one that already exists."""
    try:
        if os.path.exists(SECRETS_PATH):
            return False, f"{SECRETS_PATH} already exists; nothing was changed."
        os.makedirs(SECRETS_DIR, exist_ok=True)
        with open(SECRETS_PATH, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(SECRETS_PATH, 0o600)      # best-effort; no-op on Windows
        except Exception:  # noqa: BLE001
            pass
        return True, SECRETS_PATH
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #

def render() -> None:
    """Draw the setup screen. The caller stops the app after this."""
    missing_cred = not has_credential()
    missing_key = not key_is_usable()

    st.markdown("### Welcome — PeerParley needs two things before you start")
    if missing_cred and missing_key:
        st.info("This looks like a brand-new installation. It takes about a "
                "minute, and this page does the fiddly parts for you.", icon="👋")
    elif missing_key:
        st.warning(
            "**Sign-in is set up, but this app has no usable encryption key**, "
            "so nothing can be stored yet — not a roster, not a response, not a "
            "grade. Adding the key below finishes the job. If a key was "
            "configured before and you have changed it, put the original back "
            "instead: data saved under the old key cannot be read with a new "
            "one.", icon="🔑")
    else:
        st.warning("**Nobody can sign in to this app yet** — there is no "
                   "password and no instructor account. Set one below.", icon="🔐")

    st.markdown(
        "1. **A password** you will use to sign in as the administrator.\n"
        "2. **An encryption key**, which scrambles every student record this "
        "app stores. PeerParley generates this one; you only have to keep a "
        "copy of it."
    )

    with st.form("pp_setup"):
        pw1 = st.text_input("Choose an administrator password", type="password",
                            help="At least 10 characters. You can add accounts "
                                 "for other instructors later, from inside the "
                                 "app.")
        pw2 = st.text_input("Type it again", type="password")
        url = st.text_input(
            "This app's web address (optional)",
            placeholder="https://your-peerparley-address",
            help="Copy it from your browser's address bar. It is what makes each "
                 "student's personal link work; you can also fill it in later, "
                 "on the first tab.")
        go = st.form_submit_button("Generate my settings", type="primary")

    if go:
        if len(pw1) < 10:
            st.error("Use at least 10 characters — this password is the only "
                     "thing standing between the internet and your students' "
                     "evaluations.")
        elif pw1 != pw2:
            st.error("Those two passwords are not the same.")
        else:
            st.session_state["pp_setup_block"] = secrets_block(
                password_hash(pw1), new_fernet_key(), url)

    block = st.session_state.get("pp_setup_block")
    if not block:
        with st.expander("Rather do this by hand?"):
            st.markdown(
                "Copy `.streamlit/secrets.toml.example` to "
                "`.streamlit/secrets.toml` and fill in `app_password_sha256` "
                "and `fernet_key`. **DEPLOYMENT.md** walks through it, and "
                "`python3 selfcheck.py` will tell you whether it worked."
            )
        return

    st.divider()
    st.success("Here are your settings. Nothing is saved until you do one of "
               "the two things below.", icon="✅")
    st.caption("Your password is not in this text — only an irreversible hash "
               "of it. The key, though, *is* the key: anyone who has it can "
               "read your stored student data.")
    st.code(block, language="toml")

    writable, why_not = can_write_secrets()
    c1, c2 = st.columns([1, 1.4])
    if writable:
        if c1.button("Save it for me", type="primary", key="pp_setup_write"):
            ok, detail = write_secrets(block)
            if ok:
                st.success(f"Saved to `{detail}`. Now stop the app and start it "
                           f"again (Ctrl+C, then `streamlit run app.py`) — "
                           f"Streamlit reads this file at startup.", icon="💾")
                st.caption("Then sign in with the username **admin** and the "
                           "password you just chose.")
            else:
                st.error(detail)
    else:
        c1.caption(why_not)
    c2.download_button("⬇ Download secrets.toml", block, "secrets.toml",
                       "text/plain",
                       help="For Streamlit Cloud: open the app's Settings → "
                            "Secrets and paste the contents in.")

    st.markdown(
        "**On Streamlit Community Cloud:** open **Manage app → Settings → "
        "Secrets**, paste the text above, and save. The app restarts on its "
        "own. Then sign in as **admin**."
    )
    st.caption("Keep a copy of the key in a password manager, not only here. "
               "Losing it means losing access to everything already stored.")

"""Instructor accounts, roles, and per-section ownership.

Several instructors can share one PeerParley deployment. Accounts live as an
encrypted blob in the same vault as everything else (`accounts.ppj`), so there's
no separate database and no plaintext password anywhere — passwords are stored
as salted PBKDF2-SHA256 hashes.

Roles:
  * instructor — sees and administers only the surveys they own.
  * admin      — sees and administers every instructor's surveys, and manages
                 accounts.

Break-glass admin: the app's existing shared password (`app_password_sha256`
in secrets) always authenticates as a built-in admin named `admin`, so you can
never be locked out even if the stored account list is empty or damaged. Sign in
with it, then add instructors from the admin panel.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Dict, Optional

from .vault import Vault

ACCOUNTS_KEY = "accounts.ppj"
_PBKDF2_ROUNDS = 200_000


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #
# Matches what the app already asked of an instructor changing their own
# password. Enforced here as well, because the account panel used to accept any
# temporary password an admin typed — including none at all.
MIN_PASSWORD = 8


def new_temp_password() -> str:
    """A one-off temporary password for a new account or a reset.

    These used to be the fixed string "PeerParley-Welcome", in every new
    account and every reset — and that string is in the source of a public
    repository. `must_change` only forces a new password *after* signing in, so
    anyone who knew an instructor's username could sign in first with the
    published password, set their own, lock the instructor out and read their
    students' records. A random one, shown once to the admin who made it,
    keeps the same hand-over with nothing anyone else can know.
    """
    import secrets as _secrets
    return _secrets.token_urlsafe(9)        # 12 characters, ~72 bits


def check_password(password: str) -> None:
    """Raise ValueError, in plain English, for a password not worth keeping."""
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"Use at least {MIN_PASSWORD} characters.")


def hash_password(password: str, salt: Optional[str] = None) -> Dict[str, str]:
    salt = salt or os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return {"salt": salt, "hash": dk.hex()}


def _verify_password(password: str, salt: str, expected_hash: str) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return hmac.compare_digest(dk.hex(), expected_hash)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def load_accounts(vault: Optional[Vault] = None) -> Dict[str, dict]:
    vault = vault or Vault()
    try:
        return json.loads(vault.get_bytes(ACCOUNTS_KEY).decode("utf-8"))
    except Exception:
        return {}


def save_accounts(accounts: Dict[str, dict], vault: Optional[Vault] = None) -> None:
    vault = vault or Vault()
    vault.put_bytes(ACCOUNTS_KEY, json.dumps(accounts, default=str).encode("utf-8"))


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def authenticate(username: str, password: str, cfg, vault: Optional[Vault] = None
                 ) -> Optional[dict]:
    """Return a session user dict {user, name, role, source} or None."""
    username = (username or "").strip()
    vault = vault or Vault()
    accounts = load_accounts(vault)

    acct = accounts.get(username)
    if acct and acct.get("active", True) and \
            _verify_password(password, acct.get("salt", ""), acct.get("hash", "")):
        return {"user": username, "name": acct.get("name", username),
                "role": acct.get("role", "instructor"), "source": "vault",
                "must_change": bool(acct.get("must_change", False))}

    # Break-glass shared-password admin (from secrets). The username must be
    # typed explicitly as "admin" — a blank username never signs anyone in.
    expected = (getattr(cfg, "app_password_sha256", "") or "").strip().lower()
    if username == "admin" and expected and \
            hmac.compare_digest(_sha256(password), expected):
        return {"user": "admin", "name": "Administrator", "role": "admin",
                "source": "secrets", "must_change": False}
    return None


def is_admin(user: Optional[dict]) -> bool:
    return bool(user) and user.get("role") == "admin"


# --------------------------------------------------------------------------- #
# Management (admin-only in the UI)
# --------------------------------------------------------------------------- #
def add_account(username: str, name: str, role: str, password: str,
                vault: Optional[Vault] = None, must_change: bool = True) -> None:
    username = (username or "").strip()
    if not username:
        raise ValueError("Username is required.")
    if username == "admin":
        raise ValueError("'admin' is reserved for the break-glass account.")
    check_password(password)
    vault = vault or Vault()
    accounts = load_accounts(vault)
    if username in accounts:
        raise ValueError(f"An account named '{username}' already exists.")
    accounts[username] = {"name": name or username,
                          "role": role if role in ("admin", "instructor") else "instructor",
                          "active": True, "must_change": must_change,
                          **hash_password(password)}
    save_accounts(accounts, vault)


def set_password(username: str, password: str, vault: Optional[Vault] = None,
                 must_change: bool = False) -> None:
    check_password(password)
    vault = vault or Vault()
    accounts = load_accounts(vault)
    if username not in accounts:
        raise ValueError("No such account.")
    accounts[username].update(hash_password(password))
    accounts[username]["must_change"] = must_change
    save_accounts(accounts, vault)


def set_role(username: str, role: str, vault: Optional[Vault] = None) -> None:
    vault = vault or Vault()
    accounts = load_accounts(vault)
    if username not in accounts:
        raise ValueError("No such account.")
    accounts[username]["role"] = "admin" if role == "admin" else "instructor"
    save_accounts(accounts, vault)


def set_active(username: str, active: bool, vault: Optional[Vault] = None) -> None:
    vault = vault or Vault()
    accounts = load_accounts(vault)
    if username not in accounts:
        raise ValueError("No such account.")
    accounts[username]["active"] = bool(active)
    save_accounts(accounts, vault)


def remove_account(username: str, vault: Optional[Vault] = None) -> None:
    vault = vault or Vault()
    accounts = load_accounts(vault)
    accounts.pop(username, None)
    save_accounts(accounts, vault)

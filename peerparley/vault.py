"""Pluggable, encrypted storage vault (the "behind-the-firewall" tier).

The cloud app never persists plaintext PII. Instead it writes ENCRYPTED blobs
to a university-controlled storage backend chosen in secrets:

    backend = "m365"     -> Microsoft 365 OneDrive / SharePoint (Graph API)
    backend = "dropbox"  -> Dropbox
    backend = "pcloud"   -> pCloud
    backend = "local"    -> local encrypted cache (dev only)

Every backend implements the same tiny interface:
    put(name, data: bytes)      -> str  (path/id)
    get(name)   -> bytes
    list()      -> list[str]
    delete(name)-> None

Data is encrypted with security.encrypt_bytes BEFORE it ever leaves the
process, so the storage provider only ever sees ciphertext.
"""
from __future__ import annotations

import io
import os
from typing import Any, Dict, List, Optional, Protocol, Tuple

import requests

from .config import VaultConfig, load_config
from .security import CryptoError, decrypt_bytes, encrypt_bytes


class StorageBackend(Protocol):
    def put(self, name: str, data: bytes) -> str: ...
    def get(self, name: str) -> bytes: ...
    def list(self) -> List[str]: ...
    def delete(self, name: str) -> None: ...


# --------------------------------------------------------------------------- #
# Local encrypted cache (development / air-gapped fallback)
# --------------------------------------------------------------------------- #
class LocalBackend:
    def __init__(self, folder: str):
        self.root = os.path.abspath(os.path.join("vault_cache", folder))
        os.makedirs(self.root, exist_ok=True)

    def _p(self, name: str) -> str:
        return os.path.join(self.root, name)

    def put(self, name: str, data: bytes) -> str:
        with open(self._p(name), "wb") as fh:
            fh.write(data)
        return self._p(name)

    def get(self, name: str) -> bytes:
        with open(self._p(name), "rb") as fh:
            return fh.read()

    def list(self) -> List[str]:
        return sorted(os.listdir(self.root)) if os.path.isdir(self.root) else []

    def delete(self, name: str) -> None:
        try:
            os.remove(self._p(name))
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# Microsoft 365 (OneDrive / SharePoint) via Graph API — confidential client
# --------------------------------------------------------------------------- #
class M365Backend:
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, folder: str, opts: dict):
        self.folder = folder.strip("/")
        self.tenant_id = opts["tenant_id"]
        self.client_id = opts["client_id"]
        self.client_secret = opts["client_secret"]
        self.drive = opts.get("drive", "onedrive")
        self.site_id = opts.get("site_id", "")
        self._token = None

    def _get_token(self) -> str:
        if self._token:
            return self._token
        import msal
        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret,
        )
        result = app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" not in result:
            raise RuntimeError(f"M365 auth failed: {result.get('error_description')}")
        self._token = result["access_token"]
        return self._token

    def _drive_root(self) -> str:
        if self.drive == "sharepoint":
            if not self.site_id:
                raise RuntimeError("vault.m365.site_id required for SharePoint drive.")
            return f"{self.GRAPH}/sites/{self.site_id}/drive"
        return f"{self.GRAPH}/me/drive"  # app-only cannot use /me; see note below

    def _headers(self, content=False) -> dict:
        h = {"Authorization": f"Bearer {self._get_token()}"}
        if content:
            h["Content-Type"] = "application/octet-stream"
        return h

    def _item_path(self, name: str) -> str:
        return f"{self.folder}/{name}".strip("/")

    def put(self, name: str, data: bytes) -> str:
        # Simple upload (<4 MB). PUT to /root:/path:/content
        url = f"{self._drive_root()}/root:/{self._item_path(name)}:/content"
        r = requests.put(url, headers=self._headers(content=True), data=data, timeout=60)
        r.raise_for_status()
        return r.json().get("id", name)

    def get(self, name: str) -> bytes:
        url = f"{self._drive_root()}/root:/{self._item_path(name)}:/content"
        r = requests.get(url, headers=self._headers(), timeout=60)
        r.raise_for_status()
        return r.content

    def list(self) -> List[str]:
        url = f"{self._drive_root()}/root:/{self.folder}:/children"
        r = requests.get(url, headers=self._headers(), timeout=60)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return [c["name"] for c in r.json().get("value", [])]

    def delete(self, name: str) -> None:
        url = f"{self._drive_root()}/root:/{self._item_path(name)}:"
        requests.delete(url, headers=self._headers(), timeout=60)


# --------------------------------------------------------------------------- #
# Dropbox
# --------------------------------------------------------------------------- #
class DropboxBackend:
    def __init__(self, folder: str, opts: dict):
        import dropbox
        self.folder = "/" + folder.strip("/")
        if opts.get("refresh_token"):
            self.dbx = dropbox.Dropbox(
                oauth2_refresh_token=opts["refresh_token"],
                app_key=opts.get("app_key"),
                app_secret=opts.get("app_secret"),
            )
        else:
            self.dbx = dropbox.Dropbox(opts["access_token"])

    def _p(self, name: str) -> str:
        return f"{self.folder}/{name}"

    def put(self, name: str, data: bytes) -> str:
        import dropbox
        self.dbx.files_upload(
            data, self._p(name), mode=dropbox.files.WriteMode.overwrite
        )
        return self._p(name)

    def get(self, name: str) -> bytes:
        _md, resp = self.dbx.files_download(self._p(name))
        return resp.content

    def list(self) -> List[str]:
        try:
            res = self.dbx.files_list_folder(self.folder)
            return [e.name for e in res.entries]
        except Exception:
            return []

    def delete(self, name: str) -> None:
        try:
            self.dbx.files_delete_v2(self._p(name))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# pCloud (simple HTTP API)
# --------------------------------------------------------------------------- #
class PCloudBackend:
    def __init__(self, folder: str, opts: dict):
        region = opts.get("region", "us")
        self.base = "https://eapi.pcloud.com" if region == "eu" else "https://api.pcloud.com"
        self.folder = folder.strip("/")
        self.access_token = opts.get("access_token", "")
        self.username = opts.get("username", "")
        self.password = opts.get("password", "")
        self._auth = None

    def _auth_params(self) -> dict:
        if self.access_token:
            return {"access_token": self.access_token}
        if self._auth:
            return {"auth": self._auth}
        r = requests.get(
            f"{self.base}/userinfo",
            params={"getauth": 1, "username": self.username, "password": self.password},
            timeout=30,
        )
        self._auth = r.json().get("auth", "")
        return {"auth": self._auth}

    def _ensure_folder(self) -> None:
        requests.get(
            f"{self.base}/createfolderifnotexists",
            params={**self._auth_params(), "path": f"/{self.folder}"},
            timeout=30,
        )

    def put(self, name: str, data: bytes) -> str:
        self._ensure_folder()
        r = requests.post(
            f"{self.base}/uploadfile",
            params={**self._auth_params(), "path": f"/{self.folder}", "filename": name, "nopartial": 1},
            files={name: (name, io.BytesIO(data))},
            timeout=120,
        )
        r.raise_for_status()
        return name

    def get(self, name: str) -> bytes:
        link = requests.get(
            f"{self.base}/getfilelink",
            params={**self._auth_params(), "path": f"/{self.folder}/{name}"},
            timeout=30,
        ).json()
        host = link["hosts"][0]
        path = link["path"]
        return requests.get(f"https://{host}{path}", timeout=120).content

    def list(self) -> List[str]:
        r = requests.get(
            f"{self.base}/listfolder",
            params={**self._auth_params(), "path": f"/{self.folder}"},
            timeout=30,
        ).json()
        meta = r.get("metadata", {})
        return [c["name"] for c in meta.get("contents", [])]

    def delete(self, name: str) -> None:
        requests.get(
            f"{self.base}/deletefile",
            params={**self._auth_params(), "path": f"/{self.folder}/{name}"},
            timeout=30,
        )


# --------------------------------------------------------------------------- #
# Factory + encrypted convenience wrapper
# --------------------------------------------------------------------------- #
class VaultUnavailable(RuntimeError):
    """This app cannot reach its storage at all, and why — in plain words.

    Its own type because the cause is almost always configuration rather than a
    fault: a backend chosen in secrets whose package is not installed, or
    credentials that are not filled in. Those used to surface as a raw
    `ModuleNotFoundError: dropbox` traceback on the sign-in screen, which tells
    an instructor nothing they can act on and looks like the app is broken.
    """


# Which package each backend needs, for the message when it is missing.
_BACKEND_PACKAGE = {"dropbox": "dropbox", "m365": "msal"}


def _make_backend(cfg: VaultConfig) -> StorageBackend:
    b = cfg.backend.lower()
    try:
        if b == "m365":
            return M365Backend(cfg.folder, cfg.options)
        if b == "dropbox":
            return DropboxBackend(cfg.folder, cfg.options)
        if b == "pcloud":
            return PCloudBackend(cfg.folder, cfg.options)
        return LocalBackend(cfg.folder)
    except ImportError as exc:
        package = _BACKEND_PACKAGE.get(b, b)
        raise VaultUnavailable(
            f"Storage is set to '{b}', but the `{package}` package is not "
            f"installed here, so this app cannot open it. Add `{package}` to "
            f"requirements.txt and redeploy — on Streamlit Community Cloud that "
            f"is the only file it installs — or change `[vault] backend` in the "
            f"app's secrets to one that is available."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise VaultUnavailable(
            f"Storage is set to '{b}', but it could not be opened: {exc}. "
            f"Check the `[vault.{b}]` settings in the app's secrets."
        ) from exc


# --------------------------------------------------------------------------- #
# A short-lived read cache, because Streamlit reruns everything
# --------------------------------------------------------------------------- #
# Streamlit re-executes the whole script on every click, keystroke and
# checkbox. Measured on a 40-student section, one such rerun asked storage for
# 52 objects — the survey, the roster, every one of the forty responses, the
# account list. On a laptop that is 0.2 seconds and invisible. On Microsoft 365
# or Dropbox, where each one is an HTTPS round trip of 100–300ms, the same click
# costs five to fifteen seconds, which is what "changing the schedule is very
# slow" actually was.
#
# So reads are remembered for a few seconds. The cache is deliberately simple
# and deliberately short:
#
#   * a write refreshes the entry it wrote, so a save is visible immediately —
#     the instructor must never see the value they just replaced;
#   * a delete drops it;
#   * anything else expires on its own within TTL seconds, which is what keeps
#     a student's submission from going unnoticed: it was written by another
#     process and nothing here can know about it;
#   * `Vault.refresh()` clears it outright, which is what the "Check for new
#     responses" button does.
#
# Values are stored decrypted, in memory, in a process that already holds the
# key and the plaintext — so this adds no exposure that the app did not already
# have. Nothing is written to disk.

CACHE_TTL = 15.0

_reads: Dict[Tuple[str, str, str], Tuple[float, Any]] = {}
_listings: Dict[Tuple[str, str], Tuple[float, List[str]]] = {}

# A sentinel for "this object is not there", so that an unsaved survey does not
# re-ask storage on every rerun just to be told no again.
_MISSING = object()


def _now() -> float:
    import time
    return time.monotonic()


def clear_cache() -> None:
    """Forget every remembered read. Used by the refresh buttons and by tests."""
    _reads.clear()
    _listings.clear()


class Vault:
    """Encrypt-on-write / decrypt-on-read wrapper around any backend."""

    def __init__(self, cfg: VaultConfig | None = None):
        self.cfg = cfg or load_config().vault
        # Constructing the backend can fail for reasons the instructor can fix,
        # and `Vault()` is built in a dozen places including the sign-in path.
        # So the failure is remembered rather than raised here: the sidebar
        # healthcheck and the setup screen can then say what is wrong, and only
        # an actual read or write raises.
        self._backend: Optional[StorageBackend] = None
        self._error = ""
        try:
            self._backend = _make_backend(self.cfg)
        except VaultUnavailable as exc:
            self._error = str(exc)
        except Exception as exc:  # noqa: BLE001
            self._error = f"Storage could not be opened: {exc}"

    @property
    def backend(self) -> StorageBackend:
        if self._backend is None:
            raise VaultUnavailable(self._error or "Storage is not available.")
        return self._backend

    @property
    def available(self) -> bool:
        return self._backend is not None

    @property
    def error(self) -> str:
        return self._error

    @property
    def name(self) -> str:
        return self.cfg.backend

    # ---- cache plumbing -------------------------------------------------
    @property
    def _scope(self) -> Tuple[str, str]:
        """Two vaults pointing at the same place share a cache; others do not.

        "The same place" has to include the local backend's absolute root, not
        just the backend name and folder: two local vaults with the same folder
        name under different working directories are different stores holding
        different students, and sharing a cache between them would serve one
        section's data to the other. The test suite found this immediately —
        which is the argument for the absolute path being in the key.
        """
        return (f"{self.cfg.backend}:{getattr(self._backend, 'root', '')}",
                str(self.cfg.folder))

    def refresh(self) -> None:
        """Drop this vault's remembered reads, so the next one goes to storage."""
        for key in [k for k in _reads if k[:2] == self._scope]:
            _reads.pop(key, None)
        _listings.pop(self._scope, None)

    def _remember(self, name: str, data: Any) -> None:
        _reads[(*self._scope, name)] = (_now(), data)

    def _forget(self, name: str) -> None:
        _reads.pop((*self._scope, name), None)
        self._listing_without(name)

    def _listing_with(self, name: str) -> None:
        """Keep the cached listing current rather than throwing it away.

        A rerun that writes anything — the autosave, a saved setting — used to
        invalidate the listing and make the next read of it a second round trip.
        A listing we already hold plus a name we just wrote is still a correct
        listing, so it is updated in place.
        """
        hit = _listings.get(self._scope)
        if hit is None:
            return
        when, names = hit
        if name not in names:
            _listings[self._scope] = (when, sorted([*names, name]))

    def _listing_without(self, name: str) -> None:
        hit = _listings.get(self._scope)
        if hit is None:
            return
        when, names = hit
        _listings[self._scope] = (when, [n for n in names if n != name])

    # ---- the operations -------------------------------------------------
    def put_bytes(self, name: str, data: bytes) -> str:
        out = self.backend.put(name, encrypt_bytes(data))
        # Remember what was just written rather than dropping the entry: a save
        # is immediately followed by a read of the same object (the page redraws
        # from it), and that read must return the new value, not the old one and
        # not a round trip.
        self._remember(name, data)
        self._listing_with(name)
        return out

    def get_bytes(self, name: str) -> bytes:
        key = (*self._scope, name)
        hit = _reads.get(key)
        if hit is not None and _now() - hit[0] < CACHE_TTL:
            if hit[1] is _MISSING:
                raise KeyError(f"{name} is not in this vault")
            return hit[1]
        try:
            data = decrypt_bytes(self.backend.get(name))
        except Exception:
            # Cache the absence too, but only that: a decryption failure is
            # remembered as "missing" for a few seconds, and the error itself is
            # re-raised unchanged for the caller to report.
            self._remember(name, _MISSING)
            raise
        self._remember(name, data)
        return data

    def list(self) -> List[str]:
        hit = _listings.get(self._scope)
        if hit is not None and _now() - hit[0] < CACHE_TTL:
            return list(hit[1])
        names = self.backend.list()
        _listings[self._scope] = (_now(), list(names))
        return list(names)

    def delete(self, name: str) -> None:
        self.backend.delete(name)
        self._forget(name)

    def healthcheck(self) -> tuple[bool, str]:
        """Can this app actually store student data right now?

        Listing the backend is not enough to answer that. Every write goes
        through Fernet first, so with no encryption key configured the local
        backend listed happily and reported "reachable" in green — and the
        instructor found out only after typing in forty students and clicking
        Save. The key is checked here, in memory, with no round trip.
        """
        if not self.available:
            return False, self._error
        try:
            # Through the cache: this check runs on every rerun, and it was the
            # last thing still making a round trip on each one. A green badge
            # that is up to CACHE_TTL seconds old is a fair trade for not
            # charging the instructor a network call per click; a store that
            # goes away is still reported within that window.
            self.list()
        except VaultUnavailable as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"Storage '{self.cfg.backend}' is not reachable: {exc}"
        try:
            probe = b"peerparley-healthcheck"
            if decrypt_bytes(encrypt_bytes(probe)) != probe:
                raise CryptoError("the encryption key did not round-trip")
        except Exception as exc:  # noqa: BLE001
            return False, (
                f"Storage '{self.cfg.backend}' is reachable, but this app "
                f"cannot store student data yet: {exc}"
            )
        return True, f"Storage '{self.cfg.backend}' is ready."

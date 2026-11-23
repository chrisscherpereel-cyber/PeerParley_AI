"""The cloud storage backends: every page of files, and a token that renews.

Reported (from another project's notes): "a recent upload to PeerParley through
the GitHub website replaced its storage code with an older copy. That undid
last week's fixes: Microsoft 365 only sees the first ~200 files again, its
sign-in expires after about an hour, and Dropbox only reads the first page of
files." These tests pin all three, so no later copy can bring them back
unnoticed. No network: the provider APIs are replaced with fakes.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from peerparley import vault as vmod  # noqa: E402


# --------------------------------------------------------------------------- #
# Microsoft 365
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self.content = status, body or {}, content

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeMsal:
    """msal.ConfidentialClientApplication, counting sign-ins."""
    issued = 0
    lifetime = 3599

    def __init__(self, *a, **k):
        pass

    def acquire_token_for_client(self, scopes):
        _FakeMsal.issued += 1
        return {"access_token": f"tok{_FakeMsal.issued}",
                "expires_in": _FakeMsal.lifetime}


@pytest.fixture()
def m365(monkeypatch):
    _FakeMsal.issued = 0
    _FakeMsal.lifetime = 3599
    monkeypatch.setitem(sys.modules, "msal",
                        types.SimpleNamespace(ConfidentialClientApplication=_FakeMsal))
    return vmod.M365Backend("PeerParley", {
        "tenant_id": "t", "client_id": "c", "client_secret": "s",
        "drive": "sharepoint", "site_id": "site"})


def test_m365_lists_every_page_not_just_the_first_200(m365, monkeypatch):
    pages = {f"p{i}": {"value": [{"name": f"f{i}_{j}.ppj"} for j in range(200)],
                       **({"@odata.nextLink": f"p{i + 1}"} if i < 2 else {})}
             for i in range(3)}
    seen = []

    def fake(method, url, **kw):
        seen.append(url)
        if "children" in url:
            return _Resp(body=pages["p0"])
        return _Resp(body=pages[url])
    monkeypatch.setattr(vmod.requests, "request", fake)
    names = m365.list()
    assert len(names) == 600 and names[-1] == "f2_199.ppj"
    assert seen[1:] == ["p1", "p2"]


def test_m365_renews_its_token_before_it_expires(m365, monkeypatch):
    monkeypatch.setattr(vmod.requests, "request",
                        lambda m, u, **k: _Resp(content=b"x"))
    clock = [1000.0]
    import time as _time
    monkeypatch.setattr(_time, "time", lambda: clock[0])
    m365.get("a.ppj")
    m365.get("b.ppj")
    assert _FakeMsal.issued == 1                     # reused while fresh
    clock[0] += 3599 - 200                           # inside the 5-minute margin
    m365.get("c.ppj")
    assert _FakeMsal.issued == 2                     # renewed, not expired


def test_m365_a_rejected_token_is_renewed_and_the_call_retried(m365, monkeypatch):
    calls = []

    def fake(method, url, headers=None, **kw):
        calls.append(headers["Authorization"])
        return _Resp(401) if len(calls) == 1 else _Resp(content=b"data")
    monkeypatch.setattr(vmod.requests, "request", fake)
    assert m365.get("a.ppj") == b"data"
    assert calls == ["Bearer tok1", "Bearer tok2"]


def test_m365_a_missing_folder_is_empty_but_other_errors_are_not(m365, monkeypatch):
    monkeypatch.setattr(vmod.requests, "request", lambda m, u, **k: _Resp(404))
    assert m365.list() == []
    monkeypatch.setattr(vmod.requests, "request", lambda m, u, **k: _Resp(500))
    with pytest.raises(RuntimeError):
        m365.list()


def test_m365_delete_reports_a_failure(m365, monkeypatch):
    monkeypatch.setattr(vmod.requests, "request", lambda m, u, **k: _Resp(404))
    m365.delete("gone.ppj")                          # already gone: fine
    monkeypatch.setattr(vmod.requests, "request", lambda m, u, **k: _Resp(403))
    with pytest.raises(RuntimeError):
        m365.delete("locked.ppj")


# --------------------------------------------------------------------------- #
# Dropbox
# --------------------------------------------------------------------------- #
def _dropbox_backend(pages, first_error=None):
    dropbox = pytest.importorskip("dropbox")
    b = vmod.DropboxBackend.__new__(vmod.DropboxBackend)
    b.folder = "/PeerParley"

    def page(i):
        return types.SimpleNamespace(
            entries=[types.SimpleNamespace(name=n) for n in pages[i]],
            has_more=i < len(pages) - 1, cursor=f"c{i + 1}")

    class _Dbx:
        def files_list_folder(self, folder):
            if first_error is not None:
                raise first_error
            return page(0)

        def files_list_folder_continue(self, cursor):
            return page(int(cursor[1:]))
    b.dbx = _Dbx()
    return b, dropbox


def test_dropbox_reads_every_page():
    pages = [[f"a{i}.ppj" for i in range(500)], [f"b{i}.ppj" for i in range(500)],
             ["c0.ppj"]]
    b, _ = _dropbox_backend(pages)
    names = b.list()
    assert len(names) == 1001 and names[-1] == "c0.ppj"


def test_dropbox_only_a_missing_folder_counts_as_empty():
    dropbox = pytest.importorskip("dropbox")
    from dropbox.files import ListFolderError, LookupError as DbxLookupError
    missing = dropbox.exceptions.ApiError(
        "rid", ListFolderError.path(DbxLookupError.not_found), "msg", "en")
    b, _ = _dropbox_backend([[]], first_error=missing)
    assert b.list() == []
    b, _ = _dropbox_backend([[]], first_error=OSError("network down"))
    with pytest.raises(OSError):
        b.list()                 # used to come back as "no files at all"

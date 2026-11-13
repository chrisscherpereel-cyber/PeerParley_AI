"""Microsoft 365 storage: every page of a folder listing is read, and the
sign-in token is renewed rather than kept forever (the shared vault lives as
long as the server process, and a Graph token expires after about an hour)."""
from __future__ import annotations


class _R:
    def __init__(self, body):
        self.body, self.status_code = body, 200

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def _backend(monkeypatch, pages, issued):
    import msal
    from peerparley import vault as vault_mod

    class FakeApp:
        def __init__(self, *a, **kw):
            pass

        def acquire_token_for_client(self, scopes):
            issued.append(1)
            return {"access_token": f"tok{len(issued)}"}

    monkeypatch.setattr(msal, "ConfidentialClientApplication", FakeApp)
    monkeypatch.setattr(vault_mod.requests, "get",
                        lambda url, **kw: _R(pages.get(url, pages["first"])))
    return vault_mod.M365Backend("PeerParley", {
        "tenant_id": "t", "client_id": "c", "client_secret": "s",
        "drive": "sharepoint", "site_id": "x"})


def test_m365_listing_follows_every_page(monkeypatch):
    pages = {"first": {"value": [{"name": f"f{i}"} for i in range(200)],
                       "@odata.nextLink": "https://graph/next"},
             "https://graph/next": {"value": [{"name": "f200"}]}}
    names = _backend(monkeypatch, pages, []).list()
    assert len(names) == 201 and names[-1] == "f200"


def test_m365_token_is_requested_from_msal_each_time(monkeypatch):
    issued = []
    b = _backend(monkeypatch, {"first": {"value": []}}, issued)
    b.list()
    b.list()
    # MSAL is asked every call; it serves its own cache and renews near expiry.
    assert len(issued) == 2


def test_dropbox_listing_follows_every_page():
    from peerparley.vault import DropboxBackend

    class Page:
        def __init__(self, names, more, cursor=None):
            self.entries = [type("E", (), {"name": n})() for n in names]
            self.has_more, self.cursor = more, cursor

    class Dbx:
        def files_list_folder(self, folder):
            return Page(["a", "b"], True, "c1")

        def files_list_folder_continue(self, cursor):
            return Page(["c"], False)

    b = DropboxBackend.__new__(DropboxBackend)
    b.folder, b.dbx = "/PeerParley", Dbx()
    assert b.list() == ["a", "b", "c"]

"""What the browser can tell us: this app's address, and the viewer's clock.

Both answers used to be typed in by hand, and both were a source of mistakes
worth removing:

* **The address.** Student links are built from it, and the box's default was a
  real URL belonging to another deployment — so the "have you entered your
  address?" check passed and forty students were emailed a link to somebody
  else's app. The browser already knows where it is.
* **The clock.** Open and close times were compared against the *server's*
  clock, which on Streamlit Community Cloud is UTC. An instructor in Arizona
  setting "closes Friday 5pm" was setting 5pm UTC — 10am their time. The
  browser knows its own offset, so the scheduling clock can start at the right
  answer instead of at the server's.

These live here, as plain functions over a context object, rather than inside
``app.py``: a helper buried in the Streamlit script cannot be tested, and the
last defect of that shape took a whole page down. Streamlit exposes the
browsing context only in recent versions and not at all in its test harness, so
every lookup is defensive and "we do not know" is always an allowed answer —
it just means the instructor fills the field in, as before.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional


def app_url(ctx: Any = None) -> str:
    """The address this page is open at, without query string or fragment.

    The query string is dropped deliberately: it carries this app's own state,
    and on a student's device it carries their personal token.
    """
    url = _attr(ctx, "url")
    if not url:
        url = _from_headers(ctx)
    url = str(url or "").split("?", 1)[0].split("#", 1)[0].strip().rstrip("/")
    return url if url.startswith(("http://", "https://")) else ""


def _from_headers(ctx: Any) -> str:
    """Rebuild the address from request headers, for older Streamlit."""
    try:
        headers = dict(_attr(ctx, "headers") or {})
    except Exception:  # noqa: BLE001
        return ""
    lower = {str(k).lower(): v for k, v in headers.items()}
    host = str(lower.get("host") or "")
    if not host:
        return ""
    proto = str(lower.get("x-forwarded-proto") or "")
    if not proto:
        proto = "http" if host.startswith(("localhost", "127.0.0.1", "0.0.0.0")) else "https"
    return f"{proto}://{host}"


def clock_offset_minutes(ctx: Any = None,
                         server_now: Optional[dt.datetime] = None) -> Optional[int]:
    """Minutes the viewer's clock runs ahead of this server's, or None.

    Streamlit reports the browser's ``getTimezoneOffset()``, which is minutes to
    *add to local time to reach UTC* — the opposite sign of what "ahead of the
    server" means, and a classic way to get this backwards. The server's own UTC
    offset is subtracted as well, so the answer is right whether the server runs
    in UTC (Streamlit Cloud) or in some other zone (a laptop).
    """
    raw = _attr(ctx, "timezone_offset")
    if raw is None:
        return None
    try:
        browser_from_utc = -int(raw)
    except (TypeError, ValueError):
        return None
    now = server_now or dt.datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    server_from_utc = int((now.utcoffset() or dt.timedelta(0)).total_seconds() // 60)
    return browser_from_utc - server_from_utc


def _attr(ctx: Any, name: str):
    """Read one attribute off the context, treating any failure as absence.

    Streamlit raises rather than returning None when there is no browsing
    context — in a test harness, a background thread, or an older version — and
    none of that should be able to reach the instructor as an error.
    """
    if ctx is None:
        return None
    try:
        return getattr(ctx, name, None)
    except Exception:  # noqa: BLE001
        return None

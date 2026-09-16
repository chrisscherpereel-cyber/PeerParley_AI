"""Autosaving the working dataset, so signing out doesn't undo an evening.

v2.2.0 made AI drafts durable and I called the problem solved. It wasn't. The
drafts were only one of six things that had to survive, and two defects made
even those unreachable:

* **The responses were never persisted.** ``long_df`` lived in
  ``st.session_state`` alone. The Vault tab's ``.ppx`` bundle held a copy, but
  only of ``long_df`` — ``self_evals`` (every self-rating) and ``roster`` (every
  name-to-email mapping) were not in it, so even a diligent manual save came
  back missing the self-evaluation column and the ability to email anyone.
* **The draft key moved.** Drafts are keyed by survey slug, and the slug is
  built from the course box, which resets to empty on sign-in. Drafts saved
  under ``Testing2-eval1`` were then looked for under ``section-eval1``. They
  were sitting in the vault the whole time, addressed by a name the app had
  forgotten how to ask for.

The second one is the instructive failure: persistence keyed to transient UI
state is not persistence. So the fix is not a better key — it is to make the
*workspace itself* durable, course name included, and restore it as a unit.

What is saved, per instructor:

    workspace__<user>.json     meta (course, eval number, counts, timestamp),
                               self_evals, roster
    workspace__<user>.frame    long_df, as parquet when available, else CSV

Both go through ``Vault.put_bytes``, which Fernet-encrypts before the bytes
leave the process — this is student PII and gets the same treatment as
everything else here. Two objects rather than one because a DataFrame does not
belong inside a JSON document; base64-ing a parquet file into a string field
would triple its size for no gain.

Saving is always best-effort. An autosave that could crash the page it is
protecting would be worse than no autosave.
"""
from __future__ import annotations

import datetime as dt
import io
import json
from typing import Any, Dict, Optional, Tuple

import pandas as pd

WORKSPACE_VERSION = 1


def workspace_key(username: str) -> str:
    return f"workspace__{(username or 'default').strip().lower()}.json"


def frame_key(username: str) -> str:
    return f"workspace__{(username or 'default').strip().lower()}.frame"


# --------------------------------------------------------------------------- #
# self_evals: tuple keys have to become strings for JSON
# --------------------------------------------------------------------------- #
#
# The mapping is {(team, name_key): {...}}. A tuple cannot be a JSON object key,
# and the separator has to be one that cannot appear in a team name or a name
# key — both of those are free text — or a round trip would silently split a key
# in the wrong place and attach one student's self-rating to another.

_SEP = "␟"   # SYMBOL FOR UNIT SEPARATOR: printable, and not typeable


def _encode_self_evals(self_evals: Optional[Dict]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in (self_evals or {}).items():
        if isinstance(key, (tuple, list)) and len(key) == 2:
            out[f"{key[0]}{_SEP}{key[1]}"] = value
        else:
            out[f"{_SEP}{key}"] = value      # already flat; keep it addressable
    return out


def _decode_self_evals(data: Optional[Dict]) -> Dict:
    out: Dict = {}
    for key, value in (data or {}).items():
        if _SEP in key:
            team, name = key.split(_SEP, 1)
            out[(team, name)] = value
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #

def _frame_to_bytes(df: pd.DataFrame) -> Tuple[bytes, str]:
    """Parquet when pyarrow is installed, CSV otherwise.

    The format is recorded alongside the bytes rather than guessed on read: a
    CSV written by a host without pyarrow must still load on one that has it.
    """
    buf = io.BytesIO()
    try:
        df.to_parquet(buf, index=False)
        return buf.getvalue(), "parquet"
    except Exception:  # noqa: BLE001 - pyarrow missing, or an unsupported dtype
        buf = io.BytesIO()
        df.to_csv(buf, index=False)
        return buf.getvalue(), "csv"


def _frame_from_bytes(raw: bytes, fmt: str) -> pd.DataFrame:
    buf = io.BytesIO(raw)
    if fmt == "parquet":
        try:
            return pd.read_parquet(buf)
        except Exception:  # noqa: BLE001
            buf.seek(0)
    return pd.read_csv(io.BytesIO(raw))


# --------------------------------------------------------------------------- #
# Save / load
# --------------------------------------------------------------------------- #

def save(vault: Any, username: str, *, long_df: Optional[pd.DataFrame],
         self_evals: Optional[Dict] = None, roster: Any = None,
         course: str = "", eval_no: str = "") -> Tuple[bool, str]:
    """Write the working dataset. Returns (saved, error). Never raises."""
    if long_df is None or not isinstance(long_df, pd.DataFrame) or long_df.empty:
        return False, "Nothing loaded yet, so there is nothing to autosave."
    try:
        raw, fmt = _frame_to_bytes(long_df)
        vault.put_bytes(frame_key(username), raw)

        by_key = {}
        if roster is not None:
            by_key = dict(getattr(roster, "by_key", {}) or {})

        meta = {
            "version": WORKSPACE_VERSION,
            "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
            "course": course or "",
            "eval_no": str(eval_no or ""),
            "frame_format": fmt,
            "rows": int(len(long_df)),
            "students": len(by_key),
            "self_evals": _encode_self_evals(self_evals),
            "roster": by_key,
        }
        vault.put_bytes(
            workspace_key(username),
            json.dumps(meta, default=str, ensure_ascii=False).encode("utf-8"),
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def load(vault: Any, username: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Read the working dataset back.

    Returns ``(state, error)``. ``(None, "")`` means nothing was ever saved,
    which is the ordinary first-run case and not worth reporting. A *present but
    broken* workspace does report, because the usual cause is a changed Fernet
    key and the instructor should hear that their saved session is unreadable
    rather than assume it was never there.
    """
    try:
        meta_raw = vault.get_bytes(workspace_key(username))
    except Exception:
        return None, ""
    try:
        meta = json.loads(meta_raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"A saved session exists but could not be read: {exc}"
    if not isinstance(meta, dict):
        return None, "A saved session exists but is not in the expected format."

    try:
        frame_raw = vault.get_bytes(frame_key(username))
        long_df = _frame_from_bytes(frame_raw, str(meta.get("frame_format", "csv")))
    except Exception as exc:  # noqa: BLE001
        return None, f"The saved responses could not be read back: {exc}"

    from .ingest import Roster
    roster = Roster()
    roster.by_key = dict(meta.get("roster") or {})

    return {
        "long_df": long_df,
        "self_evals": _decode_self_evals(meta.get("self_evals")),
        "roster": roster,
        "course": str(meta.get("course") or ""),
        "eval_no": str(meta.get("eval_no") or ""),
        "saved_at": str(meta.get("saved_at") or ""),
        "rows": int(meta.get("rows") or len(long_df)),
        "students": int(meta.get("students") or 0),
    }, ""


def clear(vault: Any, username: str) -> Tuple[bool, str]:
    try:
        for name in (workspace_key(username), frame_key(username)):
            try:
                vault.delete(name)
            except Exception:  # noqa: BLE001 - already gone is fine
                pass
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def describe(state: Dict[str, Any]) -> str:
    """One line for the restore prompt, so it says what it would bring back."""
    course = state.get("course") or "(no course)"
    when = state.get("saved_at") or "an earlier session"
    if when and "T" in when:
        try:
            when = dt.datetime.fromisoformat(when).strftime("%b %d at %I:%M %p")
        except ValueError:
            pass
    bits = [f"**{course} · Eval {state.get('eval_no') or '?'}**",
            f"{state.get('rows', 0)} evaluation rows"]
    if state.get("students"):
        bits.append(f"{state['students']} students")
    return " · ".join(bits) + f" · saved {when}"


def fingerprint(long_df: Optional[pd.DataFrame], course: str, eval_no: str) -> str:
    """Cheap change detector, so an idle rerun doesn't re-upload the frame."""
    import hashlib
    if long_df is None or not isinstance(long_df, pd.DataFrame):
        return ""
    parts = [course or "", str(eval_no or ""), str(len(long_df)),
             ",".join(map(str, long_df.columns))]
    try:
        # Content, not just shape: editing a comment has to trigger a save.
        parts.append(str(int(pd.util.hash_pandas_object(long_df, index=False).sum())))
    except Exception:  # noqa: BLE001
        pass
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Named bundles (the Vault tab's .ppx files)
# --------------------------------------------------------------------------- #
#
# A .ppx used to be an encrypted copy of ``long_df`` and nothing else. That is
# why a conscientious manual save still came back incomplete: the
# self-evaluations and the roster were never in the file, so the restored
# session had no self-rating column and no way to email anybody.
#
# A bundle now carries the whole working set, plus the AI drafts if there are
# any — the drafts are the expensive part, and a file called "everything I had"
# that silently omits them is the same mistake in a new place.
#
# Old bundles still load. The reader tries the new format first and falls back
# to "this is just an encrypted frame", because a file written last semester is
# exactly when this matters.

BUNDLE_MAGIC = "peerparley.bundle"
BUNDLE_VERSION = 1


def bundle_bytes(long_df: pd.DataFrame, self_evals: Optional[Dict] = None,
                 roster: Any = None, course: str = "", eval_no: str = "",
                 drafts: Optional[Dict[str, Any]] = None) -> bytes:
    """Serialize a complete working set. The Vault encrypts it on write."""
    import base64
    raw, fmt = _frame_to_bytes(long_df)
    by_key = dict(getattr(roster, "by_key", {}) or {}) if roster is not None else {}
    payload = {
        "magic": BUNDLE_MAGIC,
        "version": BUNDLE_VERSION,
        "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
        "course": course or "",
        "eval_no": str(eval_no or ""),
        "frame_format": fmt,
        # base64 because this one has to be a single named file the instructor
        # can point at; the workspace autosave keeps the frame separate instead.
        "frame_b64": base64.b64encode(raw).decode("ascii"),
        "rows": int(len(long_df)),
        "self_evals": _encode_self_evals(self_evals),
        "roster": by_key,
        "drafts": drafts or {},
    }
    return json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")


def read_bundle(raw: bytes) -> Tuple[Dict[str, Any], str]:
    """Parse bundle bytes. Returns (state, note).

    ``note`` explains what a legacy bundle could not carry, so loading one says
    what is missing rather than quietly handing back a thinner session.
    """
    import base64
    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("magic") != BUNDLE_MAGIC:
            raise ValueError("not a PeerParley bundle")
    except Exception:
        # Legacy: the whole file is an encrypted DataFrame.
        from .security import CryptoError  # noqa: F401 - clarity at the call site
        frame = _frame_from_bytes(raw, "parquet")
        return ({"long_df": frame, "self_evals": {}, "roster": None,
                 "course": "", "eval_no": "", "drafts": {},
                 "rows": int(len(frame)), "legacy": True},
                "This is an older bundle: it holds the responses only. "
                "Self-evaluations, the roster and any AI drafts were not saved "
                "in that format, so they are not restored.")

    from .ingest import Roster
    roster = Roster()
    roster.by_key = dict(payload.get("roster") or {})
    frame = _frame_from_bytes(
        base64.b64decode(payload.get("frame_b64") or ""),
        str(payload.get("frame_format", "csv")),
    )
    return ({"long_df": frame,
             "self_evals": _decode_self_evals(payload.get("self_evals")),
             "roster": roster,
             "course": str(payload.get("course") or ""),
             "eval_no": str(payload.get("eval_no") or ""),
             "drafts": dict(payload.get("drafts") or {}),
             "rows": int(payload.get("rows") or len(frame)),
             "saved_at": str(payload.get("saved_at") or ""),
             "legacy": False}, "")

"""Data ingestion: Qualtrics peer-eval exports + contact roster.

`parse_qualtrics_export` reads a real PeerParley/Qualtrics raw export (the 113-
column layout) and turns it into the SAME tidy `long_df` + `self_evals` the
built-in survey produces, so a Qualtrics upload grades identically to collected
responses — including dimension grades, forced-ranking performance, pay grade
and self-evaluation.

Qualtrics layout (per respondent):
  Q{2k}.1_1 .. Q{2k}.1_4   the four 1-7 agreement ratings for roster position k
  Q{2k+1}.1                "how to increase contribution"  (improve)  for k
  Q{2k+1}.2                "most significant contribution"  (contribution) for k
  Q22.1_k                  forced rank for k  (1 High, 2 Adequate, 3 Low)
  Q23.1_k                  dollars allocated to k
  Q24.1 / Q24.2            own contribution / confidential note
  Team, Section, Class, Team Member 1..10, Recipient{First,Last,Email}
"""
from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

MAX_TEAM = 10


# --------------------------------------------------------------------------- #
# Name normalization
# --------------------------------------------------------------------------- #
def normalize_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9,\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    if "," in s:
        parts = [p.strip() for p in s.split(",", 1)]
        if len(parts) == 2:
            s = f"{parts[1]} {parts[0]}".strip()
    return re.sub(r"\s+", " ", s).strip()


def name_key(name: str) -> str:
    """Order-independent key so 'First Last' == 'Last First'."""
    return " ".join(sorted(normalize_name(name).split()))


def _find(cols, needles) -> Optional[str]:
    low = {str(c).lower().replace("_", " ").strip(): c for c in cols}
    for n in needles:
        for k, orig in low.items():
            if n in k:
                return orig
    return None


# --------------------------------------------------------------------------- #
# File readers
# --------------------------------------------------------------------------- #
def read_table(file, filename: str = "") -> pd.DataFrame:
    name = (filename or getattr(file, "name", "")).lower()
    raw = file.read() if hasattr(file, "read") else file
    buf = io.BytesIO(raw) if isinstance(raw, (bytes, bytearray)) else raw
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(buf)
    # A contact list saved from Excel on Windows is usually cp1252, not UTF-8,
    # and one accented name was enough to end the upload in a raw
    # UnicodeDecodeError. Try the encodings people actually have, in order.
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            buf.seek(0)
        except Exception:  # noqa: BLE001 - a plain iterable has no seek
            pass
        try:
            return pd.read_csv(buf, encoding=encoding)
        except UnicodeDecodeError:
            continue
    # latin-1 maps every byte, so reaching here means the file is not a CSV
    # at all; let pandas raise the error that says so.
    try:
        buf.seek(0)
    except Exception:  # noqa: BLE001
        pass
    return pd.read_csv(buf)


def _raw_frame(file, filename: str = "") -> pd.DataFrame:
    """Read a raw export with NO header (every cell as string)."""
    name = (filename or getattr(file, "name", "")).lower()
    raw = file.read() if hasattr(file, "read") else file
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(raw) if isinstance(raw, (bytes, bytearray)) else raw,
                             header=None, dtype=object)
    return pd.read_csv(io.BytesIO(raw) if isinstance(raw, (bytes, bytearray)) else raw,
                       header=None, dtype=object, encoding="utf-8-sig")


def read_raw_export(file, filename: str = "") -> Tuple[List[str], pd.DataFrame]:
    """Return (codes, data_rows). Row 0 is the Qualtrics question-code row; the
    first data row is the first one whose StartDate (col 0) looks like a date."""
    df = _raw_frame(file, filename).reset_index(drop=True)
    codes = ["" if v is None else str(v).strip() for v in df.iloc[0].tolist()]

    def is_data(v):
        if isinstance(v, pd.Timestamp):
            return True
        s = str(v)
        return bool(re.search(r"\d{4}-\d{2}-\d{2}", s) or re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", s))

    start = 1
    for i in range(1, min(6, len(df))):
        if is_data(df.iloc[i, 0]):
            start = i
            break
    return codes, df.iloc[start:].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Roster (name_key -> contact record) — used for email matching
# --------------------------------------------------------------------------- #
@dataclass
class Roster:
    by_key: Dict[str, dict] = field(default_factory=dict)

    def match(self, name: str) -> Optional[dict]:
        return self.by_key.get(name_key(name))


# (A `from_df` constructor used to live here. Nothing called it, and it picked
# "First Name" as the full-name column — so every lookup through it missed.
# Rosters are built by `survey.roster_for_matching` and by the Qualtrics parser
# below, both of which key on the full name.)


def _f(v):
    try:
        f = float(v)
        return f if f == f else None   # drop NaN
    except (TypeError, ValueError):
        return None


def parse_qualtrics_export(file, filename: str = ""
                           ) -> Tuple[pd.DataFrame, Dict, Roster, dict]:
    """Parse a Qualtrics raw export into (long_df, self_evals, roster, report).

    long_df rows are one per (evaluator -> teammate) with points, comments,
    r0..r3 and rank — exactly what grading.compute consumes. self_evals maps
    (team, name_key) -> {ratings, rank}. roster gives name -> email for delivery.
    """
    codes, data = read_raw_export(file, filename)
    idx = {c: i for i, c in enumerate(codes)}

    def cell(row, code):
        i = idx.get(code)
        if i is None or i >= len(row):
            return ""
        v = row[i]
        if v is None:
            return ""
        try:
            if pd.isna(v):
                return ""
        except (TypeError, ValueError):
            pass
        return v

    rows: List[dict] = []
    self_evals: Dict = {}
    roster = Roster()

    for _, r in data.iterrows():
        row = list(r)
        first = str(cell(row, "RecipientFirstName")).strip()
        last = str(cell(row, "RecipientLastName")).strip()
        evaluator = f"{first} {last}".strip()
        if not evaluator:
            continue
        ev_key = name_key(evaluator)
        team = str(cell(row, "Team")).strip()
        email = str(cell(row, "RecipientEmail")).strip()
        roster.by_key.setdefault(ev_key, {"name": evaluator, "first": first, "last": last,
                                          "email": email, "team": team})

        members = [str(cell(row, f"Team Member {k}")).strip() for k in range(1, MAX_TEAM + 1)]
        members = [m for m in members if m and m.lower() != "nan"]
        conf = str(cell(row, "Q24.2") or "").strip()
        first_row = True

        for k, mate in enumerate(members, start=1):
            ratings = [_f(cell(row, f"Q{2*k}.1_{j}")) for j in range(1, 5)]
            rank = _f(cell(row, f"Q22.1_{k}"))
            alloc = _f(cell(row, f"Q23.1_{k}"))
            improve = str(cell(row, f"Q{2*k+1}.1") or "").strip()
            contribution = str(cell(row, f"Q{2*k+1}.2") or "").strip()

            if name_key(mate) == ev_key:  # self block → self-evaluation
                self_evals[(team, ev_key)] = {"ratings": ratings, "rank": rank,
                                              "self_contribution": str(cell(row, "Q24.1") or "")}
                continue

            pub = " ".join(t for t in (contribution, improve) if t).strip()
            entry = {
                "evaluator": evaluator, "evaluator_key": ev_key, "evaluator_team": team,
                "evaluatee": mate, "evaluatee_key": name_key(mate),
                "points": alloc, "public_comment": pub,
                "contribution_text": contribution, "improve_text": improve,
                "confidential_comment": conf if first_row else "",
                "rank": rank,
            }
            for j in range(4):
                entry[f"r{j}"] = ratings[j]
            rows.append(entry)
            first_row = False

    long_df = pd.DataFrame.from_records(rows)
    report = {"rows": len(long_df),
              "evaluators": int(long_df["evaluator_key"].nunique()) if not long_df.empty else 0,
              "teams": int(long_df["evaluator_team"].replace("", pd.NA).dropna().nunique())
              if not long_df.empty else 0,
              "students": len(roster.by_key)}
    return long_df, self_evals, roster, report

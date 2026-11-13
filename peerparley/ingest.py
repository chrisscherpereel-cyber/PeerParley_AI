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
# A file an instructor uploads was made by someone else's spreadsheet program,
# saved in whatever format that program chose. The release audit pushed thirty
# of those through the upload path and found two kinds of failure:
#
#   * five raised raw pandas errors — an empty file, a binary or a PDF with the
#     wrong extension, a corrupt workbook — and because the upload was not
#     guarded, each one replaced the whole page with a traceback;
#   * six were worse: they *read*, as nothing. Excel's "Unicode Text" format
#     (UTF-16, tab-separated), a European semicolon CSV, and a workbook whose
#     roster is on its second sheet all came back as zero students with no
#     word as to why, and the instructor saw an empty page.
#
# So every file goes through `_decode_table`, which tries the formats people
# actually have, and every failure becomes an `InputError` — a message written
# for the person who uploaded the file, which the app shows as it stands.


class InputError(ValueError):
    """A file that cannot be used, with a message meant for the instructor."""


_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")


def _as_bytes(file) -> bytes:
    raw = file.read() if hasattr(file, "read") else file
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return bytes(raw or b"")


def _decode_text(raw: bytes) -> str:
    """Bytes → text, in the encodings real spreadsheets are saved in."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")             # Excel "Unicode Text"
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _looks_binary(raw: bytes) -> bool:
    """A file that is not text at all, whatever its extension says."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return False
    sample = raw[:4096]
    if not sample:
        return False
    if sample.startswith((b"%PDF", b"PK\x03\x04", b"\xd0\xcf\x11\xe0")):
        return True
    controls = sum(1 for b in sample if b < 9 or 13 < b < 32)
    return controls > len(sample) * 0.05


def _sniff_separator(text: str) -> str:
    """Comma, semicolon or tab — whichever the header line actually uses.

    A spreadsheet in a European locale saves "CSV" with semicolons, because the
    comma is its decimal mark. Read with a comma, the whole header becomes one
    column and the team column cannot be found — which used to come back as
    three students "with no team" rather than as a format problem.
    """
    head = next((ln for ln in text.splitlines() if ln.strip()), "")
    counts = {sep: head.count(sep) for sep in (",", ";", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _decode_table(raw: bytes, filename: str, header) -> pd.DataFrame:
    """The one place a file becomes a frame. Raises InputError, nothing else."""
    name = (filename or "").lower()
    if not raw.strip():
        raise InputError("That file is empty. Check you uploaded the saved "
                         "file, not a new blank one.")
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        try:
            sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None,
                                   header=header, dtype=object)
        except Exception as exc:  # noqa: BLE001
            raise InputError(
                "That file has a spreadsheet's name but could not be opened as "
                "one. Open it in Excel and use Save As → Excel Workbook "
                f"(.xlsx), then upload it again. ({type(exc).__name__})"
            ) from exc
        # A workbook often has a notes or instructions sheet first. Take the
        # sheet with the most filled-in cells rather than whichever is first.
        filled = {n: int(df.notna().sum().sum()) for n, df in sheets.items()}
        if not filled or max(filled.values()) == 0:
            raise InputError("Every sheet in that workbook is empty.")
        return sheets[max(filled, key=filled.get)]

    if _looks_binary(raw):
        raise InputError(
            "That file is not a spreadsheet or a CSV — it looks like "
            + ("a PDF" if raw[:4] == b"%PDF" else
               "a Word or Excel file renamed to .csv" if raw[:2] == b"PK" else
               "a binary file")
            + ". Save it from Excel or Google Sheets as CSV or .xlsx and upload "
              "that instead.")
    text = _decode_text(raw)
    sep = _sniff_separator(text)
    try:
        return pd.read_csv(io.StringIO(text), sep=sep, header=header,
                           dtype=object if header is None else None,
                           skip_blank_lines=True)
    except pd.errors.EmptyDataError as exc:
        raise InputError("That file has no rows in it.") from exc
    except Exception as exc:  # noqa: BLE001
        raise InputError(
            "That file could not be read as a CSV — some rows have a different "
            "number of columns from the header. Open it in Excel or Google "
            "Sheets and save it again as CSV. "
            f"({type(exc).__name__}: {str(exc)[:120]})") from exc


def read_table(file, filename: str = "") -> pd.DataFrame:
    """A contact list, as a frame with its header row. Raises InputError.

    Blank rows and the unnamed empty columns Excel leaves after the last real
    one are dropped here, so nothing downstream mistakes them for students.
    """
    name = filename or getattr(file, "name", "")
    df = _decode_table(_as_bytes(file), name, header=0)
    # A cell holding only spaces is empty. The contact-list template's formula
    # columns leave a space in every unused row, so without this the template
    # read as a hundred rows long; the unused ones are dropped here.
    df = df.apply(lambda col: col.map(
        lambda v: None if isinstance(v, str) and not v.strip() else v))
    df = df.map(lambda v: None if type(v).__module__.startswith("openpyxl")
                else v) if hasattr(df, "map") else df
    df = df.dropna(how="all")
    df = df.loc[:, [c for c in df.columns
                    if not (str(c).startswith("Unnamed:")
                            and df[c].isna().all())]]
    if df.empty:
        raise InputError("That file has a header row but no students under "
                         "it.")
    return df.reset_index(drop=True)
def _raw_frame(file, filename: str = "") -> pd.DataFrame:
    """A raw export with NO header, every cell as text. Raises InputError.

    Through the same decoder as a contact list, so an export saved by Excel on
    Windows, or as UTF-16, reads instead of ending in a UnicodeDecodeError.
    """
    name = filename or getattr(file, "name", "")
    return _decode_table(_as_bytes(file), name, header=None)


def read_raw_export(file, filename: str = "") -> Tuple[List[str], pd.DataFrame]:
    """Return (codes, data_rows). Row 0 is the Qualtrics question-code row; the
    first data row is the first one whose StartDate (col 0) looks like a date."""
    df = _raw_frame(file, filename).reset_index(drop=True)
    if df.empty:
        raise InputError("That export has no rows in it.")
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
    # A contact list, or a Qualtrics export of some other survey, uploaded
    # here used to parse "successfully" into zero rows — and the Results tab
    # then had nothing to say about why. None of the peer-evaluation question
    # codes being present is not an empty evaluation; it is the wrong file.
    known = [c for c in codes
             if re.fullmatch(r"Q(2[23]\.1_\d+|\d+\.1_[1-4]|\d+\.[12])", c)]
    if not known:
        looks_contact = any(re.search(r"(?i)first|last|email|team", c)
                            for c in codes)
        raise InputError(
            "That file is not a PeerParley Qualtrics export — none of the "
            "peer-evaluation questions (Q2.1_1, Q22.1_1, Q23.1_1 …) are in its "
            "header."
            + (" It looks like a contact list: upload that under "
               "**Contact list** above instead." if looks_contact else
               " In Qualtrics, use Data & Analysis → Export → CSV with "
               "\"Download all fields\" and \"Use numeric values\"."))

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

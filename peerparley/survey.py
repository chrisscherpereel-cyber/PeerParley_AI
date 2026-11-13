"""Built-in survey: set up, distribute links, collect responses.

This is the "setup / administer" half of PeerParley. Instead of exporting a
Qualtrics survey and re-uploading the results, the instructor uploads only the
**contact list** (names, emails, teams); PeerParley then serves each student a
personal evaluation form and stores the sealed responses in the same
firewall-side vault the rest of the app uses. Collected responses are turned
into the exact tidy `long_df` the grading engine already consumes, so the
Review / PDF / Email tabs work unchanged.

Storage layout (all encrypted by the Vault before leaving the process):

    survey__<slug>.ppj     the survey wording/config for one course+eval
    roster__<slug>.ppj     the contact list snapshot (teams + members + ids)
    resp__<slug>__m<id>.ppj            one student's submission

`<slug>` is derived from the course label + evaluation number, so two sections
or two rounds never collide.

**A student is identified by a permanent id**, assigned when they first appear
in a contact list and kept for as long as the survey exists. The id is what a
link token carries, what a submission is filed under, and what the answers
inside a submission refer to.

It did not used to be. Until 2.16.0 a student was their *position* — their
index in their team's list in the stored snapshot — and submissions were filed
as `resp__<slug>__<team>__p<pos>.ppj` with the answers inside keyed by the
teammate's index. That made the roster the one thing that could not be touched
once links went out: adding a student named "Adams" to a team renumbered
everyone after them, so the links already sent pointed at the wrong students
and the stored answers described the wrong people. Nothing errored; the grades
were just wrong. `roster_shift` existed to warn an instructor off doing it.

With ids, positions are presentational. A student can be added, moved between
teams or removed and nobody else is disturbed: their teammates' links keep
working and their teammates' answers keep meaning what they meant. Old
positional links are still honoured (`resolve_token`), and a survey stored
under the old scheme is migrated on sight (`migrate_roster`).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .config import load_config
from .ingest import Roster, name_key
from .tokens import make_token, read_token
from .vault import Vault


# --------------------------------------------------------------------------- #
# Survey definition
# --------------------------------------------------------------------------- #
SCALE_LABELS = ["Strongly agree", "Agree", "Somewhat agree",
                "Neither agree nor disagree", "Somewhat disagree",
                "Disagree", "Strongly disagree"]  # 7 → 1

RATING_STATEMENTS = [
    "I consider him/her a team player.",
    "He/she did his/her share of the work on the team.",
    "He/she contributed high quality of work to the team.",
    "The team performed well because of this individual.",
]

RANK_CATEGORIES = ["High Performer", "Adequate Performer", "Low Performer"]

DEFAULT_SURVEY: Dict = {
    "title": "Peer Evaluation",
    "intro": ("You will evaluate each member of your team — including yourself. "
              "For each person, rate the statements, add brief anonymous feedback, "
              "rank their contribution, and split $100 across the team."),

    # ---- Introduction / header ----
    "show_header": True,

    # ---- Rating matrix (4 statements, 7-point agree/disagree) ----
    "ask_ratings": True,
    "ratings_prompt": ("For each team member (including yourself) please indicate the "
                       "degree to which you disagree or agree with the following statements."),
    "rating_statements": list(RATING_STATEMENTS),
    "scale_labels": list(SCALE_LABELS),

    # ---- Qualitative feedback (two questions per member) ----
    "ask_improve": True,
    "improve_prompt": ("If you were {member}, what specific things would you do to "
                       "increase your contribution to the team? (Shared anonymously.)"),
    "ask_contribution": True,
    "contribution_prompt": ("If you were {member}, what would you identify as your most "
                            "significant contributions to the team? (Shared anonymously.)"),

    # ---- Forced ranking ----
    "ask_ranking": True,
    "ranking_prompt": ("Rank each team member's contribution to the team's performance, "
                       "including yourself. Use every category at least once."),
    "ranking_categories": list(RANK_CATEGORIES),

    # ---- Pay allocation ----
    # Minutes the instructor's clock runs ahead of the server's. None means
    # nobody has decided yet, so the app can adopt the browser's own offset the
    # first time the schedule panel is opened; an explicit 0 means "use the
    # server's clock", which is what every survey saved before 2.11 did and what
    # a missing key still means.
    "clock_offset_minutes": None,

    "ask_allocation": True,
    # The $100 is shared among TEAMMATES, not the whole team including yourself.
    # That is what the grader assumes (an even share is 100/(n-1)), and money
    # kept back for yourself was being discarded — which quietly rewarded
    # withholding, because every dollar not given out lowered a teammate at no
    # cost to the student holding it.
    "allocation_prompt": ("You have been given $100 to pay your teammates for their "
                          "contribution to the project. Allocate a portion to each of them "
                          "(consider both performance and effort). It must total exactly "
                          "$100. You are not paying yourself."),
    "points_total": 100,

    # ---- Your own contribution (released anonymously) ----
    "ask_self_contribution": True,
    "self_contribution_prompt": ("Briefly describe YOUR significant contributions to the "
                                 "team's performance. (Shared anonymously.)"),

    # ---- Confidential note to the instructor ----
    "ask_confidential": True,
    "confidential_prompt": ("Any additional comments for the instructor about your team's "
                            "performance? (Confidential — not released.)"),

    # ---- Scheduling ----
    "is_open": True,          # master switch — off = closed regardless of dates
    "opens_at": "",           # ISO datetime; blank = open as soon as is_open is on
    "closes_at": "",          # ISO datetime; blank = no automatic close
    "closed_note": "This peer evaluation is now closed. Thank you.",

    # ---- What the STUDENT feedback PDF shows (instructor-controlled) ----
    "report": {
        "performance": True,        # High/Adequate/Low pill
        "grade_adjustment": True,   # the +/- % vs team score meter
        "dimensions": True,         # rating meters + letter grades
        "pay_grade": True,          # pay grade line
        "valued": True,             # "What your teammates valued"
        "focus": True,              # "Where to focus next"
        "response_quality": True,   # "The feedback you gave" (Q + points)
        "narrative": True,          # v2: the approved AI summary, when one exists
    },
}

REPORT_DEFAULTS = dict(DEFAULT_SURVEY["report"])


# --------------------------------------------------------------------------- #
# Open / close scheduling
# --------------------------------------------------------------------------- #
def parse_dt(value):
    """Parse an ISO datetime string into a naive datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


# How far the instructor's clock is ahead of the server's, in minutes. Stored
# with the survey, so a schedule means the same thing whoever opens the app and
# wherever the app happens to be running.
#
# Why this exists: open and close times were compared against the *server's*
# clock, which on Streamlit Community Cloud is UTC. An instructor in Arizona
# typing "closes Friday 5pm" was setting 5pm UTC — 10am their time, so the
# survey shut at mid-morning on Friday with nothing on screen having said so.
# The panel printed the server clock, which disclosed the problem without
# solving it.
CLOCK_KEY = "clock_offset_minutes"


def clock_offset(survey_cfg: Optional[Dict] = None) -> int:
    """Minutes to add to the server clock to get the instructor's wall clock."""
    try:
        return int((survey_cfg or {}).get(CLOCK_KEY, 0) or 0)
    except (TypeError, ValueError):
        return 0


def local_now(survey_cfg: Optional[Dict] = None) -> datetime:
    """Now, on the clock the instructor set. Every schedule is read against it."""
    return datetime.now() + timedelta(minutes=clock_offset(survey_cfg))


def offset_from_wall_clock(stated: datetime,
                           server_now: Optional[datetime] = None,
                           round_to: int = 5) -> int:
    """The offset implied by "it is <stated> where I am", in minutes.

    Rounded to five minutes because the instructor is typing a wall-clock time,
    not timing a stopwatch: the difference between a stated 5:00 and a server
    4:58:37 is their typing speed, not a real 1m23s offset. Every real zone
    offset is a multiple of fifteen minutes, so five is finer than it needs to
    be and still absorbs the noise.
    """
    server_now = server_now or datetime.now()
    delta = (stated - server_now).total_seconds() / 60.0
    step = max(1, int(round_to))
    return int(round(delta / step)) * step


def describe_clock(survey_cfg: Optional[Dict] = None) -> str:
    """One line for the instructor: what time this survey thinks it is."""
    offset = clock_offset(survey_cfg)
    now = local_now(survey_cfg)
    if offset == 0:
        return f"Scheduling clock: **{now:%b %d, %Y %I:%M %p}** (this server's own time)."
    hours = offset / 60.0
    sign = "ahead of" if offset > 0 else "behind"
    return (f"Scheduling clock: **{now:%b %d, %Y %I:%M %p}** — "
            f"{abs(hours):g} hour(s) {sign} this server's clock.")


def window_state(survey_cfg: Dict):
    """Return one of 'open', 'not_yet', 'closed', 'disabled' for the survey now.

    Times are compared against the clock the instructor set (the server's own
    clock when they have not set one). The master `is_open` switch, when off,
    closes the survey regardless of dates.
    """
    if not survey_cfg.get("is_open", True):
        return "disabled"
    now = local_now(survey_cfg)
    opens = parse_dt(survey_cfg.get("opens_at"))
    closes = parse_dt(survey_cfg.get("closes_at"))
    if opens and now < opens:
        return "not_yet"
    if closes and now > closes:
        return "closed"
    return "open"


def window_message(survey_cfg: Dict) -> str:
    """A student-facing sentence describing the current window."""
    state = window_state(survey_cfg)
    if state == "not_yet":
        opens = parse_dt(survey_cfg.get("opens_at"))
        return (f"This peer evaluation opens on {opens:%b %d, %Y at %I:%M %p}. "
                "Please come back then.") if opens else "This peer evaluation isn't open yet."
    if state in ("closed", "disabled"):
        return survey_cfg.get("closed_note", "This peer evaluation is now closed. Thank you.")
    closes = parse_dt(survey_cfg.get("closes_at"))
    return (f"Open now — closes {closes:%b %d, %Y at %I:%M %p}." if closes
            else "Open now.")


# --------------------------------------------------------------------------- #
# Slugs, secrets, storage keys
# --------------------------------------------------------------------------- #
def slugify(course: str, eval_no: str) -> str:
    base = f"{course or 'section'}_eval{eval_no or '1'}"
    return re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-") or "section-eval1"


def token_secret(cfg=None) -> str:
    """The HMAC secret for links. Prefer an explicit token_secret; otherwise
    derive one deterministically from the Fernet key so links work out of the
    box wherever the app is already configured to encrypt."""
    cfg = cfg or load_config()
    explicit = (getattr(cfg, "token_secret", "") or "").strip()
    if explicit:
        return explicit
    import hashlib
    seed = (getattr(cfg, "fernet_key", "") or "peerparley").encode("utf-8")
    return hashlib.sha256(b"peerparley-links:" + seed).hexdigest()


def _safe(s) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", str(s)).strip("-") or "x"


def key_survey(slug: str) -> str:
    return f"survey__{slug}.ppj"


def key_roster(slug: str) -> str:
    return f"roster__{slug}.ppj"


def key_response(slug: str, member_id: str) -> str:
    """Where one student's submission lives, by their permanent id."""
    return f"resp__{slug}__m{_safe(member_id)}.ppj"


def key_response_legacy(slug: str, team: str, pos: int) -> str:
    """The pre-2.16.0 positional name. Read, and migrated; never written."""
    return f"resp__{slug}__{_safe(team)}__p{pos}.ppj"


def _resp_prefix(slug: str) -> str:
    """Shared by both names, so one listing finds submissions either way."""
    return f"resp__{slug}__"


# --------------------------------------------------------------------------- #
# Student identity
# --------------------------------------------------------------------------- #
# Random rather than derived from the name, because every derivable thing about
# a student is also a thing an instructor fixes: a misspelt surname, a personal
# address swapped for a university one. Deriving the id from any of those means
# correcting a typo silently creates a new student and orphans their answers.
# A random id is assigned once and then *matched* onto later uploads of the
# contact list by name, falling back to email — see `build_teams`.
MEMBER_ID_BYTES = 8


def new_member_id() -> str:
    import secrets
    return secrets.token_hex(MEMBER_ID_BYTES)


def snapshot_members(snap: Optional[dict]) -> List[Tuple[str, int, dict]]:
    """Every member of a snapshot as (team, position, record).

    Unassigned students come back with a team of "" — they are on the roster
    and must stay visible, but they have no teammates to evaluate.
    """
    out: List[Tuple[str, int, dict]] = []
    for team, members in ((snap or {}).get("teams", {}) or {}).items():
        for pos, m in enumerate(members or []):
            out.append((team, pos, m))
    for pos, m in enumerate((snap or {}).get("unassigned", []) or []):
        out.append(("", pos, m))
    return out


def member_by_id(snap: Optional[dict], member_id: str) -> Optional[dict]:
    """The record for one id, with its team — or None."""
    if not member_id:
        return None
    for team, pos, m in snapshot_members(snap):
        if str(m.get("id", "")) == str(member_id):
            return {**m, "team": team, "pos": pos}
    return None


def find_member(snap: Optional[dict], name: str = "", email: str = "") -> Optional[dict]:
    """A member matched by name, else by email. How ids survive a re-upload."""
    key = name_key(name) if name else ""
    mail = (email or "").strip().lower()
    if key:
        for team, pos, m in snapshot_members(snap):
            if name_key(m.get("name", "")) == key:
                return {**m, "team": team, "pos": pos}
    if mail:
        for team, pos, m in snapshot_members(snap):
            if (m.get("email", "") or "").strip().lower() == mail:
                return {**m, "team": team, "pos": pos}
    return None


def resolve_token(payload: Optional[dict], snap: Optional[dict]) -> str:
    """The member id a link token means, or "".

    Tokens issued from 2.16.0 carry the id itself (`i`). Ones issued before it
    carry a team and a position (`t`, `p`), and are resolved against the stored
    snapshot — so every link already in a student's inbox keeps working. A
    positional link whose position no longer holds the student it was issued
    for resolves to nothing rather than to whoever is standing there now: that
    is the failure the id scheme exists to prevent, and it must not be
    reintroduced by the compatibility path.
    """
    if not payload:
        return ""
    direct = str(payload.get("i", "") or "")
    if direct:
        return direct if member_by_id(snap, direct) else ""
    team = str(payload.get("t", "") or "")
    try:
        pos = int(payload.get("p", -1))
    except (TypeError, ValueError):
        return ""
    members = ((snap or {}).get("teams", {}) or {}).get(team, []) or []
    if 0 <= pos < len(members):
        issued = members[pos]
        # Only honour it if that seat has not been renumbered since. The
        # snapshot records who each position was issued to at migration time.
        issued_to = str(issued.get("issued_pos", pos))
        if str(pos) == issued_to:
            return str(issued.get("id", "") or "")
    return ""


# --------------------------------------------------------------------------- #
# Encrypted JSON helpers (Vault already encrypts on write / decrypts on read)
# --------------------------------------------------------------------------- #
def _save_json(vault: Vault, name: str, obj) -> None:
    vault.put_bytes(name, json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8"))


def _load_json(vault: Vault, name: str):
    return json.loads(vault.get_bytes(name).decode("utf-8"))


# --------------------------------------------------------------------------- #
# Roster snapshot (teams + ordered members) from the contact list
# --------------------------------------------------------------------------- #
def _contact_columns(df: pd.DataFrame):
    """Resolve name/first/last/email/team columns from a contact list.

    Deliberately stricter than ingest.Roster's fuzzy matcher: a standalone
    full-name column is matched exactly (so a "First Name" column is not
    mistaken for the full name), while first/last are matched on their own
    unambiguous substrings.
    """
    low = {str(c).lower().strip(): c for c in df.columns}

    def exact(cands):
        for cand in cands:
            if cand in low:
                return low[cand]
        return None

    def contains(cands):
        for cand in cands:
            for k, orig in low.items():
                if cand in k:
                    return orig
        return None

    full = exact(["name", "full name", "fullname", "student name", "student"])
    first = contains(["first name", "firstname", "first"])
    last = contains(["last name", "lastname", "last"])
    email = contains(["primaryemail", "email", "e-mail"])
    # The team column, by exact header first. A substring match alone took the
    # first column containing "team" — and the contact-list template has
    # "Number on Team" and "Team Member 1…6" beside "Team". With those columns
    # in another order, every student landed on a team named after its size.
    team = exact(["team", "team name", "team number", "team #", "team no",
                  "team no.", "group", "group name", "group number"])
    if not team:
        for k, orig in low.items():
            if ("team" in k or "group" in k) and not any(
                    bad in k for bad in ("member", "number on", "size",
                                         "count", "how many", "mates")):
                team = orig
                break
    klass = exact(["class", "course", "class name"]) or contains(["class", "course"])
    section = exact(["section", "sec"]) or contains(["section"])
    return full, first, last, email, team, klass, section


def _cell(row, column) -> str:
    """One contact-list cell as clean text — "" for anything empty.

    It used to be `str(row.get(column, "")).strip()`, and `str()` of a missing
    pandas value is the text "nan". So a row with no name but anything at all in
    another column became a student called **"nan nan"** — and the built-in
    contact-list template guarantees exactly that, because its formula columns
    run down a hundred rows and leave a space in every unused one. The release
    audit found it by uploading the template as shipped.

    A whole-number float is written as a whole number, because a spreadsheet
    column of team numbers reads as floats and the teams used to be labelled
    "1.0", "2.0" on screen and in every student's email.
    """
    if not column:
        return ""
    value = row.get(column, "")
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "nat") else text


# The example row that ships in assets/ContactList_Template.xlsx.
_TEMPLATE_EXAMPLE = {("aaa", "bbb", "bbb@ccc.com")}


def contact_list_problems(contact_df: pd.DataFrame) -> Tuple[List[str], List[str]]:
    """What is wrong with a contact list, before anything is built from it.

    Returns ``(errors, warnings)``. An error means nothing useful can be made
    from the file; a warning means it will work but not the way the instructor
    probably expects. Both used to be silent: a list with no name column came
    back as zero students, and one with no team column as everybody on no
    team — and the page offered no reason for either.
    """
    errors: List[str] = []
    warnings: List[str] = []
    if contact_df is None or contact_df.empty:
        return ["That file has no students in it."], []
    full, first, last, email, team, _klass, _section = _contact_columns(contact_df)
    shown = ", ".join(f"“{c}”" for c in list(contact_df.columns)[:8])
    if not (full or first or last):
        errors.append(
            "No name column was found. PeerParley looks for a column headed "
            "“First Name” and “Last Name”, or a single “Name” — this file's "
            f"columns are {shown}. Rename one and upload it again, or start "
            "from the template above.")
    if not team:
        warnings.append(
            "No team column was found, so every student is on the roster with "
            "no team. Add a column headed “Team”, or put students on teams "
            "afterwards under **👥 Teams**.")
    if not email:
        warnings.append(
            "No email column was found. Links can still be built and copied, "
            "but PeerParley cannot email them to anyone.")
    # The template's own example student, left in by mistake, becomes a real
    # student with a real link — and a teammate everybody on team 1 is asked
    # to evaluate. Said, not silently dropped: it is the instructor's file.
    if first and last:
        for _, row in contact_df.iterrows():
            sample = (_cell(row, first).lower(), _cell(row, last).lower(),
                      _cell(row, email).lower() if email else "")
            if sample in _TEMPLATE_EXAMPLE:
                warnings.append(
                    "The template's example student (**AAA BBB**, "
                    "BBB@CCC.com) is still in this file, so it will be "
                    "treated as a real student. Delete that row and upload "
                    "the file again.")
                break
    return errors, warnings


def build_roster(contact_df: pd.DataFrame,
                 existing: Optional[dict] = None) -> Dict[str, Any]:
    """Turn a contact list into a roster snapshot body: teams and unassigned.

    Returns ``{"teams": {team: [ordered records]}, "unassigned": [records]}``.
    A record is ``{id, name, first, last, email, class, section}``; positions
    are the index within a team, sorted by name so the order is stable.

    `existing` is the snapshot already stored, if any. Every student in it is
    matched onto this upload by name, then by email, and **keeps their id** —
    which is what makes re-uploading a corrected contact list safe. Without
    that, fixing one misspelt surname would mint a new student and orphan every
    answer already attached to the old one.

    Students with no team, and students who are the only member of theirs, go
    to `unassigned` rather than being dropped. They used to be dropped — a team
    of one has nobody to evaluate, which is true, but the student was then
    simply absent from the app with nothing said, and an instructor fixing a
    blank Team cell had no way to see who needed one.
    """
    full_c, first_c, last_c, email_c, team_c, class_c, section_c = _contact_columns(contact_df)
    teams: Dict[str, List[dict]] = {}
    loose: List[dict] = []
    seen = set()
    for _, row in contact_df.iterrows():
        first = _cell(row, first_c)
        last = _cell(row, last_c)
        name = _cell(row, full_c) or f"{first} {last}".strip()
        if not name:
            continue
        team = _cell(row, team_c)
        key = (team, name_key(name))
        if key in seen:
            continue
        seen.add(key)
        email = _cell(row, email_c)
        prior = find_member(existing, name, email)
        record = {
            "id": (prior or {}).get("id") or new_member_id(),
            "name": name,
            "first": first or name.split(" ")[0],
            "last": last or name.split(" ")[-1],
            "email": email,
            "class": _cell(row, class_c),
            "section": _cell(row, section_c),
        }
        (teams.setdefault(team, []) if team else loose).append(record)

    ordered: Dict[str, List[dict]] = {}
    for team, members in teams.items():
        members.sort(key=lambda m: name_key(m["name"]))
        if len(members) >= 2:
            ordered[team] = members
        else:
            loose.extend(members)       # a team of one: visible, not vanished
    loose.sort(key=lambda m: name_key(m["name"]))
    return {"teams": dict(sorted(ordered.items(), key=lambda kv: str(kv[0]))),
            "unassigned": loose}


def build_teams(contact_df: pd.DataFrame,
                existing: Optional[dict] = None) -> Dict[str, List[dict]]:
    """Just the teams from `build_roster`, for callers that want only those."""
    return build_roster(contact_df, existing)["teams"]


def save_setup(course: str, eval_no: str, contact_df: pd.DataFrame,
               survey_cfg: Dict, vault: Optional[Vault] = None,
               owner: Optional[str] = None):
    """Persist the roster snapshot + survey config for this course/eval.

    `owner` stamps who the survey belongs to; if omitted, any existing owner is
    preserved so a re-save doesn't orphan a section.
    """
    vault = vault or Vault()
    slug = slugify(course, eval_no)
    existing = load_roster_snapshot(vault, slug)
    if existing:
        existing = migrate_roster(vault, slug, existing)
    # Ids already assigned are carried onto this upload, so re-saving a
    # corrected contact list does not mint new students.
    body = build_roster(contact_df, existing)
    snap_owner = owner or (existing.get("owner") if existing else "") or ""
    _save_json(vault, key_roster(slug),
               {"course": course, "eval_no": eval_no, "teams": body["teams"],
                "unassigned": body["unassigned"], "owner": snap_owner,
                "schema": ROSTER_SCHEMA})
    _save_json(vault, key_survey(slug), survey_cfg)
    return slug, body["teams"]


def save_config(course: str, eval_no: str, survey_cfg: Dict,
                vault: Optional[Vault] = None) -> Tuple[str, Dict[str, List[dict]]]:
    """Save the questions, schedule and report settings; keep the saved roster.

    This is what a second visit needs. ``save_setup`` requires a contact list,
    because it writes the roster snapshot — but the file uploader is empty when
    an instructor comes back to a survey they set up last week, and that used to
    leave the save button greyed out with no way to change a closing date.

    Writing only the config is also the safer operation of the two: a student's
    identity in a submission is their position in the stored roster, so the
    roster is exactly the thing not to rewrite once answers exist. Here it is
    not touched at all.
    """
    vault = vault or Vault()
    slug = slugify(course, eval_no)
    _save_json(vault, key_survey(slug), survey_cfg)
    snap = load_roster_snapshot(vault, slug) or {}
    return slug, dict(snap.get("teams", {}) or {})


ROSTER_SCHEMA = 2       # 1 = positional (pre-2.16.0), 2 = permanent ids


def _retire_legacy_response(vault: Vault, slug: str, team: str,
                            pos: int) -> None:
    """Take a migrated positional object out of circulation.

    It has to go: `all_responses` lists by the `resp__<slug>__` prefix, which
    both names share, so a migrated submission left in both places is read
    twice and every rating it carries is counted twice. A tombstone is the
    fallback for storage that refuses deletes.
    """
    name = key_response_legacy(slug, team, pos)
    try:
        vault.delete(name)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        _save_json(vault, name, {"superseded_by": "migrated", "slug": slug})
    except Exception:  # noqa: BLE001
        pass


def migrate_roster(vault: Vault, slug: str,
                   snap: Optional[dict] = None) -> Optional[dict]:
    """Bring a survey stored under the positional scheme onto ids.

    Three things change, in this order, so that a crash between any two leaves
    the vault readable:

    1. every member gets an id, and `issued_pos` records the position their
       link was issued against, which is what lets an old link still resolve;
    2. each stored submission's `members` map — keyed by the teammate's index —
       is rewritten to be keyed by the teammate's id, and the submission is
       re-filed under its author's id;
    3. the snapshot is stamped `schema: 2`, after the responses are moved, so
       an interrupted run is retried rather than skipped.

    Idempotent: a snapshot already at schema 2 is returned untouched. Safe to
    call on every read, which is what the app does — an instructor should never
    have to know a migration happened.

    Each old positional object is then retired, because both names share the
    `resp__<slug>__` prefix that `all_responses` lists: left in place, every
    migrated submission was read **twice**, which silently doubled the ratings
    each student appeared to have received. Deletion is preferred; on a vault
    that refuses deletes the object is overwritten with a tombstone, which
    `all_responses` skips.
    """
    snap = snap if snap is not None else load_roster_snapshot(vault, slug)
    if not snap:
        return snap
    if int(snap.get("schema", 1)) >= ROSTER_SCHEMA:
        return snap

    teams = {t: list(ms or []) for t, ms in (snap.get("teams", {}) or {}).items()}
    # 1. ids, and the position each link was issued against
    for team, members in teams.items():
        for pos, m in enumerate(members):
            if not m.get("id"):
                m["id"] = new_member_id()
            m.setdefault("issued_pos", pos)

    # 2. the submissions
    for team, members in teams.items():
        by_pos = {str(p): str(m["id"]) for p, m in enumerate(members)}
        for pos, m in enumerate(members):
            try:
                old = _load_json(vault, key_response_legacy(slug, team, pos))
            except Exception:  # noqa: BLE001
                continue
            answers = old.get("members")
            if isinstance(answers, dict):
                old["members"] = {by_pos.get(str(k), str(k)): v
                                  for k, v in answers.items()}
            old["team"] = team
            old["pos"] = pos
            try:
                save_response_by_id(vault, slug, str(m["id"]), old)
            except Exception:  # noqa: BLE001
                # Storage refused the write. Leave the snapshot at schema 1 so
                # the next read tries again, rather than stamping it migrated
                # with the answers still only in the old place.
                return snap
            _retire_legacy_response(vault, slug, team, pos)

    # 3. the snapshot
    body = {**snap, "teams": teams,
            "unassigned": list(snap.get("unassigned", []) or []),
            "schema": ROSTER_SCHEMA}
    try:
        _save_json(vault, key_roster(slug), body)
    except Exception:  # noqa: BLE001
        return snap
    return body


def saved_teams(vault: Vault, slug: str) -> Dict[str, List[dict]]:
    """The team list students' links were built against, as stored.

    Link tokens carry a team and a position *in this list*, so this — not a
    freshly uploaded file — is what a link resolves against. Building links from
    anything else risks handing a student someone else's form.
    """
    snap = load_roster_snapshot(vault, slug) or {}
    return dict(snap.get("teams", {}) or {})


def roster_shift(vault: Vault, slug: str, contact_df: pd.DataFrame) -> Dict[str, Any]:
    """Which teams would re-saving this contact list change, and do they have
    answers attached?

    This used to be a danger notice. A student's identity in a submission was
    their *position* in the stored snapshot, so re-saving a contact list
    rebuilt those positions: add one student named "Adams" to a team and
    everyone after them shifted by one, and submissions already in the vault
    began describing the wrong people. Nothing errored; the grades were simply
    wrong, and the only safe answer was "don't".

    From 2.16.0 it is not dangerous, because identity is a permanent id that
    `build_roster` matches onto the upload and keeps. Re-saving now moves
    students between teams rather than renumbering anybody, and every link
    still opens its own student's form.

    It is still worth telling the instructor, for a different reason: a bulk
    re-upload moves people **without** the per-student confirmation the Teams
    editor gives, so answers that stop applying are left in place rather than
    cleared — a stale answer about a former teammate is simply ignored when
    grading. The editor is the precise instrument; this is the blunt one.

    Returns ``{"teams": [...], "responses": n, "safe": bool}`` — the teams
    whose membership would change, and how many submissions are already stored
    for them.
    """
    out: Dict[str, Any] = {"teams": [], "responses": 0, "safe": True}
    snap = load_roster_snapshot(vault, slug)
    if not snap:
        return out                      # nothing stored yet: nothing to disturb
    old_teams = snap.get("teams", {}) or {}
    new_teams = build_teams(contact_df)
    moved = [
        team for team, members in old_teams.items()
        if [m.get("name") for m in members]
        != [m.get("name") for m in new_teams.get(team, [])]
    ]
    if not moved:
        return out
    affected = [r for r in all_responses(vault, slug) if r.get("team") in moved]
    out["teams"] = sorted(moved)
    out["responses"] = len(affected)
    out["safe"] = not affected
    return out


# --------------------------------------------------------------------------- #
# Editing the roster after it is saved
# --------------------------------------------------------------------------- #
# Every operation here takes the stored snapshot, returns the new one, and says
# what it did. None of them renumbers anybody's identity, because identity is
# the id — so a move or a removal disturbs only the answers that genuinely
# stopped being true, which `edit_impact` names before anything is written.
MIN_TEAM = 2        # below this a "team" has nobody to evaluate


def _answers_about(resp: dict, member_id: str) -> bool:
    return str(member_id) in {str(k) for k in (resp.get("members") or {})}


def edit_impact(vault: Vault, slug: str, action: str, member_id: str = "",
                team: str = "") -> Dict[str, Any]:
    """What would this edit discard? Reported before it is done, never after.

    An edit is only ever allowed to drop answers that have stopped describing
    anything. Moving a student to another team means:

    * the allocations and comments **they wrote about their old teammates** no
      longer describe a team they are on;
    * their **old teammates' answers about them** likewise.

    Their own submission survives the move, and so does everything their old
    teammates wrote about each *other*. Removing a student from the evaluation
    drops their submission outright and every reference to them.

    Returns ``{"drops": [plain-English lines], "responses": n, "safe": bool}``.
    `safe` is True when nothing stored is affected — the ordinary case before
    links go out, and the case where the student has not submitted and nobody
    has written about them yet.
    """
    out: Dict[str, Any] = {"drops": [], "responses": 0, "safe": True}
    snap = roster_now(vault, slug)
    if not snap:
        return out
    who = member_by_id(snap, member_id)
    if not who:
        return out
    name = who.get("name", member_id)
    from_team = who.get("team", "")
    if action == "move" and (team or "") == from_team:
        return out

    affected = 0
    mine = load_response_by_id(vault, slug, member_id)
    if action == "remove":
        if mine:
            affected += 1
            out["drops"].append(f"**{name}**'s own submission is deleted.")
    elif action == "move" and mine and (mine.get("members") or {}):
        wrote = [k for k in (mine.get("members") or {})
                 if str(k) != str(member_id)]
        if wrote:
            affected += 1
            out["drops"].append(
                f"The ratings and comments **{name}** wrote about their "
                f"{len(wrote)} teammate(s) on team {from_team or '—'} are "
                f"cleared. Their own self-evaluation is kept.")

    teammates = ((snap.get("teams", {}) or {}).get(from_team, []) or [])
    about = []
    for m in teammates:
        if str(m.get("id")) == str(member_id):
            continue
        resp = load_response_by_id(vault, slug, str(m.get("id", "")))
        if resp and _answers_about(resp, member_id):
            about.append(m.get("name", ""))
    if about:
        affected += len(about)
        verb = "deleted" if action == "remove" else "cleared"
        out["drops"].append(
            f"What {len(about)} teammate(s) wrote about **{name}** is {verb} "
            f"({', '.join(sorted(about)[:5])}"
            f"{'…' if len(about) > 5 else ''}).")

    if action == "move" and team:
        out["drops"].append(
            f"**{name}**'s new teammates on team {team} have not evaluated "
            f"them. That shows as no data rather than a low score, and the "
            f"instructor summary names them.")
    out["responses"] = affected
    out["safe"] = affected == 0
    return out


def _strip_answers_about(vault: Vault, slug: str, snap: dict,
                         team: str, member_id: str) -> int:
    """Remove every reference to one student from their team's submissions."""
    touched = 0
    for m in ((snap.get("teams", {}) or {}).get(team, []) or []):
        mid = str(m.get("id", ""))
        if not mid or mid == str(member_id):
            continue
        resp = load_response_by_id(vault, slug, mid)
        if not resp or not _answers_about(resp, member_id):
            continue
        resp["members"] = {k: v for k, v in (resp.get("members") or {}).items()
                           if str(k) != str(member_id)}
        save_response_by_id(vault, slug, mid, resp)
        touched += 1
    return touched


def _write_roster(vault: Vault, slug: str, snap: dict) -> dict:
    body = {**snap, "schema": ROSTER_SCHEMA}
    _save_json(vault, key_roster(slug), body)
    return body


def _place(snap: dict, team: str, record: dict) -> None:
    """Put a record on a team (or the unassigned list), in name order."""
    bucket = (snap.setdefault("teams", {}).setdefault(team, [])
              if team else snap.setdefault("unassigned", []))
    bucket.append(record)
    bucket.sort(key=lambda m: name_key(m.get("name", "")))


def _lift(snap: dict, member_id: str) -> Optional[dict]:
    """Take a record off whatever list it is on, and return it."""
    for team, members in list((snap.get("teams", {}) or {}).items()):
        for i, m in enumerate(members or []):
            if str(m.get("id")) == str(member_id):
                return members.pop(i)
    loose = snap.get("unassigned", []) or []
    for i, m in enumerate(loose):
        if str(m.get("id")) == str(member_id):
            return loose.pop(i)
    return None


# Nothing here dissolves a team automatically, and that is deliberate.
#
# The first version of these operations tidied away any team that fell below
# MIN_TEAM, which made building one impossible: create team 3, move the first
# student in, and the team was removed under you before the second arrived. It
# also meant a team could disappear as a side effect of an edit the instructor
# made somewhere else.
#
# So an undersized or even empty team stays until `delete_team` is called. A
# team of one is simply not gradeable yet: the editor says "needs 1 more", and
# that student's own link tells them they are not on a team yet rather than
# handing them a form with only themselves on it.


def undersized_teams(snap: Optional[dict]) -> Dict[str, int]:
    """{team: how many more members it needs} for teams that cannot grade."""
    return {t: MIN_TEAM - len(ms or [])
            for t, ms in ((snap or {}).get("teams", {}) or {}).items()
            if len(ms or []) < MIN_TEAM}


def move_student(vault: Vault, slug: str, member_id: str,
                 team: str) -> Dict[str, Any]:
    """Move one student to another team (or to unassigned, with team="").

    Their own submission moves with them, minus the answers they wrote about
    the team they are leaving; their old teammates' answers about them are
    cleared. Nobody else's identity, link or answers change.
    """
    snap = roster_now(vault, slug)
    if not snap:
        return {"ok": False, "detail": "This survey has no saved roster."}
    who = member_by_id(snap, member_id)
    if not who:
        return {"ok": False, "detail": "That student is not on this roster."}
    old_team = who.get("team", "")
    if (team or "") == old_team:
        return {"ok": False, "detail": f"{who['name']} is already there."}

    cleared = _strip_answers_about(vault, slug, snap, old_team, member_id)
    mine = load_response_by_id(vault, slug, member_id)
    if mine:
        kept_self = {k: v for k, v in (mine.get("members") or {}).items()
                     if str(k) == str(member_id)}
        mine["members"] = kept_self      # their self-evaluation, nothing else
        mine["team"] = team
        save_response_by_id(vault, slug, member_id, mine)

    record = _lift(snap, member_id)
    _place(snap, team or "", record)
    _write_roster(vault, slug, snap)
    where = f"team {team}" if team else "the unassigned list"
    detail = f"Moved **{record['name']}** from {old_team or '—'} to {where}."
    if cleared:
        detail += f" {cleared} teammate submission(s) no longer refer to them."
    short = undersized_teams(snap)
    if short:
        detail += (" " + "; ".join(f"team {t} needs {n} more member(s)"
                                   for t, n in sorted(short.items()))
                   + " before it can be graded.")
    return {"ok": True, "detail": detail, "snapshot": snap}


def add_student(vault: Vault, slug: str, name: str, team: str = "",
                email: str = "", first: str = "", last: str = "",
                klass: str = "", section: str = "") -> Dict[str, Any]:
    """Add a student to a team. They get a new id and their own link.

    Nobody else's link changes, and nobody else's answers are touched — the
    point of the id scheme. Their new teammates simply have no answers about
    them, which grades as no data.
    """
    name = (name or "").strip()
    if not name:
        return {"ok": False, "detail": "A student needs a name."}
    snap = roster_now(vault, slug)
    if not snap:
        return {"ok": False, "detail": "This survey has no saved roster."}
    if find_member(snap, name, email):
        return {"ok": False,
                "detail": f"**{name}** is already on this roster."}
    record = {
        "id": new_member_id(), "name": name,
        "first": (first or name.split(" ")[0]).strip(),
        "last": (last or name.split(" ")[-1]).strip(),
        "email": (email or "").strip(),
        "class": (klass or "").strip(), "section": (section or "").strip(),
    }
    _place(snap, team or "", record)
    _write_roster(vault, slug, snap)
    where = f"team {team}" if team else "the unassigned list"
    extra = ("" if record["email"] else
             " They have no email address, so their link has to be given to "
             "them another way.")
    return {"ok": True, "snapshot": snap, "member_id": record["id"],
            "detail": f"Added **{name}** to {where}. Their personal link is "
                      f"on the Send links panel; everyone else's is "
                      f"unchanged.{extra}"}


def remove_student(vault: Vault, slug: str, member_id: str) -> Dict[str, Any]:
    """Take a student off the evaluation entirely.

    Their submission is deleted and every reference to them in their teammates'
    submissions is removed, because neither describes anything any more. Their
    link stops working; nobody else's does.
    """
    snap = roster_now(vault, slug)
    if not snap:
        return {"ok": False, "detail": "This survey has no saved roster."}
    who = member_by_id(snap, member_id)
    if not who:
        return {"ok": False, "detail": "That student is not on this roster."}
    team = who.get("team", "")
    cleared = _strip_answers_about(vault, slug, snap, team, member_id)
    deleted = False
    if load_response_by_id(vault, slug, member_id) is not None:
        try:
            vault.delete(key_response(slug, member_id))
            deleted = True
        except Exception:  # noqa: BLE001
            # Storage that refuses deletes: blank the submission instead, so
            # it can no longer be graded, and say so.
            save_response_by_id(vault, slug, member_id,
                                {"slug": slug, "team": team, "members": {},
                                 "removed": True})
    _lift(snap, member_id)
    _write_roster(vault, slug, snap)
    detail = f"Removed **{who['name']}** from the evaluation."
    if deleted:
        detail += " Their submission was deleted."
    if cleared:
        detail += f" {cleared} teammate submission(s) no longer refer to them."
    short = undersized_teams(snap)
    if short:
        detail += (" " + "; ".join(f"team {t} needs {n} more member(s)"
                                   for t, n in sorted(short.items()))
                   + " before it can be graded.")
    return {"ok": True, "detail": detail, "snapshot": snap}


def create_team(vault: Vault, slug: str, team: str) -> Dict[str, Any]:
    """Make an empty team, so students can be moved into it."""
    team = (team or "").strip()
    if not team:
        return {"ok": False, "detail": "A team needs a name."}
    snap = roster_now(vault, slug)
    if not snap:
        return {"ok": False, "detail": "This survey has no saved roster."}
    if team in (snap.get("teams", {}) or {}):
        return {"ok": False, "detail": f"Team {team} already exists."}
    # An empty team is a real team in the snapshot, and it stays until someone
    # deletes it. Nothing here tidies it away, so an instructor can create a
    # team and then fill it one student at a time.
    snap.setdefault("teams", {})[team] = []
    snap["teams"] = dict(sorted(snap["teams"].items(), key=lambda kv: str(kv[0])))
    _write_roster(vault, slug, snap)
    return {"ok": True, "snapshot": snap,
            "detail": f"Team {team} is ready — move at least {MIN_TEAM} "
                      f"students into it and it can be graded."}


def rename_team(vault: Vault, slug: str, team: str,
                new_name: str) -> Dict[str, Any]:
    """Rename a team. Links and answers are unaffected: they key on ids.

    Submissions carry their author's team as a label, so those are relabelled
    too — otherwise the grader would file this team's answers under a team that
    no longer exists.
    """
    new_name = (new_name or "").strip()
    if not new_name:
        return {"ok": False, "detail": "A team needs a name."}
    snap = roster_now(vault, slug)
    if not snap:
        return {"ok": False, "detail": "This survey has no saved roster."}
    teams = snap.get("teams", {}) or {}
    if team not in teams:
        return {"ok": False, "detail": f"There is no team {team}."}
    if new_name in teams and new_name != team:
        return {"ok": False,
                "detail": f"Team {new_name} already exists — move the students "
                          f"into it instead, if you mean to merge them."}
    members = teams.pop(team)
    teams[new_name] = members
    snap["teams"] = dict(sorted(teams.items(), key=lambda kv: str(kv[0])))
    relabelled = 0
    for m in members:
        resp = load_response_by_id(vault, slug, str(m.get("id", "")))
        if resp is not None:
            resp["team"] = new_name
            save_response_by_id(vault, slug, str(m["id"]), resp)
            relabelled += 1
    _write_roster(vault, slug, snap)
    detail = f"Team {team} is now team {new_name}."
    if relabelled:
        detail += f" {relabelled} submission(s) were relabelled."
    return {"ok": True, "detail": detail, "snapshot": snap}


def delete_team(vault: Vault, slug: str, team: str) -> Dict[str, Any]:
    """Dissolve a team. Its students go to unassigned, keeping their ids.

    Nothing is deleted: a student who has submitted keeps their submission, and
    it becomes gradeable again as soon as they are on a team. Removing a
    student's data is `remove_student`, which is a separate, explicit act.
    """
    snap = roster_now(vault, slug)
    if not snap:
        return {"ok": False, "detail": "This survey has no saved roster."}
    teams = snap.get("teams", {}) or {}
    if team not in teams:
        return {"ok": False, "detail": f"There is no team {team}."}
    members = list(teams.pop(team) or [])
    for m in members:
        _place(snap, "", m)
    _write_roster(vault, slug, snap)
    if not members:
        return {"ok": True, "snapshot": snap,
                "detail": f"Team {team} was empty and is gone."}
    return {"ok": True, "snapshot": snap,
            "detail": f"Team {team} is dissolved. Its {len(members)} student(s) "
                      f"are unassigned, with their submissions kept — move "
                      f"them to a team and they grade again."}


def all_team_names(snap: Optional[dict]) -> List[str]:
    """Every team a student can be moved to, empty ones included."""
    return sorted(((snap or {}).get("teams", {}) or {}), key=str)


def survey_owner(vault: Vault, slug: str) -> Optional[str]:
    """The owner username stamped on a survey, '' if unowned, None if missing."""
    snap = load_roster_snapshot(vault, slug)
    if snap is None:
        return None
    return snap.get("owner", "") or ""


def survey_objects(vault: Vault, slug: str) -> List[str]:
    """Everything stored for one survey, by object name.

    Listed rather than guessed, so a response for a team whose name was tidied
    into something unexpected still turns up. Named separately from the delete
    itself because the instructor is shown this list — with counts — before
    anything is removed.
    """
    from .feedback_ai import drafts_key
    try:
        names = set(vault.list())
    except Exception:  # noqa: BLE001
        names = set()
    wanted = []
    for fixed in (key_survey(slug), key_roster(slug), drafts_key(slug)):
        if fixed in names:
            wanted.append(fixed)
    wanted += sorted(n for n in names if str(n).startswith(_resp_prefix(slug)))
    return wanted


def describe_survey(vault: Vault, slug: str) -> Dict[str, Any]:
    """What deleting this survey would destroy, for the confirmation to state.

    Counts come from object names, so this costs one listing however large the
    section is — it runs while the instructor is looking at a sidebar.
    """
    from .feedback_ai import drafts_key
    objects = survey_objects(vault, slug)
    snap = load_roster_snapshot(vault, slug) or {}
    teams = snap.get("teams", {}) or {}
    return {
        "objects": objects,
        "responses": sum(1 for n in objects
                         if str(n).startswith(_resp_prefix(slug))),
        "students": sum(len(v) for v in teams.values()),
        "teams": len(teams),
        "has_drafts": drafts_key(slug) in objects,
        "owner": snap.get("owner", "") or "",
        "course": snap.get("course", ""),
        "eval_no": str(snap.get("eval_no", "") or ""),
    }


def delete_survey(vault: Vault, slug: str) -> Tuple[int, List[str]]:
    """Delete one survey and everything stored with it. Returns (deleted, errors).

    What goes: the questions and schedule, the roster snapshot that student
    links resolve against, every collected response, and the AI drafts for this
    survey. Nothing else — another survey's objects do not share these names,
    which is asserted by a test, because a slug prefix is a blunt instrument and
    "QA101-eval1" is a prefix of "QA101-eval10".

    It is not recoverable: the vault has no trash. The caller is responsible for
    asking first, and for telling the instructor they can keep an encrypted
    bundle from the Vault tab beforehand if the responses still matter.
    """
    deleted, errors = 0, []
    for name in survey_objects(vault, slug):
        try:
            vault.delete(name)
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    # The listing and any remembered reads are now wrong; a stale one would
    # leave the deleted survey in the picker.
    try:
        vault.refresh()
    except Exception:  # noqa: BLE001
        pass
    return deleted, errors


def list_surveys(vault: Optional[Vault] = None):
    """Every survey in the vault, with owner + counts, for the picker."""
    vault = vault or Vault()
    try:
        names = [n for n in vault.list()
                 if str(n).startswith("roster__") and str(n).endswith(".ppj")]
    except Exception:
        names = []
    out = []
    for n in names:
        try:
            snap = _load_json(vault, n)
        except Exception:
            continue
        teams = snap.get("teams", {})
        out.append({"slug": n[len("roster__"):-len(".ppj")],
                    "course": snap.get("course", ""), "eval_no": snap.get("eval_no", ""),
                    "owner": snap.get("owner", "") or "", "teams": len(teams),
                    "students": sum(len(v) for v in teams.values())})
    return sorted(out, key=lambda s: (s["course"], str(s["eval_no"])))


def can_access(owner: Optional[str], user: Optional[dict]) -> bool:
    """Admins reach everything; instructors reach only what they own."""
    if user and user.get("role") == "admin":
        return True
    return bool(user) and bool(owner) and owner == user.get("user")


def visible_surveys(all_surveys, user: Optional[dict]):
    if user and user.get("role") == "admin":
        return all_surveys
    me = (user or {}).get("user")
    return [s for s in all_surveys if s.get("owner") == me]


def load_roster_snapshot(vault: Vault, slug: str) -> Optional[dict]:
    """The stored snapshot, as stored. Does not migrate — see `roster_now`."""
    try:
        return _load_json(vault, key_roster(slug))
    except Exception:
        return None


def roster_now(vault: Vault, slug: str) -> Optional[dict]:
    """The snapshot, migrated onto permanent ids if it is not already.

    What every caller that is about to *use* a snapshot should ask for. A
    read-only glance at what is stored wants `load_roster_snapshot`.
    """
    return migrate_roster(vault, slug)


def load_survey(vault: Vault, slug: str) -> Dict:
    try:
        return {**DEFAULT_SURVEY, **_load_json(vault, key_survey(slug))}
    except Exception:
        return dict(DEFAULT_SURVEY)


def roster_for_matching(snapshot: dict) -> Roster:
    """A Roster (name_key -> record) rebuilt from the snapshot, for email match."""
    r = Roster()
    for team, members in (snapshot or {}).get("teams", {}).items():
        for m in members:
            r.by_key[name_key(m["name"])] = {
                "name": m["name"], "first": m.get("first", ""),
                "last": m.get("last", ""), "email": m.get("email", ""),
                "team": team,
            }
    return r


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #
def student_links(base_url: str, slug: str, teams: Dict[str, List[dict]],
                  secret: str) -> List[dict]:
    base = (base_url or "").strip()
    sep = "&" if "?" in base else "?"
    out = []
    for team, members in teams.items():
        for pos, m in enumerate(members):
            # The token carries the student's permanent id, so their link keeps
            # working through every later edit to the roster. A member with no
            # id can only be one from a snapshot that has not been migrated
            # yet, and falls back to the positional form.
            mid = str(m.get("id", "") or "")
            claim = {"s": slug, "i": mid} if mid else {"s": slug, "t": team,
                                                       "p": pos}
            tok = make_token(claim, secret)
            link = f"{base}{sep}t={tok}" if base else f"?t={tok}"
            out.append({"team": team, "pos": pos, "name": m["name"],
                        "id": mid, "email": m.get("email", ""), "link": link})
    return out


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
def save_response_by_id(vault: Vault, slug: str, member_id: str,
                        payload: dict) -> None:
    """File one student's submission under their permanent id."""
    _save_json(vault, key_response(slug, member_id),
               {**payload, "member_id": member_id})


def load_response_by_id(vault: Vault, slug: str,
                        member_id: str) -> Optional[dict]:
    try:
        return _load_json(vault, key_response(slug, member_id))
    except Exception:
        return None


def save_response(vault: Vault, slug: str, team: str, pos: int,
                  payload: dict) -> None:
    """Save by team and position, which is how the test drivers address it.

    Resolved to the student's id against the stored roster, so everything ends
    up under one scheme. With no roster to resolve against — a harness that
    writes submissions without one — the old positional name is used, and
    `all_responses` still reads it.
    """
    member = None
    snap = load_roster_snapshot(vault, slug)
    members = ((snap or {}).get("teams", {}) or {}).get(team, []) or []
    if 0 <= pos < len(members):
        member = members[pos]
    if member and member.get("id"):
        save_response_by_id(vault, slug, str(member["id"]),
                            {**payload, "team": team, "pos": pos})
        return
    _save_json(vault, key_response_legacy(slug, team, pos), payload)


def load_response(vault: Vault, slug: str, team: str,
                  pos: int) -> Optional[dict]:
    snap = load_roster_snapshot(vault, slug)
    members = ((snap or {}).get("teams", {}) or {}).get(team, []) or []
    if 0 <= pos < len(members) and members[pos].get("id"):
        got = load_response_by_id(vault, slug, str(members[pos]["id"]))
        if got is not None:
            return got
    try:
        return _load_json(vault, key_response_legacy(slug, team, pos))
    except Exception:
        return None


def response_count(vault: Vault, slug: str) -> int:
    """How many students have submitted — from the object names alone.

    The Start here checklist wants this number on every rerun, and it used to
    get it by calling ``all_responses``, which fetches and decrypts all forty
    submissions. One listing answers the same question.
    """
    prefix = _resp_prefix(slug)
    try:
        names = [str(n) for n in vault.list() if str(n).startswith(prefix)]
    except Exception:  # noqa: BLE001
        return 0
    # Both naming schemes share the prefix, so counting every name would count
    # a migrated author twice — once under their id and once under the
    # positional name migration retired. Decided from the names alone, because
    # the Start here checklist asks for this number on every rerun and fetching
    # forty submissions to answer it is the cost 2.12.0 removed.
    #
    # Where any id-keyed submission exists the roster has been migrated, so the
    # id names are the complete set and any positional name left beside them is
    # a retired one. Where none does, nothing has been migrated and the
    # positional names are all there is.
    ids = [n for n in names if n.startswith(f"{prefix}m")]
    return len(ids) if ids else len(names)


def all_responses(vault: Vault, slug: str) -> List[dict]:
    prefix = _resp_prefix(slug)
    try:
        names = [n for n in vault.list() if str(n).startswith(prefix)]
    except Exception:
        names = []
    out = []
    for n in names:
        try:
            got = _load_json(vault, n)
        except Exception:
            continue
        # A positional object that migration has superseded, on storage that
        # would not delete it. Reading it would double its author's answers.
        if isinstance(got, dict) and not got.get("superseded_by"):
            out.append(got)
    return out


def response_status(vault: Vault, slug: str) -> List[dict]:
    """Per-student responded/not-responded rows, for the progress panel."""
    snap = roster_now(vault, slug)
    if not snap:
        return []
    responses = all_responses(vault, slug)
    by_id = {str(r.get("member_id", "")) for r in responses
             if r.get("member_id")}
    by_seat = {(r.get("team"), int(r.get("pos", -1))) for r in responses
               if not r.get("member_id")}
    rows = []
    for team, members in (snap.get("teams", {}) or {}).items():
        for pos, m in enumerate(members):
            rows.append({"team": team, "pos": pos, "name": m["name"],
                         "id": str(m.get("id", "")),
                         "email": m.get("email", ""),
                         "responded": str(m.get("id", "")) in by_id
                                      or (team, pos) in by_seat})
    # On the roster but not on a team: they have nothing to fill in, so they
    # are listed as such rather than as a student who has not replied.
    for m in (snap.get("unassigned", []) or []):
        rows.append({"team": "", "pos": -1, "name": m["name"],
                     "id": str(m.get("id", "")),
                     "email": m.get("email", ""), "responded": False,
                     "unassigned": True})
    return rows


def _author_of(resp: dict, snap: dict) -> Optional[dict]:
    """Who wrote this submission, and the team they are on *now*.

    By id when the submission carries one, which is every submission written
    or migrated from 2.16.0 on. Older ones carry only the team and position
    they were saved against, and are resolved that way — so a vault that is
    part-way through migration still grades completely.

    None when the author is no longer on a team (they have been moved to the
    unassigned list, or removed): there is no team to grade their answers
    within, and guessing one is how answers end up attributed to strangers.
    """
    teams = snap.get("teams", {}) or {}
    mid = str(resp.get("member_id", "") or "")
    if mid:
        who = member_by_id(snap, mid)
        if not who or not who.get("team"):
            return None
        members = teams.get(who["team"], []) or []
        if not (0 <= int(who.get("pos", -1)) < len(members)):
            return None
        return {"team": who["team"], "pos": int(who["pos"]),
                "members": members, "id": mid}
    team = resp.get("team", "")
    members = teams.get(team, []) or []
    try:
        pos = int(resp.get("pos", -1))
    except (TypeError, ValueError):
        return None
    if not (0 <= pos < len(members)):
        return None
    return {"team": team, "pos": pos, "members": members,
            "id": str(members[pos].get("id", ""))}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _rank_num(category) -> float:
    """Forced-ranking category -> 1 (High) / 2 (Adequate) / 3 (Low)."""
    if not category:
        return float("nan")
    c = str(category).lower()
    if "high" in c:
        return 1.0
    if "low" in c:
        return 3.0
    if "adequate" in c or "expected" in c or "medium" in c:
        return 2.0
    return float("nan")


def self_evaluations(vault: Vault, slug: str) -> Dict:
    """Map (team, name_key) -> the student's self-ratings + self rank, for the
    self-evaluation section of the grade/feedback."""
    from .ingest import name_key as _nk
    snap = roster_now(vault, slug)
    if not snap:
        return {}
    out: Dict = {}
    for resp in all_responses(vault, slug):
        author = _author_of(resp, snap)
        if not author:
            continue
        team, pos, members = author["team"], author["pos"], author["members"]
        answers = resp.get("members") or {}
        self_ans = (answers.get(str(members[pos].get("id", "")))
                    or answers.get(str(pos)))
        if not self_ans:
            continue
        out[(team, _nk(members[pos]["name"]))] = {
            "ratings": self_ans.get("ratings"),
            "rank": _rank_num(self_ans.get("rank")),
            "self_contribution": resp.get("self_contribution", ""),
        }
    return out


def responses_to_long(vault: Vault, slug: str) -> pd.DataFrame:
    """Turn collected submissions into the tidy long-format the grader consumes:
    one row per (evaluator -> evaluatee). Points come from the $100 allocation and
    the public comment from the anonymous 'contribution' + 'improve' feedback.
    Handles both the full survey payload (`members`) and the older flat payload."""
    snap = roster_now(vault, slug)
    if not snap:
        return pd.DataFrame()
    rows = []
    for resp in all_responses(vault, slug):
        # Resolved by the author's permanent id, so an edited roster re-grades
        # correctly instead of silently reading someone else's answers.
        author = _author_of(resp, snap)
        if not author:
            continue
        team, pos, members = author["team"], author["pos"], author["members"]
        evaluator = members[pos]["name"]
        ev_key = name_key(evaluator)
        conf = resp.get("confidential", "") or ""
        # Answers are keyed by the teammate's id; a submission saved before
        # 2.16.0 and not yet migrated keys them by position instead.
        member_ans = answers_by_position(resp.get("members") or {}, members)
        first = True

        if member_ans:  # full survey design
            for tpos_str, ans in member_ans.items():
                tpos = int(tpos_str)
                if tpos == pos or tpos >= len(members):
                    continue
                improve = str((ans or {}).get("improve", "") or "").strip()
                contribution = str((ans or {}).get("contribution", "") or "").strip()
                pub = " ".join(t for t in (contribution, improve) if t).strip()
                ratings = (ans or {}).get("ratings") or []
                row = {
                    "evaluator": evaluator, "evaluator_key": ev_key,
                    "evaluator_team": team, "evaluatee": members[tpos]["name"],
                    "evaluatee_key": name_key(members[tpos]["name"]),
                    "points": _num((ans or {}).get("alloc")),
                    "public_comment": pub,
                    "contribution_text": contribution,
                    "improve_text": improve,
                    "confidential_comment": conf if first else "",
                    "rank": _rank_num((ans or {}).get("rank")),
                }
                for d in range(4):
                    row[f"r{d}"] = _num(ratings[d]) if d < len(ratings) else float("nan")
                rows.append(row)
                first = False
        else:  # legacy flat payload (alloc/comments)
            alloc = resp.get("alloc", {}) or {}
            comments = resp.get("comments", {}) or {}
            for tpos_str, pts in alloc.items():
                tpos = int(tpos_str)
                if tpos == pos or tpos >= len(members):
                    continue
                rows.append({
                    "evaluator": evaluator, "evaluator_key": ev_key,
                    "evaluator_team": team, "evaluatee": members[tpos]["name"],
                    "evaluatee_key": name_key(members[tpos]["name"]),
                    "points": _num(pts),
                    "public_comment": str(comments.get(tpos_str, "") or ""),
                    "confidential_comment": conf if first else "",
                })
                first = False

    # ---- students nobody has evaluated yet ---------------------------------
    # Without this they are absent from the results altogether rather than
    # listed with "No data", which is how a student who has just been moved to
    # another team vanishes: their new teammates have not written about them,
    # and the answers they wrote about their old team were cleared with the
    # move, so not one row in the frame mentions them.
    #
    # A presence row names them on their team and carries nothing else — the
    # grader counts it for membership only (see `compute`, which skips a row
    # whose evaluator and evaluatee are the same student). The instructor then
    # gets them in the summary as "No data", and `data_warnings` says who they
    # are, which is the whole point.
    seen = {str(r["evaluator_key"]) for r in rows} | \
           {str(r["evaluatee_key"]) for r in rows}
    for team, members in (snap.get("teams", {}) or {}).items():
        if len(members or []) < MIN_TEAM:
            continue            # not a gradeable team yet; reported elsewhere
        for m in members:
            key = name_key(m["name"])
            if key in seen:
                continue
            rows.append({
                "evaluator": m["name"], "evaluator_key": key,
                "evaluator_team": team, "evaluatee": m["name"],
                "evaluatee_key": key, "points": float("nan"),
                "public_comment": "", "contribution_text": "",
                "improve_text": "", "confidential_comment": "",
                "rank": float("nan"),
                **{f"r{d}": float("nan") for d in range(4)},
            })
    return pd.DataFrame.from_records(rows)


# --------------------------------------------------------------------------- #
# Public student form (served by app.py when a ?t=<token> is present)
# --------------------------------------------------------------------------- #
def _wid(member: dict, index: int) -> str:
    """The widget-key stem for one teammate on the student form.

    Their permanent id, not their position. Streamlit keeps a widget's value in
    session state by key across reruns, so a positional key ties a typed value
    to a *seat*: if the instructor moved a teammate while a student had the
    form open, the student's next click re-rendered with the new member list
    and the $30 they had typed for one teammate silently became another's. The
    id follows the person. Position is the fallback for a roster from before
    ids existed.
    """
    return str(member.get("id") or f"p{index}")


def answers_by_position(stored: Dict, members: List[dict]) -> Dict[str, dict]:
    """Stored id-keyed answers → the position keys the form widgets use.

    The form renders one fixed member list in one pass, so positions are a
    perfectly good index *inside* it — they are only dangerous as a durable
    identity. So the translation happens at the two boundaries, here on the way
    in and in `answers_by_id` on the way out, and the widget code in between is
    left alone.

    A key that is a plain integer is a position, from a submission saved before
    2.16.0 that migration has not reached; anything else is an id.
    """
    pos_of = {str(m.get("id", "")): str(i) for i, m in enumerate(members)}
    out: Dict[str, dict] = {}
    for key, value in (stored or {}).items():
        k = str(key)
        if k in pos_of:
            out[pos_of[k]] = value
        elif k.isdigit() and int(k) < len(members):
            out[k] = value              # legacy positional
    return out


def answers_by_id(answers: Dict, members: List[dict]) -> Dict[str, dict]:
    """The form's position-keyed answers → the id keys they are stored under."""
    out: Dict[str, dict] = {}
    for key, value in (answers or {}).items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(members):
            mid = str(members[idx].get("id", "") or "")
            out[mid or str(idx)] = value
    return out


def submission_problems(answers: Dict, survey_cfg: Dict, n_members: int,
                        self_pos: Optional[int] = None) -> List[str]:
    """Pure validation used both for live feedback and to gate the submit button.
    Returns a list of human-readable problems ([] means ready to submit)."""
    problems: List[str] = []
    if survey_cfg.get("ask_ratings", True):
        stmts = survey_cfg.get("rating_statements") or RATING_STATEMENTS
        missing = 0
        for i in range(n_members):
            r = (answers.get(str(i), {}) or {}).get("ratings") or []
            for d in range(len(stmts)):
                if d >= len(r) or r[d] in (None, ""):
                    missing += 1
        if missing:
            problems.append(f"{missing} rating statement(s) still unanswered.")
    if survey_cfg.get("ask_ranking", True):
        cats = survey_cfg.get("ranking_categories") or RANK_CATEGORIES
        ranks = [(answers.get(str(i), {}) or {}).get("rank") for i in range(n_members)]
        if any(r is None for r in ranks):
            problems.append("Place every team member in a ranking category.")
        elif n_members >= len(cats) and len(set(ranks)) < len(cats):
            problems.append("Use every ranking category at least once.")
    if survey_cfg.get("ask_allocation", True):
        total = int(survey_cfg.get("points_total", 100))
        # Self is excluded: the student does not pay themselves, so their own
        # row carries no allocation and must not count towards the total.
        running = sum(int((answers.get(str(i), {}) or {}).get("alloc") or 0)
                      for i in range(n_members) if i != self_pos)
        if running != total:
            problems.append(f"Pay allocation must total exactly ${total} (currently ${running}).")
    return problems


def render_student_app(token: str) -> None:
    """Render one student's evaluation form and record their submission.

    This runs BEFORE the instructor password gate, so it is the only public
    surface. It can read the roster/survey and write a sealed response, but it
    is otherwise the same encrypted vault the console uses.
    """
    import streamlit as st

    cfg = load_config()
    payload = read_token(token, token_secret(cfg))
    if not payload:
        st.error("This link is invalid or has expired. Please contact your instructor.")
        return

    slug = payload.get("s", "")

    vault = Vault()
    if not vault.available:
        # The instructor's storage is misconfigured or unreachable. A student
        # can do nothing about that and should not be shown the reason, which
        # names the institution's storage setup — they get the same short
        # message as any other "not right now", and their link keeps working
        # once it is fixed.
        st.error("This evaluation isn't available right now. Please try again "
                 "later, or contact your instructor if it continues.")
        return
    try:
        snap = roster_now(vault, slug)
        survey = load_survey(vault, slug)
    except Exception:  # noqa: BLE001
        snap, survey = None, dict(DEFAULT_SURVEY)
    if not snap:
        st.error("This evaluation isn't available right now. Please try again later.")
        return

    # The link identifies the student, not a seat on a team — so a student whose
    # instructor has since moved them to another team follows the same link and
    # finds their new teammates on the form.
    member_id = resolve_token(payload, snap)
    me = member_by_id(snap, member_id) if member_id else None
    if not me:
        st.error("This link doesn't match the current roster. Contact your instructor.")
        return
    team = me.get("team", "")
    pos = int(me.get("pos", 0))
    members = (snap.get("teams", {}) or {}).get(team, []) or []
    if not team or len(members) < MIN_TEAM:
        # On the roster but not on a team: there is nobody to evaluate. Said
        # plainly, rather than showing a form with only themselves on it.
        st.info("You're on the roster for this evaluation, but you haven't "
                "been assigned to a team yet, so there's nothing to fill in "
                "just now. Your instructor will let you know — this same link "
                "will work once you're on a team.")
        return

    st.header(survey.get("title", "Peer Evaluation"))

    # ---- Introduction / header --------------------------------------------
    if survey.get("show_header", True):
        klass = me.get("class") or snap.get("course", "")
        section = me.get("section", "")
        bits = [f"**{me['name']}**"]
        if klass:
            bits.append(f"Class: {klass}")
        if section:
            bits.append(f"Section: {section}")
        bits.append(f"Team {team} · Evaluation {snap.get('eval_no', '')}")
        st.caption("  ·  ".join(bits))
        st.caption("You will evaluate the following team members (and yourself): "
                   + ", ".join(m["name"] for m in members))
    else:
        st.caption(f"{snap.get('course', '')} · Evaluation {snap.get('eval_no', '')}")

    state = window_state(survey)
    if state != "open":
        (st.info if state == "not_yet" else st.warning)(window_message(survey))
        return
    if parse_dt(survey.get("closes_at")):
        st.caption(window_message(survey))
    if survey.get("intro"):
        st.markdown(survey["intro"])

    scale = survey.get("scale_labels") or SCALE_LABELS
    statements = survey.get("rating_statements") or RATING_STATEMENTS
    cats = survey.get("ranking_categories") or RANK_CATEGORIES
    total = int(survey.get("points_total", 100))

    # Load any prior submission once, then let widget state drive the rest (this
    # form is NOT wrapped in st.form, so every answer re-runs and validates live).
    prior_key = f"pp_prior::{slug}::{member_id}"
    if prior_key not in st.session_state:
        st.session_state[prior_key] = (
            load_response_by_id(vault, slug, member_id) or {})
    prior = st.session_state[prior_key]
    # Stored by teammate id; the widgets below index by position in this render.
    pmembers = answers_by_position(prior.get("members", {}) or {}, members)

    answers: Dict[str, dict] = {}
    missing_ratings = 0

    # ---- per-member: ratings (radio) + qualitative --------------------------
    for i, m in enumerate(members):
        who = m["name"] + ("  (yourself)" if i == pos else "")
        st.markdown(f"### {who}")
        pa = pmembers.get(str(i), {}) or {}
        entry: Dict = {}

        if survey.get("ask_ratings", True):
            st.caption(survey.get("ratings_prompt", ""))
            ex_r = pa.get("ratings") or [None] * len(statements)
            rvals = []
            for si, stmt in enumerate(statements):
                default_idx = None
                if si < len(ex_r) and ex_r[si]:
                    try:
                        default_idx = 7 - int(ex_r[si])
                    except Exception:
                        default_idx = None
                choice = st.radio(stmt, scale, index=default_idx, horizontal=True,
                                  key=f"r_{_wid(m, i)}_{si}")
                if choice is None:
                    missing_ratings += 1
                rvals.append((7 - scale.index(choice)) if choice in scale else None)
            entry["ratings"] = rvals

        if survey.get("ask_improve", True):
            entry["improve"] = st.text_area(
                (survey.get("improve_prompt", "") or "").replace("{member}", m["name"]),
                value=str(pa.get("improve", "")), key=f"imp_{_wid(m, i)}")
        if survey.get("ask_contribution", True):
            entry["contribution"] = st.text_area(
                (survey.get("contribution_prompt", "") or "").replace("{member}", m["name"]),
                value=str(pa.get("contribution", "")), key=f"con_{_wid(m, i)}")
        answers[str(i)] = entry
        st.divider()

    # ---- forced ranking (radio) + live check --------------------------------
    rank_issue = None
    if survey.get("ask_ranking", True):
        st.markdown("### Forced ranking")
        st.caption(survey.get("ranking_prompt", ""))
        for i, m in enumerate(members):
            pa = pmembers.get(str(i), {}) or {}
            exr = pa.get("rank")
            who = m["name"] + ("  (yourself)" if i == pos else "")
            choice = st.radio(who, cats, index=(cats.index(exr) if exr in cats else None),
                              horizontal=True, key=f"rank_{_wid(m, i)}")
            answers[str(i)]["rank"] = choice if choice in cats else None
        ranks = [answers[str(i)].get("rank") for i in range(len(members))]
        if any(r is None for r in ranks):
            rank_issue = "Place every team member in a ranking category."
        elif len(members) >= len(cats) and len(set(ranks)) < len(cats):
            rank_issue = "Use every ranking category at least once."
        (st.warning if rank_issue else st.success)(
            "⚠️ " + rank_issue if rank_issue else "Ranking complete.")
        st.divider()

    # ---- pay allocation (live running total) --------------------------------
    allocs: Dict[str, int] = {}
    alloc_issue = None
    if survey.get("ask_allocation", True):
        st.markdown("### Pay students")
        st.caption(survey.get("allocation_prompt", ""))
        for i, m in enumerate(members):
            if i == pos:
                # No box for yourself: the $100 is for teammates. A student who
                # kept dollars back used to lower every teammate for free.
                continue
            pa = pmembers.get(str(i), {}) or {}
            allocs[str(i)] = st.number_input(m["name"], min_value=0, max_value=total,
                                             value=int(pa.get("alloc", 0) or 0), step=1,
                                             key=f"alloc_{_wid(m, i)}")
            answers[str(i)]["alloc"] = allocs[str(i)]
        running = sum(int(v) for v in allocs.values())
        if running == total:
            st.success(f"Allocated ${running} / ${total}. 👍")
        elif running < total:
            alloc_issue = f"${total - running} left to allocate (${running} / ${total})."
            st.warning("⚠️ " + alloc_issue)
        else:
            alloc_issue = f"Over by ${running - total} (${running} / ${total})."
            st.error("⚠️ " + alloc_issue)
        st.divider()

    # ---- your own contribution ----------------------------------------------
    self_contribution = ""
    if survey.get("ask_self_contribution", True):
        st.markdown("### Your contribution")
        self_contribution = st.text_area(survey.get("self_contribution_prompt", ""),
                                         value=str(prior.get("self_contribution", "")),
                                         key="self_contrib")

    # ---- confidential note --------------------------------------------------
    conf = ""
    if survey.get("ask_confidential", True):
        st.markdown("### Confidential note to the instructor")
        conf = st.text_area(survey.get("confidential_prompt", ""),
                            value=str(prior.get("confidential", "")), key="conf_note")

    # ---- live validation summary + gated submit -----------------------------
    st.markdown("---")
    problems = submission_problems(answers, survey, len(members), self_pos=pos)
    if problems:
        st.error("**Before you can submit:**\n\n" + "\n".join(f"- {p}" for p in problems))
    else:
        st.success("Everything looks complete — you're ready to submit.")

    if st.button("Submit evaluation", type="primary", key="pp_student_submit",
                 disabled=bool(problems)):
        if window_state(survey) != "open":
            st.warning(window_message(survey))
            return
        record = {
            "slug": slug, "team": team, "pos": pos, "member_id": member_id,
            "evaluator": me["name"], "evaluator_key": name_key(me["name"]),
            "submitted": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            # Keyed by the teammate's id, so a later roster edit cannot make
            # these answers describe somebody else.
            "members": answers_by_id(answers, members),
            "self_contribution": self_contribution,
            "confidential": conf,
        }
        try:
            save_response_by_id(vault, slug, member_id, record)
            st.session_state[prior_key] = record
            st.success("Thank you — your evaluation has been recorded. You may reopen "
                       "this link to revise it until the evaluation closes.")
            st.balloons()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Sorry, we couldn't save your response: {exc}")

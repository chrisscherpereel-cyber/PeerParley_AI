"""Contact lists, including the one PeerParley ships.

The 2.17.0 audit uploaded `assets/ContactList_Template.xlsx` exactly as
shipped and got a student called **"nan nan"**. Two things combined. The
template's formula columns run down a hundred rows and leave a space in every
unused one, so those rows were read as rows. And the reader did
`str(cell).strip()`, and `str()` of a missing pandas value is the text "nan".
Any spreadsheet with stray content beside a blank name did the same; the
template guaranteed it.

The same `str()` labelled a numeric Team column "1.0", "2.0" — on screen and in
every student's email — and the team column was found by the first header that
merely *contained* "team", which in the template's own layout could be "Number
on Team": every student would have landed on a team named after its size.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import io
import os
import sys

import pandas as pd
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from peerparley import ingest, survey  # noqa: E402

TEMPLATE = os.path.join(REPO, "assets", "ContactList_Template.xlsx")


def _xlsx(frame: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    frame.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def _names(body):
    return ({t: [m["name"] for m in ms] for t, ms in body["teams"].items()},
            [m["name"] for m in body["unassigned"]])


# --------------------------------------------------------------------------- #
# The shipped template
# --------------------------------------------------------------------------- #
def test_the_template_as_shipped_has_no_phantom_students():
    df = ingest.read_table(open(TEMPLATE, "rb"), "ContactList_Template.xlsx")
    assert len(df) == 1, f"the template read as {len(df)} rows, not its one example"
    teams, loose = _names(survey.build_roster(df))
    everyone = [n for ns in teams.values() for n in ns] + loose
    assert everyone == ["AAA BBB"]
    assert not any("nan" in n.lower() for n in everyone)


def test_leaving_the_example_student_in_is_pointed_out():
    df = ingest.read_table(open(TEMPLATE, "rb"), "ContactList_Template.xlsx")
    errors, warnings = survey.contact_list_problems(df)
    assert not errors
    assert any("AAA BBB" in w for w in warnings)


def test_a_filled_template_saved_by_excel_reads_correctly():
    """Excel caches the formula results: "Team Member 1…" holds teammates'
    names, "Number on Team" a count — and both headers contain "team"."""
    students = [("Ann", "Apple", 1), ("Bob", "Brown", 1), ("Cid", "Cole", 1),
                ("Dee", "Diaz", 2), ("Eve", "Ellis", 2), ("Finn", "Ford", 2)]
    rows = []
    for i, (f, l, t) in enumerate(students, start=1):
        mates = [f"{a} {b}" for a, b, tt in students if tt == t]
        row = {"FirstName": f, "LastName": l, "PrimaryEmail": f"{f}@nau.edu",
               "ExternalDataReference": i, "Class": "MGT 301", "Section": 1,
               "Team": t, "Number on Team": len(mates)}
        for k in range(1, 7):
            row[f"Team Member {k}"] = mates[k - 1] if k <= len(mates) else " "
        rows.append(row)
    for _ in range(40):                      # the unused formula rows
        rows.append({"FirstName": None, "LastName": None, "PrimaryEmail": None,
                     "ExternalDataReference": "", "Class": "", "Section": "",
                     "Team": None, "Number on Team": "",
                     **{f"Team Member {k}": " " for k in range(1, 7)}})
    df = ingest.read_table(io.BytesIO(_xlsx(pd.DataFrame(rows))), "filled.xlsx")
    assert len(df) == 6
    assert survey.contact_list_problems(df) == ([], [])
    teams, loose = _names(survey.build_roster(df))
    assert teams == {"1": ["Ann Apple", "Bob Brown", "Cid Cole"],
                     "2": ["Dee Diaz", "Eve Ellis", "Finn Ford"]}
    assert loose == []
    rec = survey.build_roster(df)["teams"]["1"][0]
    assert rec["class"] == "MGT 301" and rec["section"] == "1"


# --------------------------------------------------------------------------- #
# Cells
# --------------------------------------------------------------------------- #
def test_a_blank_name_beside_other_content_is_not_a_student_called_nan():
    df = pd.DataFrame({"First Name": ["Ann", None, "Bob"],
                       "Last Name": ["Apple", None, "Brown"],
                       "Email": ["a@x", "stray@x", "b@x"],
                       "Team": ["1", "1", "1"]})
    teams, loose = _names(survey.build_roster(df))
    assert teams == {"1": ["Ann Apple", "Bob Brown"]}
    assert not loose


@pytest.mark.parametrize("team_value,label", [(1, "1"), (1.0, "1"), ("1", "1"),
                                              (" 2 ", "2"), ("Alpha", "Alpha"),
                                              (2.5, "2.5")])
def test_team_labels_are_written_the_way_a_person_would(team_value, label):
    df = pd.DataFrame({"Name": ["Ann Apple", "Bob Brown"],
                       "Team": [team_value, team_value]})
    teams, _ = _names(survey.build_roster(df))
    assert list(teams) == [label]


def test_a_whole_column_of_numeric_teams_is_not_labelled_1_point_0():
    df = ingest.read_table(io.BytesIO(_xlsx(pd.DataFrame(
        {"Name": ["Ann Apple", "Bob Brown", "Cid Cole", "Dee Diaz"],
         "Team": [1, 1, 2, 2]}))), "teams.xlsx")
    assert set(survey.build_roster(df)["teams"]) == {"1", "2"}


# --------------------------------------------------------------------------- #
# Finding the team column
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("columns,expected", [
    (["First Name", "Last Name", "Number on Team", "Team Member 1", "Team"], "Team"),
    (["First Name", "Last Name", "Team Size", "Team"], "Team"),
    (["First Name", "Last Name", "Team Name"], "Team Name"),
    (["First Name", "Last Name", "Project Team"], "Project Team"),
    (["Name", "Group"], "Group"),
    (["First Name", "Last Name", "Team Member 1", "Team Member 2"], None),
])
def test_the_team_column_is_never_a_count_or_a_member_column(columns, expected):
    df = pd.DataFrame([["x"] * len(columns)], columns=columns)
    assert survey._contact_columns(df)[4] == expected


def test_a_list_with_no_team_column_says_so():
    df = pd.DataFrame({"First Name": ["Ann"], "Last Name": ["Apple"],
                       "Email": ["a@x"]})
    errors, warnings = survey.contact_list_problems(df)
    assert not errors and any("No team column" in w for w in warnings)


def test_a_list_with_no_name_column_is_refused_with_its_columns_named():
    df = pd.DataFrame({"Email": ["a@x"], "Team": ["1"]})
    errors, _ = survey.contact_list_problems(df)
    assert errors and "No name column" in errors[0] and "“Email”" in errors[0]


# --------------------------------------------------------------------------- #
# Formats real spreadsheets are saved in
# --------------------------------------------------------------------------- #
GOOD = "First Name,Last Name,Email,Team\nAnn,Apple,a@x,1\nBob,Brown,b@x,1\n"


@pytest.mark.parametrize("label,data", [
    ("utf-8", GOOD.encode("utf-8")),
    ("utf-8 with BOM", ("﻿" + GOOD).encode("utf-8")),
    ("cp1252 (Excel on Windows)", GOOD.replace("Ann", "José").encode("cp1252")),
    ("utf-16 (Excel Unicode Text)", GOOD.replace(",", "\t").encode("utf-16")),
    ("semicolons (European Excel)", GOOD.replace(",", ";").encode("utf-8")),
])
def test_the_formats_spreadsheets_actually_save_in_all_read(label, data):
    df = ingest.read_table(io.BytesIO(data), "list.csv")
    teams, _ = _names(survey.build_roster(df))
    assert list(teams) == ["1"] and len(teams["1"]) == 2, label


def test_a_workbook_whose_roster_is_not_on_the_first_sheet_is_found():
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame({"Read me": ["instructions"]}).to_excel(xw, sheet_name="Notes",
                                                             index=False)
        pd.read_csv(io.StringIO(GOOD)).to_excel(xw, sheet_name="Roster",
                                                index=False)
    df = ingest.read_table(io.BytesIO(buf.getvalue()), "two-sheets.xlsx")
    assert len(survey.build_teams(df)["1"]) == 2


@pytest.mark.parametrize("label,data,name,says", [
    ("empty", b"", "x.csv", "empty"),
    ("header only", b"First Name,Last Name,Team\n", "x.csv", "no students"),
    ("a PDF", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n", "x.csv", "PDF"),
    ("binary", bytes(range(256)) * 4, "x.csv", "not a spreadsheet"),
    ("not really a workbook", b"hello", "x.xlsx", "could not be opened"),
])
def test_an_unusable_file_raises_a_message_not_a_traceback(label, data, name, says):
    with pytest.raises(ingest.InputError) as caught:
        ingest.read_table(io.BytesIO(data), name)
    assert says.lower() in str(caught.value).lower(), (label, str(caught.value))


def test_a_contact_list_uploaded_as_a_qualtrics_export_is_recognised():
    with pytest.raises(ingest.InputError, match="contact list"):
        ingest.parse_qualtrics_export(io.BytesIO(GOOD.encode()), "export.csv")

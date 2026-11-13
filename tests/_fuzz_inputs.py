"""Push malformed and awkward files through every path an upload takes.

The contact list and the Qualtrics export are the app's untrusted input: they
come from a spreadsheet someone else made, saved by whatever program they had.
Each case below is run through the same functions the app calls, in the same
order, all the way to the PDFs — and the rule is that a bad file may produce a
*message*, never a traceback.

"Handled" means one of:
  * the function returns normally (perhaps with no teams), or
  * it raises `ingest.InputError`, whose message the app shows as it stands.

Anything else escaping is a crash on the instructor's screen.

Usage:  python tests/_fuzz_inputs.py <working-dir>
"""
from __future__ import annotations

import io
import os
import sys
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

RESULTS: list = []


def contact_cases():
    import pandas as pd
    H = "First Name,Last Name,Email,Team\n"
    good = H + "Ann,Apple,ann@x.edu,1\nBob,Brown,bob@x.edu,1\nCid,Cole,cid@x.edu,1\n"
    yield "empty file", b"", ".csv"
    yield "header only", H.encode(), ".csv"
    yield "one student", (H + "Ann,Apple,ann@x.edu,1\n").encode(), ".csv"
    yield "well formed", good.encode(), ".csv"
    yield "no team column", b"First Name,Last Name,Email\nAnn,Apple,a@x\nBob,Brown,b@x\n", ".csv"
    yield "no name columns", b"Email,Team\na@x,1\nb@x,1\n", ".csv"
    yield "full-name column only", b"Name,Team\nAnn Apple,1\nBob Brown,1\n", ".csv"
    yield "single-word names", b"Name,Team\nCher,1\nMadonna,1\n", ".csv"
    yield "numeric names and teams", b"First Name,Last Name,Team\n1,2,1.0\n3,4,1\n", ".csv"
    yield "team written 1 and 1.0", (H + "Ann,Apple,a@x,1\nBob,Brown,b@x,1.0\n").encode(), ".csv"
    yield "whitespace everywhere", (H + "  Ann , Apple ,  a@x ,  1 \n Bob,Brown,b@x,1 \n").encode(), ".csv"
    yield "duplicate student", (H + "Ann,Apple,a@x,1\nAnn,Apple,a@x,1\nBob,B,b@x,1\n").encode(), ".csv"
    yield "same name on two teams", (H + "Ann,Apple,a@x,1\nBob,B,b@x,1\nAnn,Apple,a2@x,2\nCid,C,c@x,2\n").encode(), ".csv"
    yield "unicode, apostrophes, emoji", (
        H + "Zoë,O'Brien-Núñez,z@x,1\n李,小龍,l@x,1\nAmir,عبدالله,a@x,1\n🙂,Smile,s@x,1\n").encode("utf-8"), ".csv"
    yield "cp1252 accents", (H + "José,Peña,j@x,1\nRenée,Côté,r@x,1\n").encode("cp1252"), ".csv"
    yield "utf-8 with BOM", ("﻿" + good).encode("utf-8"), ".csv"
    yield "utf-16 (Excel 'Unicode text')", good.replace(",", "\t").encode("utf-16"), ".csv"
    yield "semicolon separated", good.replace(",", ";").encode(), ".csv"
    yield "quoted commas in names", (H + '"Ann, Jr.",Apple,a@x,1\n"Bob",B,b@x,1\n').encode(), ".csv"
    yield "very long name", (H + "A" * 600 + ",B,a@x,1\nBob,B,b@x,1\n").encode(), ".csv"
    yield "blank rows and trailing columns", (
        "First Name,Last Name,Email,Team,,\n,,,,,\nAnn,Apple,a@x,1,,\n,,,,,\nBob,B,b@x,1,,\n").encode(), ".csv"
    yield "every cell blank", (H + ",,,\n,,,\n").encode(), ".csv"
    yield "binary renamed .csv", bytes(range(256)) * 8, ".csv"
    yield "a PDF renamed .csv", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\n", ".csv"
    yield "garbage renamed .xlsx", b"this is not a spreadsheet", ".xlsx"
    big = H + "".join(f"S{i},L{i},s{i}@x.edu,{i % 100}\n" for i in range(500))
    yield "500 students", big.encode(), ".csv"

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame({"Notes": ["see next sheet"]}).to_excel(xw, sheet_name="Read me", index=False)
        pd.read_csv(io.StringIO(good)).to_excel(xw, sheet_name="Roster", index=False)
    yield "xlsx with the roster on sheet 2", buf.getvalue(), ".xlsx"

    buf = io.BytesIO()
    pd.read_csv(io.StringIO(good)).to_excel(buf, index=False, engine="openpyxl")
    yield "well-formed xlsx", buf.getvalue(), ".xlsx"


def run_contact(label, data, ext, pd, survey, ingest, Vault, compute,
                GradeSettings, results_to_frame, pdfgen, slugger):
    stage = "read_table"
    try:
        df = ingest.read_table(io.BytesIO(data), f"upload{ext}")
        stage = "contact_list_problems"
        # The app's exact path: a list with an error is refused with that
        # message before anything is built from it.
        errors, warnings = survey.contact_list_problems(df)
        if errors:
            raise ingest.InputError(" ".join(errors))
        stage = "build_roster"
        body = survey.build_roster(df)
        stage = "save_setup"
        vault = Vault()
        slug, teams = survey.save_setup(f"FZ{slugger()}", "1", df,
                                        dict(survey.DEFAULT_SURVEY),
                                        vault=vault, owner="admin")
        stage = "student_links"
        survey.student_links("https://pp.example", slug, teams,
                             survey.token_secret())
        stage = "grading an empty section"
        long_df = survey.responses_to_long(vault, slug)
        res = compute(long_df, GradeSettings())
        results_to_frame(res)
        stage = "summary PDF"
        pdfgen.build_section_summary_pdf(res, "FZ", "1")
        n = sum(len(v) for v in body["teams"].values())
        note = f"  [warns: {len(warnings)}]" if warnings else ""
        return "ok", (f"{len(body['teams'])} team(s), {n} student(s), "
                      f"{len(body['unassigned'])} unassigned{note}")
    except getattr(ingest, "InputError", ()) as exc:
        return "message", f"{stage}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return "CRASH", (f"{stage}: {type(exc).__name__}: {exc}"[:240]
                         + "\n" + "".join(traceback.format_tb(exc.__traceback__)[-2:]))


def qualtrics_cases(qa_regression):
    good = qa_regression.make_qualtrics_export()
    yield "the reference export", good, ".csv"
    yield "empty file", b"", ".csv"
    yield "header rows only", b"\n".join(good.split(b"\n")[:3]) + b"\n", ".csv"
    yield "a contact list by mistake", b"First Name,Last Name,Email,Team\nAnn,Apple,a@x,1\n", ".csv"
    yield "binary", bytes(range(256)) * 4, ".csv"
    lines = good.split(b"\n")
    yield "truncated mid-row", b"\n".join(lines[:5]) + b"\n" + lines[5][:20], ".csv"
    yield "points cells are text", good.replace(b",25,", b",lots,"), ".csv"


def run_qualtrics(label, data, ext, ingest, compute, GradeSettings,
                  results_to_frame, pdfgen):
    stage = "parse_qualtrics_export"
    try:
        long_df, self_evals, roster, rep = ingest.parse_qualtrics_export(
            io.BytesIO(data), f"export{ext}")
        stage = "compute"
        res = compute(long_df, GradeSettings(), self_evals=self_evals)
        stage = "results_to_frame"
        results_to_frame(res)
        stage = "PDFs"
        pdfgen.build_section_summary_pdf(res, "QX", "1")
        pdfgen.build_confidential_pdf(res, "QX", "1")
        for t in res[:1]:
            for m in t.members[:1]:
                pdfgen.build_individual_pdf(m, "1", "QX")
        return "ok", f"{len(long_df)} rows, {sum(len(t.members) for t in res)} students"
    except getattr(ingest, "InputError", ()) as exc:
        return "message", f"{stage}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return "CRASH", (f"{stage}: {type(exc).__name__}: {exc}"[:240]
                         + "\n" + "".join(traceback.format_tb(exc.__traceback__)[-2:]))


def load_export_builder():
    """Just `make_qualtrics_export` from qa_regression.py.

    qa_regression is a script with no `__main__` guard, so importing it runs
    its entire check suite as a side effect. The function is lifted out of the
    source instead, and run in a namespace with the imports it needs.
    """
    import ast
    import types
    import pandas as pd
    path = os.path.join(REPO, "qa_regression.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "make_qualtrics_export")
    module = ast.Module(body=[fn], type_ignores=[])
    ns = {"pd": pd}
    for node in tree.body:                 # the constants it refers to
        if isinstance(node, ast.Assign) and all(
                isinstance(t, ast.Name) and t.id.isupper() for t in node.targets):
            try:
                exec(compile(ast.Module(body=[node], type_ignores=[]),
                             path, "exec"), ns)
            except Exception:  # noqa: BLE001
                pass
    exec(compile(module, path, "exec"), ns)
    return types.SimpleNamespace(make_qualtrics_export=ns["make_qualtrics_export"])


def main(workdir):
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)
    from cryptography.fernet import Fernet
    os.environ.update(FERNET_KEY=Fernet.generate_key().decode(),
                      VAULT_BACKEND="local", VAULT_FOLDER="fuzz")
    import pandas as pd
    from peerparley import ingest, pdfgen, survey
    from peerparley.grading import GradeSettings, compute, results_to_frame
    from peerparley.vault import Vault
    qa_regression = load_export_builder()

    counter = iter(range(10_000))
    crashes = 0
    print("== contact lists")
    for label, data, ext in contact_cases():
        verdict, detail = run_contact(label, data, ext, pd, survey, ingest,
                                      Vault, compute, GradeSettings,
                                      results_to_frame, pdfgen,
                                      lambda: next(counter))
        crashes += verdict == "CRASH"
        print(f"  {verdict:7} {label:34} {detail}")
    print("== Qualtrics exports")
    for label, data, ext in qualtrics_cases(qa_regression):
        verdict, detail = run_qualtrics(label, data, ext, ingest, compute,
                                        GradeSettings, results_to_frame, pdfgen)
        crashes += verdict == "CRASH"
        print(f"  {verdict:7} {label:34} {detail}")
    print("CRASHES:", crashes or "none")
    return 1 if crashes else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))

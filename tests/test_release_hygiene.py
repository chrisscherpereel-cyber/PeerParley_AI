"""Release hygiene: the static checks that keep whole classes of crash out.

Each test here exists because the class it guards against has already reached
a release at least once.

**Keyless widgets in reusable functions.** Streamlit derives a widget's id from
its arguments when it has no `key`. Two calls with the same arguments in one
script run collide, and Streamlit raises `StreamlitDuplicateElementId` *while
the page is being drawn* — everything below the collision silently never
renders. Because Streamlit renders every tab's body on every run, a helper
called from two tabs runs twice per pass even though the instructor only looks
at one of them.

It has happened twice. In 2.15.0 the PDF preview's fallback download button
took the confidential report off the Results tab. The 2.17.0 audit found the
same shape in the email send paths — invitations, reminders and results all
reuse the same helpers — including the Microsoft 365 sign-in button, which
would have collided at the exact moment an instructor tried to authenticate.
Reproduced: two "Download links CSV" buttons for the same single recipient,
and the page stopped at the first.

The rule this enforces is the only version of the fix that stays true as the
app changes: **every stateful widget inside a function carries a key.**

**Unused and undefined names.** pyflakes over the whole tree, as a test, so
the cleanup in the 2.17.0 audit stays clean.

Run with:  python -m pytest tests/ -q
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Widgets that hold state, and therefore get an id Streamlit must keep unique.
STATEFUL = {
    "button", "download_button", "checkbox", "radio", "selectbox",
    "multiselect", "slider", "select_slider", "text_input", "number_input",
    "text_area", "date_input", "time_input", "file_uploader", "color_picker",
    "toggle", "data_editor", "pills", "segmented_control", "feedback",
    "audio_input", "camera_input", "chat_input",
}

# `st.form_submit_button` is namespaced by its enclosing form, so it cannot
# collide with one in a different form. The forms themselves carry keys.
EXEMPT = {"form_submit_button"}


def _source_files():
    for base, _dirs, files in os.walk(REPO):
        rel = os.path.relpath(base, REPO)
        if rel.startswith(("tests", ".git", "build", "dist")) or "__pycache__" in rel:
            continue
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(base, name)


def _keyless_widgets_in_functions():
    found = []
    for path in _source_files():
        source = open(path, encoding="utf-8").read()
        lines = source.splitlines()
        tree = ast.parse(source)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)):
                    continue
                widget = node.func.attr
                if widget not in STATEFUL or widget in EXEMPT:
                    continue
                if any(k.arg == "key" for k in node.keywords):
                    continue
                found.append(f"{os.path.relpath(path, REPO)}:{node.lineno} "
                             f"in {fn.name}(): st.{widget} — "
                             f"{lines[node.lineno - 1].strip()[:80]}")
    return found


def test_every_stateful_widget_inside_a_function_has_a_key():
    offenders = _keyless_widgets_in_functions()
    assert not offenders, (
        "A stateful widget inside a function has no key. If that function runs "
        "twice in one pass — and helpers shared between tabs do, because every "
        "tab renders on every run — Streamlit raises a duplicate-id error "
        "mid-render and the rest of the page never appears. Give each one a "
        "key built from the function's own key prefix:\n  "
        + "\n  ".join(offenders))


def test_the_guard_would_have_caught_the_2_15_0_preview_bug(tmp_path):
    """The check is only worth having if it fails on the real thing."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import streamlit as st\n"
        "def render_pdf(data):\n"
        "    st.download_button('Download PDF', data, 'preview.pdf')\n",
        encoding="utf-8")
    tree = ast.parse(bad.read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "download_button"
             and not any(k.arg == "key" for k in n.keywords)]
    assert calls, "the guard's own detection logic must flag a keyless button"


def test_pyflakes_finds_nothing_anywhere_in_the_tree():
    pytest.importorskip("pyflakes")
    targets = ["app.py", "selfcheck.py", "qa_regression.py",
               "peerparley", "tests"]
    done = subprocess.run([sys.executable, "-m", "pyflakes", *targets],
                          capture_output=True, text=True, cwd=REPO)
    assert done.returncode == 0, (
        "pyflakes found unused or undefined names. An undefined name is a "
        "NameError waiting for the code path that reaches it:\n"
        + done.stdout + done.stderr)


def test_every_module_compiles():
    import py_compile
    for path in _source_files():
        py_compile.compile(path, doraise=True)


def test_every_icon_is_one_streamlit_accepts():
    """An `icon=` that is not a single emoji or a Material icon raises
    StreamlitAPIException at render, which takes the page down.

    The 2.17.0 audit found one that had shipped: `st.info("Redrafting one
    student…", icon="↻")` in the AI writer — ↻ is an arrow, not an emoji — so
    pressing Redraft on any single narrative killed the page. The crash matrix
    could not reach it, because that path needs an existing draft and a
    configured AI provider. Checked here statically, with Streamlit's own
    validator, so no path has to be reached for the check to work.
    """
    from streamlit.string_util import validate_icon_or_emoji
    bad = []
    for path in _source_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (kw.arg == "icon" and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)):
                    try:
                        validate_icon_or_emoji(kw.value.value)
                    except Exception as exc:  # noqa: BLE001
                        bad.append(f"{os.path.relpath(path, REPO)}:{node.lineno} "
                                   f"icon={kw.value.value!r} — {exc}")
    assert not bad, ("Streamlit rejects these icons at render time:\n  "
                     + "\n  ".join(bad))

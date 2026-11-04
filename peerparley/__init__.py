"""PeerParley — Peer evaluation, made clear.

Single instructor/administrator application. Public UI runs on Streamlit Cloud;
all student PII is encrypted at rest and persisted only to a university-controlled
storage vault (Microsoft 365 / Dropbox / pCloud) behind the firewall.

v2 adds an optional, provider-agnostic AI feedback writer (``feedback_ai``,
``llm``, ``aiconfig``) that rewrites each student's peer comments into a
narrative grounded in those comments and nothing else, with an instructor
approval gate in front of anything a student sees.
"""

import sys as _sys

# Several dataclasses annotate fields as `X | None` without a
# `from __future__ import annotations`, so those annotations are evaluated when
# the class is created: on Python 3.9 the app dies at import with
# "unsupported operand type(s) for |", which tells a new user nothing. Checked
# here because `app.py` imports this module before anything else.
MIN_PYTHON = (3, 10)
if _sys.version_info < MIN_PYTHON:
    raise RuntimeError(
        "PeerParley needs Python {}.{} or newer; this is Python {}.{}.{}. "
        "Install a newer Python, then run: pip install -r requirements.txt"
        .format(MIN_PYTHON[0], MIN_PYTHON[1], *_sys.version_info[:3])
    )

__version__ = "2.14.1"

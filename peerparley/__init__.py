"""PeerParley — Peer evaluation, made clear.

Single instructor/administrator application. Public UI runs on Streamlit Cloud;
all student PII is encrypted at rest and persisted only to a university-controlled
storage vault (Microsoft 365 / Dropbox / pCloud) behind the firewall.

v2 adds an optional, provider-agnostic AI feedback writer (``feedback_ai``,
``llm``, ``aiconfig``) that rewrites each student's peer comments into a
narrative grounded in those comments and nothing else, with an instructor
approval gate in front of anything a student sees.
"""

__version__ = "2.1.2"

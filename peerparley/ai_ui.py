"""Streamlit UI for the AI feedback narrator: sidebar settings and the review panel.

Kept out of app.py for the same reason ui_helpers.py is — app.py is already the
longest file here, and this feature's UI is self-contained.

The shape of the review panel is the point of the feature, so it is worth being
explicit about it: generation is cheap and reversible, sending is neither. So the
panel makes generating a batch one click, and makes approving deliberate. Drafts
with a flag cannot be swept up by the bulk-approve action; they have to be opened
and read. An instructor who disagrees with a draft edits it in place, and their
edit is what ships — the model's version is not preserved as some more
authoritative original.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

import streamlit as st

from . import feedback_ai as fai
from .aiconfig import (
    AISettings,
    DEFAULT_PROVIDER,
    FREE_ROUTER,
    PROVIDERS,
    TONES,
    get_provider,
    get_secret,
)
from .grading import TeamResult
from .llm import estimate_cost, has_pricing

STATE_KEY = "ai_drafts"
SETTINGS_KEY = "ai_settings"


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #

def sidebar_settings() -> AISettings:
    """Render the AI panel in the sidebar and return the chosen settings.

    Returns a disabled AISettings when the instructor hasn't switched the
    feature on, so every call site can treat "off" and "not configured" the
    same way.
    """
    s: AISettings = st.session_state.get(SETTINGS_KEY) or AISettings()

    st.divider()
    st.markdown("### 🤖 AI feedback writer")
    s.enabled = st.toggle(
        "Enable", value=s.enabled, key="ai_enabled",
        help="Turns on the optional step that rewrites each student's peer "
             "comments into a readable narrative. Off by default; grading and "
             "every other part of PeerParley work exactly the same without it.",
    )
    if not s.enabled:
        st.caption("Off — student PDFs show the raw comment bullets only.")
        st.session_state[SETTINGS_KEY] = s
        return s

    keys = list(PROVIDERS)
    labels = {k: PROVIDERS[k].label for k in keys}
    idx = keys.index(s.provider) if s.provider in keys else keys.index(DEFAULT_PROVIDER)
    s.provider = st.selectbox(
        "Provider", keys, index=idx, format_func=lambda k: labels[k],
        key="ai_provider",
    )
    spec = get_provider(s.provider)
    if spec.note:
        st.caption(spec.note)

    # ---- model -----------------------------------------------------------
    models = list(spec.models)
    if models:
        default_model = s.model if s.model in models else models[0]
        s.model = st.selectbox(
            "Model", models, index=models.index(default_model),
            key=f"ai_model_{s.provider}",
        )
        if spec.allow_custom_model:
            custom = st.text_input(
                "…or another model ID", value="",
                key=f"ai_model_custom_{s.provider}",
                placeholder="e.g. mistralai/mistral-large",
                help="Overrides the dropdown. Anything the provider accepts.",
            ).strip()
            if custom:
                s.model = custom
    else:
        s.model = st.text_input(
            "Model name", value=s.model if s.provider == "local" else "",
            key="ai_model_local", placeholder="llama3.1:8b",
            help="Whatever `ollama list` (or LM Studio) shows on this machine.",
        ).strip()

    if spec.is_local:
        s.local_base_url = st.text_input(
            "Server address", value=s.local_base_url, key="ai_local_url",
            help="Ollama defaults to port 11434; LM Studio to 1234.",
        ).strip() or s.local_base_url
        st.warning(
            "A local model is only reachable when PeerParley runs on that same "
            "machine. A Streamlit Cloud deployment cannot see your laptop.",
            icon="⚠️",
        )

    # ---- key -------------------------------------------------------------
    if spec.requires_key:
        from_secrets = get_secret(spec.env_var)
        if from_secrets:
            st.success(f"Key found in secrets (`{spec.env_var}`).", icon="🔑")
            s.api_key = ""
        else:
            s.api_key = st.text_input(
                f"{spec.label} API key", value=s.api_key, type="password",
                key=f"ai_key_{s.provider}",
                help="Used for this session only and never written to disk or "
                     "the vault. For a shared deployment, put it in the app's "
                     f"secrets as {spec.env_var} instead.",
            ).strip()
            if spec.console_url:
                st.caption(f"Get a key: {spec.console_url}")
    else:
        s.api_key = ""

    # ---- writing options -------------------------------------------------
    with st.expander("Writing options"):
        tones = list(TONES)
        s.tone = st.selectbox(
            "Tone", tones,
            index=tones.index(s.tone) if s.tone in tones else 0,
            format_func=lambda t: t.capitalize(), key="ai_tone",
        )
        st.caption(TONES[s.tone])
        s.target_words = st.slider(
            "Target length (words)", 80, 400, s.target_words, 20, key="ai_words",
            help="A ceiling to aim at, not a quota. Sparse comments produce a "
                 "shorter narrative, which is the honest outcome.",
        )
        s.include_ratings = st.checkbox(
            "Let the writer see the numeric ratings", value=s.include_ratings,
            key="ai_ratings",
            help="Gives it the four dimension averages and the performance "
                 "label as context, so the narrative matches the magnitude of "
                 "the round. The figures themselves are never quoted to the "
                 "student. Confidential comments are never shared either way.",
        )
        s.extra_guidance = st.text_area(
            "Extra instruction (optional)", value=s.extra_guidance,
            key="ai_guidance", height=70,
            placeholder="e.g. Address the student as a team member, not an employee.",
            help="Added to the prompt. It cannot override the evidence rules — "
                 "an instruction to add advice will not be followed.",
        )

    # ---- grounding -------------------------------------------------------
    with st.expander("Grounding check", expanded=False):
        st.caption(
            "Citations are always checked against the real comments, with no "
            "API call — a quote that was never written is caught for free. The "
            "second pass below re-reads the finished draft and names anything "
            "that goes beyond the evidence; it costs one extra call per student."
        )
        s.verify = st.checkbox(
            "Run the second-pass audit", value=s.verify, key="ai_verify",
        )
        if s.verify:
            s.verify_threshold = st.slider(
                "Flag drafts scoring below", 0.5, 1.0, s.verify_threshold, 0.05,
                key="ai_threshold",
                help="Drafts under this score are held out of 'approve all' and "
                     "marked for a read.",
            )

    # ---- live meter ------------------------------------------------------
    drafts: Dict[str, fai.Draft] = st.session_state.get(STATE_KEY) or {}
    if drafts:
        stats = fai.batch_stats(drafts)
        u = stats["usage"]
        line = f"{u.calls} call(s) · {u.input_tokens + u.output_tokens:,} tokens"
        if spec.is_local:
            line += " · $0.00 (runs on this machine)"
        elif has_pricing(s.model):
            line += f" · ≈ ${estimate_cost(s.model, u):.4f}"
        else:
            line += " · no published price for this model"
        st.caption(line)

    ready, why = s.ready()
    if not ready:
        st.info(why)
    else:
        # Cheaper in every sense than learning the key is dead on student forty.
        if st.button("Test the key", key="ai_test_key",
                     help="Sends one tiny request to confirm the provider "
                          "accepts this key and model. Costs a few tokens."):
            st.session_state[SETTINGS_KEY] = s
            with st.spinner("Checking…"):
                ok, problem = fai.test_credentials(s)
            if ok:
                st.success(f"{spec.label} accepted the key, and `{s.model}` "
                           "answered.", icon="✅")
            else:
                st.error(problem, icon="🔑")

    if ready and s.model == FREE_ROUTER:
        st.caption(
            "The free router picks a different model per call, so wording "
            "quality varies between runs. Fine for a first draft."
        )

    st.session_state[SETTINGS_KEY] = s
    return s


# --------------------------------------------------------------------------- #
# Review panel
# --------------------------------------------------------------------------- #

def _flag_rows(draft: fai.Draft) -> None:
    for f in draft.flags:
        icon = "🔴" if f.severity == "high" else "🟡"
        origin = "citation check" if f.origin == "quote-check" else "grounding audit"
        st.markdown(f"{icon} **{f.problem}** — _{origin}_")
        if f.text and f.text != "(whole draft)":
            st.caption(f"› {f.text}")


def _source_panel(src: fai.FeedbackSource) -> None:
    st.caption(
        f"The complete evidence for {src.name} — the narrative may not say "
        "anything that isn't here."
    )
    if src.valued:
        st.markdown("**Teammates valued**")
        for c in src.valued:
            st.markdown(f"- {c}")
    if src.focus:
        st.markdown("**Teammates asked them to focus on**")
        for c in src.focus:
            st.markdown(f"- {c}")
    if src.other:
        st.markdown("**Other comments**")
        for c in src.other:
            st.markdown(f"- {c}")
    if src.ratings:
        rows = " · ".join(
            f"{label} {value:.2f}/4" + (f" ({letter})" if letter else "")
            for label, value, letter in src.ratings
        )
        st.caption(f"Ratings shared as context: {rows}"
                   + (f" · performance: {src.performance}" if src.performance else ""))
    if not src.comments:
        st.info("No written comments were submitted for this student.")


def _points_table(draft: fai.Draft) -> None:
    rows = []
    for label, points in (("Strength", draft.strength_points),
                          ("Focus", draft.focus_points)):
        for p in points:
            rows.append({
                "Kind": label,
                "Claim": p.point,
                "Cited comment": p.source,
                "Raised by": p.raised_by,
                "Citation": "✅" if p.quote_ok else f"❌ {p.quote_match:.0%}",
            })
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.caption("The model recorded no sourced points for this draft.")


def render_review_panel(teams: List[TeamResult], settings: AISettings,
                        course: str = "", eval_no: str = "") -> Dict[str, str]:
    """The AI review workflow. Returns {student key: approved narrative}.

    Called from the Results tab. The returned map is what the PDF and email
    paths use, so a draft that isn't approved here simply doesn't exist
    downstream.
    """
    drafts: Dict[str, fai.Draft] = st.session_state.setdefault(STATE_KEY, {})
    members = [m for t in teams for m in t.members]

    if not settings.enabled:
        st.caption(
            "The AI feedback writer is off. Turn it on in the sidebar to draft a "
            "readable narrative from each student's peer comments. Student PDFs "
            "currently show the raw comment bullets, which is a complete report "
            "on its own."
        )
        return {}

    ready, why = settings.ready()
    if not ready:
        st.warning(why)
        return fai.approved_narratives(drafts)

    # ---- generate --------------------------------------------------------
    missing = [m.key for m in members if m.key not in drafts]
    failed = [k for k, d in drafts.items() if not d.ok]

    g1, g2, g3 = st.columns([1.2, 1.2, 2])
    run_all = g1.button(
        f"✨ Draft feedback for all {len(members)}", key="ai_gen_all",
        type="primary" if not drafts else "secondary",
        help="Regenerates every student, replacing existing drafts. Approvals "
             "and edits for those students are cleared.",
    )
    run_rest = g2.button(
        f"Draft the {len(missing) + len(failed)} remaining", key="ai_gen_rest",
        disabled=not (missing or failed),
        help="Only students with no draft yet, plus any that errored.",
    )
    with g3:
        st.caption(
            f"{settings.spec().label} · `{settings.model}` · "
            f"{'with' if settings.verify else 'without'} the grounding audit · "
            f"{'1' if not settings.verify else '2'} call(s) per student"
        )

    targets: Optional[List[str]] = None
    if run_all:
        targets = [m.key for m in members]
    elif run_rest:
        targets = missing + failed

    if targets:
        bar = st.progress(0.0, text="Starting…")

        def _progress(i: int, total: int, name: str) -> None:
            bar.progress(i / max(total, 1), text=f"Writing {name} ({i} of {total})…")

        aborted = ""
        try:
            fresh = fai.generate_for_teams(
                teams, settings, progress=_progress, only_keys=targets,
            )
        except fai.BatchAborted as exc:
            # One credential failure, reported once. Whatever finished before
            # the stop is kept rather than thrown away with the error.
            fresh = exc.drafts
            aborted = str(exc)
        except Exception as exc:  # noqa: BLE001
            bar.empty()
            st.error(f"Could not start generation: {exc}")
            return fai.approved_narratives(drafts)

        bar.empty()
        if aborted:
            st.error(aborted, icon="🔑")
            st.caption(
                "Generation stopped at the first rejected request rather than "
                "repeating it for every remaining student. Fix the key in the "
                "sidebar, then use **Draft the remaining**."
            )
            drafts.update(fresh)
            st.session_state[STATE_KEY] = drafts
            return fai.approved_narratives(drafts)

        drafts.update(fresh)
        st.session_state[STATE_KEY] = drafts
        stats = fai.batch_stats(fresh)
        line = (
            f"{stats['written']} narrative(s) written · {stats['clean']} clean · "
            f"{stats['flagged']} flagged · {stats['thin']} too thin to write · "
            f"{stats['errors']} failed."
        )
        # Not a success when nothing was written. Saying "Drafted 40" over forty
        # failures is how an instructor ends up believing the run worked.
        if stats["written"]:
            st.success(line)
        else:
            st.error(line + "  No narratives were produced — see below.")

    if not drafts:
        st.info(
            "No drafts yet. Generating costs one or two API calls per student "
            "and changes nothing a student sees — nothing is released until you "
            "approve it below."
        )
        return {}

    # ---- batch status ----------------------------------------------------
    stats = fai.batch_stats(drafts)
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Written", stats["written"],
              help="Drafts that produced usable text. Failures and students with "
                   "too few comments are counted separately.")
    m2.metric("Approved", stats["approved"])
    m3.metric("Need a read", stats["flagged"])
    m4.metric("Unsupported claims", stats["high"])
    m5.metric("Failed", stats["errors"],
              help="Requests that errored. These wrote nothing.")

    # One banner per distinct failure, however many students it hit.
    for message, names in fai.error_groups(drafts):
        if len(names) == 1:
            continue  # a lone failure reads fine on its own row below
        st.error(
            f"**{len(names)} students failed with the same error.** {message}",
            icon="⚠️",
        )
        st.caption(
            "One cause, not " + str(len(names)) + ". Affected: "
            + ", ".join(names[:6])
            + (f", and {len(names) - 6} more." if len(names) > 6 else "")
            + "  Fix it, then use **Draft the remaining**."
        )

    if stats["high"]:
        st.warning(
            f"{stats['high']} draft(s) contain a claim the checks could not trace "
            "to any real comment. Those are the ones to read first — an invented "
            "recommendation is exactly what this feature is built to prevent.",
            icon="🔴",
        )

    a1, a2, a3 = st.columns([1.4, 1.2, 1.6])
    if a1.button(f"✅ Approve the {stats['clean']} clean draft(s)",
                 key="ai_approve_clean", disabled=not stats["clean"],
                 help="Only drafts with no flag at all. Anything flagged has to "
                      "be opened and read."):
        for d in drafts.values():
            if d.clean and d.score >= settings.verify_threshold:
                d.approved = True
        st.rerun()
    if a2.button("Clear all approvals", key="ai_unapprove",
                 disabled=not stats["approved"]):
        for d in drafts.values():
            d.approved = False
        st.rerun()
    a3.download_button(
        "⬇ Audit trail (JSON)",
        json.dumps({
            "course": course, "eval_no": eval_no,
            "provider": settings.provider, "model": settings.model,
            "tone": settings.tone, "verified": settings.verify,
            "include_ratings": settings.include_ratings,
            "extra_guidance": settings.extra_guidance,
            "drafts": [d.to_dict() for d in drafts.values()],
        }, indent=2),
        f"peerparley_ai_audit_{course or 'section'}_eval{eval_no or '1'}.json",
        "application/json",
        help="Every draft, its citations, its flags, and what you approved — "
             "the record of what an AI wrote and what a human signed off on.",
    )

    # ---- per-student review ---------------------------------------------
    st.markdown("##### Review each draft")
    st.caption(
        "Edit freely — your text is what goes in the PDF. Approve nothing you "
        "would not have written yourself."
    )

    only_flagged = st.checkbox(
        "Show only drafts needing attention", value=bool(stats["high"]),
        key="ai_filter_flagged",
    )

    by_key = {m.key: m for m in members}
    for m in members:
        d = drafts.get(m.key)
        if d is None:
            continue
        if only_flagged and (d.clean and not d.error and not d.insufficient_evidence):
            continue

        mark = ("🔴" if d.high_severity else
                "⚠️" if not d.ok else
                "🟡" if d.flags else
                "📭" if d.insufficient_evidence else
                "✅" if d.approved else "○")
        header = f"{mark} {m.team} · {m.name} — {d.summary()}"
        with st.expander(header, expanded=bool(d.high_severity) and not d.approved):
            if not d.ok:
                st.error(d.error)
                st.caption("Use *Draft the remaining* above to retry this student.")
                continue

            if d.insufficient_evidence:
                st.info(d.insufficient_evidence)
                with st.container():
                    _source_panel(fai.build_source(
                        by_key[m.key], include_ratings=settings.include_ratings))
                continue

            left, right = st.columns([1.15, 1])
            with left:
                st.markdown("**Draft — edit as you like**")
                edited = st.text_area(
                    "Narrative", value=d.edited or d.text(),
                    key=f"ai_text_{m.key}", height=260,
                    label_visibility="collapsed",
                )
                if edited.strip() != (d.edited or d.text()).strip():
                    d.edited = edited
                approve = st.checkbox(
                    "Approve — include this in the student's PDF",
                    value=d.approved, key=f"ai_ok_{m.key}",
                )
                d.approved = approve
                if d.flags and approve:
                    st.caption("Approved with flags outstanding — make sure the "
                               "text above no longer contains them.")

            with right:
                tabs = st.tabs(["Source comments", "Citations", "Flags"])
                with tabs[0]:
                    _source_panel(fai.build_source(
                        by_key[m.key], include_ratings=settings.include_ratings))
                with tabs[1]:
                    _points_table(d)
                with tabs[2]:
                    if d.flags:
                        _flag_rows(d)
                    else:
                        st.success(
                            "Nothing flagged."
                            + ("" if d.verified else
                               " (The second-pass audit did not run for this draft.)")
                        )
                    if d.verified:
                        st.caption(f"Grounding score: {d.score:.0%}")

    st.session_state[STATE_KEY] = drafts
    return fai.approved_narratives(drafts)

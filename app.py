"""AI Resume Review & Candidate Ranking — recruiter dashboard.

Run with:  streamlit run app.py

Workflow: upload a JD -> upload resumes -> configure scoring -> analyse ->
review the ranked list -> open a candidate -> record a decision -> export.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

from src.candidate_analyzer import analyze_batch, analyze_candidate
from src.config import (
    DEFAULT_WEIGHTS,
    INTERVIEW_STATUSES,
    NOT_MENTIONED,
    RECOMMENDATIONS,
    REQUIREMENT_MODES,
    SCORE_CATEGORIES,
    get_settings,
    validate_weights,
)
from src.export_docx import export_to_docx
from src.export_excel import export_to_excel
from src.jd_parser import ParsedJD, enrich_with_llm, parse_jd_file, parse_jd_text
from src.llm_client import build_client
from src.ranking import build_summary, filter_candidates, rank_candidates, to_dataframe
from src.resume_parser import ParsedResume, find_duplicates, parse_resume
from src.scoring import CandidateEvaluation
from src.storage import ExtractionCache, directory_stats, purge, save_upload
from src.utils import safe_filename, truncate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

st.set_page_config(page_title="Resume Review & Candidate Ranking",
                   page_icon="📋", layout="wide", initial_sidebar_state="expanded")

RECOMMENDATION_STYLE = {
    "Strong Match": ("#0f7b3e", "#e4f5ea"),
    "Match": ("#1d4ed8", "#e3edfd"),
    "Partial Match": ("#a16207", "#fdf4dc"),
    "Weak Match": ("#b4451c", "#fdeae1"),
    "Insufficient Information": ("#57534e", "#f0efee"),
}

st.markdown("""
<style>
  .block-container {padding-top: 2.2rem; padding-bottom: 3rem;}
  .metric-row div[data-testid="stMetricValue"] {font-size: 1.6rem;}
  .pill {display:inline-block; padding:2px 10px; border-radius:11px; font-size:0.78rem; font-weight:600;}
  .evidence {border-left:3px solid #cbd5e1; padding:4px 0 4px 11px; margin:5px 0;
             color:#334155; font-size:0.88rem;}
  .muted {color:#64748b; font-size:0.85rem;}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def init_state() -> None:
    defaults = {
        "jd": None,
        "resumes": [],          # List[ParsedResume]
        "evaluations": [],      # List[CandidateEvaluation]
        "weights": dict(DEFAULT_WEIGHTS),
        "analysis_method": "",
        "last_run": "",
        "selected_candidate": None,
        "total_uploaded": 0,
        "jd_raw_text": "",
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


init_state()
SETTINGS = get_settings()
CACHE = ExtractionCache(SETTINGS)


@st.cache_resource(show_spinner=False)
def get_client(provider: str, model: str):
    """One client per provider/model pair, reused across reruns."""
    return build_client(get_settings())


CLIENT = get_client(SETTINGS.provider, SETTINGS.default_model())


def pill(text: str) -> str:
    color, background = RECOMMENDATION_STYLE.get(text, RECOMMENDATION_STYLE["Insufficient Information"])
    return f'<span class="pill" style="color:{color};background:{background}">{text}</span>'


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar() -> None:
    st.sidebar.title("📋 Resume Ranking")
    st.sidebar.caption("Recruiter evaluation workspace")

    with st.sidebar.expander("Analysis engine", expanded=True):
        if CLIENT.is_llm:
            st.success(f"LLM: {CLIENT.describe()}")
        else:
            st.info("Offline evidence engine")
            st.caption(
                "No LLM provider is configured, so analysis runs on the built-in contextual "
                "matching engine. Set `LLM_PROVIDER` and an API key in `.env` to use a model. "
                + (f"\n\nReason: {CLIENT.init_error}" if CLIENT.init_error else ""))

    with st.sidebar.expander("Scoring weights", expanded=False):
        st.caption("Weights must total 100%.")
        weights: Dict[str, float] = {}
        for key, label in SCORE_CATEGORIES.items():
            weights[key] = st.number_input(
                label, min_value=0, max_value=100, step=1,
                value=int(st.session_state["weights"].get(key, DEFAULT_WEIGHTS[key])),
                key=f"weight_{key}")
        total = sum(weights.values())
        valid, message = validate_weights(weights)
        (st.success if valid else st.error)(f"Total: {total}%  ·  {message}")
        if valid:
            st.session_state["weights"] = weights
        if st.button("Reset to defaults", width="stretch"):
            for key, value in DEFAULT_WEIGHTS.items():
                st.session_state[f"weight_{key}"] = value
            st.session_state["weights"] = dict(DEFAULT_WEIGHTS)
            st.rerun()

    with st.sidebar.expander("Data & privacy", expanded=False):
        uploads = directory_stats(SETTINGS.upload_dir)
        outputs = directory_stats(SETTINGS.output_dir)
        cache_stats = CACHE.stats()
        st.caption(
            f"**Uploads** `{SETTINGS.upload_dir.name}/` — {uploads['files']} file(s)\n\n"
            f"**Cache** `{SETTINGS.cache_dir.name}/` — {cache_stats['entries']} entry(ies)\n\n"
            f"**Reports** `{SETTINGS.output_dir.name}/` — {outputs['files']} file(s)\n\n"
            "Resumes stay on this machine. Resume text is sent to the configured LLM "
            "provider only while an analysis is running.")
        delete_outputs = st.checkbox("Also delete generated reports", value=False)
        if st.button("🗑️ Delete all candidate data", type="secondary", width="stretch"):
            removed = purge(SETTINGS, uploads=True, cache=True, outputs=delete_outputs)
            st.session_state.update({"resumes": [], "evaluations": [], "total_uploaded": 0})
            st.success(f"Deleted {removed['uploads']} upload(s), {removed['cache']} cache "
                       f"entry(ies), {removed['outputs']} report(s).")
            st.rerun()

    if st.session_state["evaluations"]:
        st.sidebar.divider()
        st.sidebar.caption(f"Last analysis: {st.session_state['last_run']}")
        st.sidebar.caption(f"Method: {st.session_state['analysis_method']}")


# ---------------------------------------------------------------------------
# Step 1 — Job Description
# ---------------------------------------------------------------------------

def render_jd_tab() -> None:
    st.subheader("1 · Job Description")
    st.caption("Upload the JD, or paste it. Requirements are extracted and classified as "
               "mandatory or preferred — you can override every one of them below.")

    left, right = st.columns([1, 1])
    with left:
        uploaded = st.file_uploader("Upload a JD (PDF, DOCX or TXT)",
                                    type=["pdf", "docx", "txt"], key="jd_upload")
        if uploaded is not None and st.button("Extract requirements", type="primary"):
            data = uploaded.getvalue()
            save_upload(SETTINGS, f"JD_{uploaded.name}", data)
            with st.spinner("Reading the Job Description..."):
                jd = parse_jd_file(uploaded.name, data)
                if jd.ok and CLIENT.is_llm:
                    jd = enrich_with_llm(jd, CLIENT)
            st.session_state["jd"] = jd
            st.session_state["evaluations"] = []
            st.rerun()

    with right:
        pasted = st.text_area("...or paste the JD text", height=190, key="jd_paste")
        if pasted.strip() and st.button("Extract from pasted text"):
            with st.spinner("Reading the Job Description..."):
                jd = parse_jd_text(pasted, file_name="pasted_jd.txt")
                if jd.ok and CLIENT.is_llm:
                    jd = enrich_with_llm(jd, CLIENT)
            st.session_state["jd"] = jd
            st.session_state["evaluations"] = []
            st.rerun()

    jd: Optional[ParsedJD] = st.session_state["jd"]
    if jd is None:
        st.info("No Job Description loaded yet.")
        return
    if not jd.ok:
        st.error(f"Could not read this Job Description: {jd.error}")
        return

    st.divider()
    columns = st.columns(4)
    columns[0].metric("Role", truncate(jd.title or "Not stated", 28))
    columns[1].metric("Mandatory", len(jd.mandatory()))
    columns[2].metric("Preferred", len(jd.preferred()))
    columns[3].metric("Min. experience",
                      f"{jd.min_years:g} yrs" if jd.min_years is not None else "Not stated")
    st.caption(f"Requirement source: {jd.enrichment}"
               + ("  ·  Location requirement stated in the JD" if jd.requires_location()
                  else "  ·  No location requirement stated — location will not be scored"))

    st.markdown("##### Requirement controls")
    st.caption("Set each requirement to Mandatory, Preferred or Ignore. This drives scoring, "
               "the mandatory-requirements report and the ranking tie-breaks.")

    requirement_frame = pd.DataFrame([
        {"ID": r.id, "Requirement": r.text, "Category": r.category, "Mode": r.mode,
         "Why classified": r.rationale}
        for r in jd.requirements
    ])
    edited = st.data_editor(
        requirement_frame,
        hide_index=True, width="stretch", height=330, key="requirement_editor",
        column_config={
            "ID": st.column_config.TextColumn(width="small", disabled=True),
            "Requirement": st.column_config.TextColumn(width="large", disabled=True),
            "Category": st.column_config.TextColumn(width="small", disabled=True),
            "Mode": st.column_config.SelectboxColumn(options=REQUIREMENT_MODES, width="small"),
            "Why classified": st.column_config.TextColumn(width="medium", disabled=True),
        })

    modes = dict(zip(edited["ID"], edited["Mode"]))
    changed = False
    for requirement in jd.requirements:
        new_mode = modes.get(requirement.id, requirement.mode)
        if new_mode != requirement.mode:
            requirement.mode = new_mode
            requirement.source = "recruiter"
            requirement.rationale = "Set manually by the recruiter"
            changed = True
    if changed:
        st.info("Requirement modes updated. Re-run the analysis in step 3 to apply them.")

    with st.expander("Full JD text"):
        st.text(jd.text)


# ---------------------------------------------------------------------------
# Step 2 — Resumes
# ---------------------------------------------------------------------------

def _ingest(file_name: str, data: bytes, index: int) -> ParsedResume:
    """Parse a resume, using the disk cache when the same bytes were seen before."""
    from src.utils import content_hash

    cached = CACHE.get(content_hash(data))
    if cached is not None:
        cached.file_name = file_name
        return cached
    parsed = parse_resume(file_name, data, fallback_name=f"Candidate {index:03d}")
    CACHE.put(parsed)
    return parsed


def render_resume_tab() -> None:
    st.subheader("2 · Resumes")
    st.caption("Upload up to several hundred resumes at once. Extracted text is cached, so "
               "re-running against a new JD does not re-parse anything.")

    uploaded_files = st.file_uploader(
        "Upload resumes (PDF, DOCX, TXT) — multiple files supported",
        type=["pdf", "docx", "txt"], accept_multiple_files=True, key="resume_upload")

    left, right = st.columns([1, 1])
    with left:
        store_files = st.checkbox("Save uploaded files to `uploads/`", value=True,
                                  help="Keeps the original files so you can re-analyse later "
                                       "without re-uploading. Delete them any time from the sidebar.")
    with right:
        add_mode = st.checkbox("Add to the existing batch", value=False,
                               help="Leave unchecked to replace the current batch.")

    if uploaded_files and st.button("Process resumes", type="primary"):
        existing: List[ParsedResume] = list(st.session_state["resumes"]) if add_mode else []
        progress = st.progress(0.0, text="Extracting resume text...")
        start_index = len(existing) + 1

        for position, item in enumerate(uploaded_files):
            data = item.getvalue()
            if store_files:
                save_upload(SETTINGS, item.name, data)
            existing.append(_ingest(item.name, data, start_index + position))
            progress.progress((position + 1) / len(uploaded_files),
                              text=f"Extracted {position + 1} of {len(uploaded_files)} — {item.name}")
        progress.empty()

        duplicates = find_duplicates(existing)
        for resume in existing:
            if resume.file_name in duplicates:
                resume.status = "duplicate"
                resume.duplicate_of = duplicates[resume.file_name]

        st.session_state["resumes"] = existing
        st.session_state["total_uploaded"] = len(existing)
        st.session_state["evaluations"] = []
        st.rerun()

    resumes: List[ParsedResume] = st.session_state["resumes"]
    if not resumes:
        st.info("No resumes uploaded yet.")
        return

    processed = [r for r in resumes if r.ok]
    failed = [r for r in resumes if r.status in {"failed", "ocr_required"}]
    duplicates = [r for r in resumes if r.status == "duplicate"]

    st.divider()
    columns = st.columns(4)
    columns[0].metric("Total uploaded", len(resumes))
    columns[1].metric("Successfully processed", len(processed))
    columns[2].metric("Failed / OCR required", len(failed))
    columns[3].metric("Duplicates", len(duplicates))

    frame = pd.DataFrame([{
        "File": r.file_name,
        "Status": r.status,
        "Candidate": r.candidate_name,
        "Name found": "Yes" if r.name_extracted else "No — fallback used",
        "Years": r.total_years if r.total_years is not None else None,
        "Pages": r.page_count or None,
        "Extraction": r.extraction_method or "-",
        "Detail": r.error or (f"Duplicate of {r.duplicate_of}" if r.duplicate_of else ""),
    } for r in resumes])
    st.dataframe(frame, hide_index=True, width="stretch", height=290)

    if failed:
        with st.expander(f"⚠️ {len(failed)} file(s) could not be processed", expanded=True):
            for resume in failed:
                st.markdown(f"- **{resume.file_name}** — {resume.error}")
            st.caption("Fix the source files and re-upload them, or re-run just these from "
                       "step 3 once corrected. Completed results are never lost.")


# ---------------------------------------------------------------------------
# Step 3 — Analysis
# ---------------------------------------------------------------------------

def render_analysis_tab() -> None:
    st.subheader("3 · Analyse candidates")
    jd: Optional[ParsedJD] = st.session_state["jd"]
    resumes: List[ParsedResume] = st.session_state["resumes"]

    if jd is None or not jd.ok:
        st.warning("Load a Job Description in step 1 first.")
        return
    if not resumes:
        st.warning("Upload resumes in step 2 first.")
        return

    weights = st.session_state["weights"]
    valid, message = validate_weights(weights)
    if not valid:
        st.error(f"Fix the scoring weights in the sidebar before analysing: {message}")
        return

    analysable = [r for r in resumes if r.ok]
    columns = st.columns(4)
    columns[0].metric("Ready to analyse", len(analysable))
    columns[1].metric("Mandatory requirements", len(jd.mandatory()))
    columns[2].metric("Preferred requirements", len(jd.preferred()))
    columns[3].metric("Engine", "LLM" if CLIENT.is_llm else "Evidence")

    st.caption(f"Weights — " + " · ".join(
        f"{label} {weights[key]:g}" for key, label in SCORE_CATEGORIES.items()))

    left, right = st.columns([1, 3])
    with left:
        run = st.button("▶️ Run analysis", type="primary", width="stretch")
    with right:
        st.caption("Each candidate is evaluated independently. A failure on one resume never "
                   "discards results already completed.")

    if run:
        status = st.empty()
        progress = st.progress(0.0, text="Starting analysis...")

        def on_progress(done: int, total: int, name: str) -> None:
            progress.progress(min(done / max(total, 1), 1.0),
                              text=f"Analysed {done} of {total} — {truncate(name, 48)}")

        started = datetime.now()
        evaluations = analyze_batch(resumes, jd, weights, client=CLIENT,
                                    progress=on_progress, settings=SETTINGS)
        evaluations = rank_candidates(evaluations)
        elapsed = (datetime.now() - started).total_seconds()

        st.session_state["evaluations"] = evaluations
        st.session_state["last_run"] = f"{datetime.now():%d %b %Y %H:%M}"
        st.session_state["analysis_method"] = (
            f"{CLIENT.describe()}" if CLIENT.is_llm else "Offline evidence engine")
        progress.empty()
        status.success(f"Analysed {len([e for e in evaluations if e.ok])} candidate(s) in "
                       f"{elapsed:.1f}s.")
        st.rerun()

    evaluations: List[CandidateEvaluation] = st.session_state["evaluations"]
    if evaluations:
        st.divider()
        st.success(f"{len([e for e in evaluations if e.ok])} candidate(s) analysed. "
                   "Open **4 · Results** to review the ranking.")

        failed = [e for e in evaluations if e.status == "failed"]
        if failed:
            with st.expander(f"Retry {len(failed)} failed candidate(s)"):
                for evaluation in failed:
                    st.markdown(f"- **{evaluation.file_name}** — {evaluation.error}")
                if st.button("🔄 Retry failed resumes"):
                    by_name = {r.file_name: r for r in resumes}
                    retried = 0
                    for index, evaluation in enumerate(evaluations):
                        if evaluation.status != "failed":
                            continue
                        resume = by_name.get(evaluation.file_name)
                        if resume is None:
                            continue
                        evaluations[index] = analyze_candidate(resume, jd, weights, CLIENT)
                        retried += 1
                    st.session_state["evaluations"] = rank_candidates(evaluations)
                    st.success(f"Retried {retried} resume(s).")
                    st.rerun()


# ---------------------------------------------------------------------------
# Step 4 — Results
# ---------------------------------------------------------------------------

def render_results_tab() -> None:
    st.subheader("4 · Results")
    evaluations: List[CandidateEvaluation] = st.session_state["evaluations"]
    if not evaluations:
        st.info("Run the analysis in step 3 to see the ranked candidate list.")
        return

    summary = build_summary(evaluations, st.session_state["total_uploaded"] or len(evaluations))
    counts: Dict[str, int] = summary["counts"]  # type: ignore[assignment]

    st.markdown('<div class="metric-row">', unsafe_allow_html=True)
    columns = st.columns(6)
    columns[0].metric("Total candidates", summary["total_uploaded"])
    for index, name in enumerate(RECOMMENDATIONS, start=1):
        columns[index].metric(name, counts.get(name, 0))
    st.markdown("</div>", unsafe_allow_html=True)

    columns = st.columns(4)
    columns[0].metric("Processed", summary["processed"])
    columns[1].metric("Average score", summary["average_score"])
    columns[2].metric("Highest score", summary["highest_score"])
    columns[3].metric("Lowest score", summary["lowest_score"])

    st.divider()
    with st.expander("🔍 Search and filters", expanded=True):
        row1 = st.columns([2, 1, 1])
        query = row1[0].text_input(
            "Search candidates", placeholder="e.g. Google Ads, team leadership, SQL",
            help="Matches meaning, not just the exact words: 'Google Ads' also finds "
                 "candidates whose resumes say 'AdWords' or 'paid search'.")
        name_query = row1[1].text_input("Candidate name", placeholder="e.g. Mehta")
        skill_query = row1[2].text_input("Required skill", placeholder="e.g. GA4")

        row2 = st.columns([2, 1, 1])
        score_range = row2[0].slider("Score range", 0, 100, (0, 100))
        min_experience = row2[1].number_input("Min. years experience", min_value=0.0,
                                              max_value=40.0, step=0.5, value=0.0)
        mandatory_status = row2[2].selectbox(
            "Mandatory requirements", ["All", "All mandatory met", "Any mandatory missed"])

        row3 = st.columns([2, 1, 1])
        recommendations = row3[0].multiselect("Recommendation", RECOMMENDATIONS, default=[])
        industry_query = row3[1].text_input("Industry / domain", placeholder="e.g. edtech")
        education_query = row3[2].text_input("Education", placeholder="e.g. MBA")

        row4 = st.columns([2, 1, 1])
        interview_statuses = row4[0].multiselect("Interview status", INTERVIEW_STATUSES, default=[])
        shortlisted_only = row4[1].checkbox("Shortlisted only", value=False)
        include_unprocessed = row4[2].checkbox("Show unprocessed files", value=True)

    filtered = filter_candidates(
        evaluations, query=query, score_range=score_range, recommendations=recommendations,
        min_experience=min_experience or None, required_skill=skill_query,
        mandatory_status=mandatory_status, industry_query=industry_query,
        education_query=education_query, name_query=name_query,
        interview_statuses=interview_statuses, shortlisted_only=shortlisted_only,
        include_unprocessed=include_unprocessed)

    st.caption(f"Showing {len(filtered)} of {len(evaluations)} candidates. "
               "Click any column header to sort. Low scorers are never hidden.")

    frame = to_dataframe(filtered)
    display_columns = ["Rank", "Candidate Name", "Resume", "Overall Score", "Recommendation",
                       "Years of Experience", "Mandatory Met", "Mandatory Missed",
                       *SCORE_CATEGORIES.values(), "Shortlisted", "Interview Status", "Status"]
    display_columns = [c for c in display_columns if c in frame.columns]

    st.dataframe(
        frame[display_columns], hide_index=True, width="stretch", height=430,
        column_config={
            "Overall Score": st.column_config.ProgressColumn(
                "Overall Score", min_value=0, max_value=100, format="%.1f"),
            "Rank": st.column_config.NumberColumn(width="small"),
        })

    st.divider()
    render_candidate_detail(filtered)


def render_candidate_detail(candidates: List[CandidateEvaluation]) -> None:
    st.markdown("### Candidate evaluation")
    if not candidates:
        st.info("No candidates match the current filters.")
        return

    labels = [f"#{c.rank or '-'} · {c.candidate_name} · {c.overall_score:g}/100 · {c.recommendation}"
              for c in candidates]
    choice = st.selectbox("Open a candidate", range(len(candidates)),
                          format_func=lambda i: labels[i], key="candidate_picker")
    evaluation = candidates[choice]

    if not evaluation.ok:
        st.error(f"**{evaluation.file_name}** — {evaluation.status}")
        st.write(evaluation.recommendation_reason or evaluation.error)
        return

    header = st.columns([3, 1, 1])
    header[0].markdown(f"## {evaluation.candidate_name}")
    header[0].markdown(f"{pill(evaluation.recommendation)} &nbsp; "
                       f'<span class="muted">{evaluation.file_name}</span>',
                       unsafe_allow_html=True)
    header[1].metric("Overall score", f"{evaluation.overall_score:g}/100")
    total_mandatory = evaluation.mandatory_met_count() + evaluation.mandatory_missed_count()
    header[2].metric("Mandatory met", f"{evaluation.mandatory_met_count()}/{total_mandatory}")

    st.caption(f"**Why this recommendation:** {evaluation.recommendation_reason}")
    st.caption(f"Analysis method: {evaluation.analysis_method}")

    left, right = st.columns(2)
    with left:
        st.markdown("#### ✅ Why this candidate matches")
        for item in evaluation.key_strengths:
            st.markdown(f"- {item}")
    with right:
        st.markdown("#### ⚠️ Missing or weak areas")
        for item in evaluation.key_concerns:
            st.markdown(f"- {item}")

    st.markdown("#### Score breakdown")
    for category in evaluation.category_scores:
        columns = st.columns([2, 1, 6])
        columns[0].markdown(f"**{category.label}**")
        columns[1].markdown(f"`{category.display()}`")
        columns[2].progress(min(category.percentage / 100.0, 1.0))
        st.caption(category.reasoning)
        for quote in category.evidence:
            st.markdown(f'<div class="evidence">"{quote}"</div>', unsafe_allow_html=True)
    st.markdown(f"**Total: {evaluation.overall_score:g}/100**")

    tabs = st.tabs(["Mandatory requirements", "All requirements", "Resume evidence",
                    "Missing information", "Profile"])

    with tabs[0]:
        left, right = st.columns(2)
        left.markdown("**Met**")
        for item in evaluation.mandatory_requirements_met or ["None evidenced."]:
            left.markdown(f"- {item}")
        right.markdown("**Not evidenced**")
        for item in evaluation.mandatory_requirements_missed or ["None — all mandatory requirements met."]:
            right.markdown(f"- {item}")

    with tabs[1]:
        if evaluation.requirement_verdicts:
            st.dataframe(pd.DataFrame([{
                "Requirement": v.requirement, "Mode": v.mode, "Category": v.category,
                "Met": "Yes" if v.met else "No", "Relevance": f"{v.relevance:.0%}",
                "Reasoning": v.reasoning,
                "Evidence": " | ".join(v.evidence) or NOT_MENTIONED,
            } for v in evaluation.requirement_verdicts]),
                hide_index=True, width="stretch", height=330)
        else:
            st.info("No per-requirement verdicts were recorded.")

    with tabs[2]:
        st.caption("Quotes taken directly from the resume. Nothing here is inferred.")
        for item in evaluation.evidence or ["No evidence quotes were extracted."]:
            st.markdown(f'<div class="evidence">{item}</div>', unsafe_allow_html=True)

    with tabs[3]:
        st.caption("Information the resume does not provide. These are gaps to verify with the "
                   "candidate, not conclusions about them.")
        for item in evaluation.missing_information or ["Nothing material was missing."]:
            st.markdown(f"- {item}")

    with tabs[4]:
        columns = st.columns(2)
        columns[0].markdown(f"**Experience** — {evaluation.relevant_experience}")
        columns[0].markdown(f"**Location** — {evaluation.location or NOT_MENTIONED}")
        columns[0].markdown(f"**Notice period** — {evaluation.notice_period}")
        columns[1].markdown(f"**Education** — {'; '.join(evaluation.education) or NOT_MENTIONED}")
        columns[1].markdown(f"**Certifications** — {'; '.join(evaluation.certifications) or NOT_MENTIONED}")
        columns[1].markdown(f"**Skills** — {truncate(', '.join(evaluation.skills), 400) or NOT_MENTIONED}")

    st.divider()
    st.markdown("#### Recruiter decision")
    with st.form(f"decision_{evaluation.file_name}"):
        columns = st.columns([1, 1, 2])
        shortlisted = columns[0].checkbox("Shortlist", value=evaluation.shortlisted)
        interview_status = columns[1].selectbox(
            "Interview status", INTERVIEW_STATUSES,
            index=INTERVIEW_STATUSES.index(evaluation.interview_status)
            if evaluation.interview_status in INTERVIEW_STATUSES else 0)
        decision = columns[2].text_input("Final decision", value=evaluation.recruiter_decision,
                                         placeholder="e.g. Move to round 2")
        notes = st.text_area("Recruiter notes", value=evaluation.recruiter_notes, height=95,
                             placeholder="Anything the next reviewer should know...")
        if st.form_submit_button("💾 Save decision", type="primary"):
            evaluation.shortlisted = shortlisted
            evaluation.interview_status = interview_status
            evaluation.recruiter_decision = decision
            evaluation.recruiter_notes = notes
            st.success("Saved. It will be included in the Excel and DOCX exports.")


# ---------------------------------------------------------------------------
# Step 5 — Export
# ---------------------------------------------------------------------------

def render_export_tab() -> None:
    st.subheader("5 · Export")
    evaluations: List[CandidateEvaluation] = st.session_state["evaluations"]
    if not evaluations:
        st.info("Run the analysis in step 3 before exporting.")
        return

    jd: Optional[ParsedJD] = st.session_state["jd"]
    weights = st.session_state["weights"]
    summary = build_summary(evaluations, st.session_state["total_uploaded"] or len(evaluations))
    method = st.session_state["analysis_method"] or "Offline evidence engine"
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    role = safe_filename(jd.title if jd and jd.title else "role")[:40]

    left, right = st.columns(2)

    with left:
        st.markdown("#### 📊 Excel workbook")
        st.caption("Three sheets: Candidate Ranking (filterable, colour-coded), Detailed "
                   "Evaluation (per-category reasoning and evidence) and Summary.")
        if st.button("Generate Excel", type="primary", width="stretch"):
            path = SETTINGS.output_dir / f"candidate_ranking_{role}_{stamp}.xlsx"
            with st.spinner("Building the workbook..."):
                export_to_excel(evaluations, summary, weights, path, jd=jd, analysis_method=method)
            st.session_state["excel_path"] = str(path)
            st.success(f"Saved to `{path}`")

        excel_path = st.session_state.get("excel_path")
        if excel_path and Path(excel_path).exists():
            st.download_button("⬇️ Download Excel", Path(excel_path).read_bytes(),
                               file_name=Path(excel_path).name, width="stretch",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    with right:
        st.markdown("#### 📄 DOCX report")
        st.caption("A written evaluation report: ranking, per-candidate score breakdown with "
                   "reasoning, strengths, concerns, evidence and recruiter notes.")
        limit = st.number_input("Detailed sections to include (0 = all)", min_value=0,
                                max_value=500, value=0, step=5,
                                help="Large batches produce long documents. Limit this to the "
                                     "top N candidates if you only need a shortlist report.")
        if st.button("Generate DOCX", type="primary", width="stretch"):
            path = SETTINGS.output_dir / f"candidate_report_{role}_{stamp}.docx"
            with st.spinner("Building the report..."):
                export_to_docx(evaluations, summary, weights, path, jd=jd, analysis_method=method,
                               max_candidates=int(limit) or None)
            st.session_state["docx_path"] = str(path)
            st.success(f"Saved to `{path}`")

        docx_path = st.session_state.get("docx_path")
        if docx_path and Path(docx_path).exists():
            st.download_button("⬇️ Download DOCX", Path(docx_path).read_bytes(),
                               file_name=Path(docx_path).name, width="stretch",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    st.divider()
    st.caption(f"Reports are written to `{SETTINGS.output_dir}`. Delete them any time from "
               "**Data & privacy** in the sidebar.")


# ---------------------------------------------------------------------------

def main() -> None:
    render_sidebar()
    st.title("AI Resume Review & Candidate Ranking")
    st.caption("Upload a Job Description and candidate resumes, get an explainable ranking "
               "where every score traces back to evidence in the JD and the resume.")

    tabs = st.tabs(["1 · Job Description", "2 · Resumes", "3 · Analyse", "4 · Results", "5 · Export"])
    with tabs[0]:
        render_jd_tab()
    with tabs[1]:
        render_resume_tab()
    with tabs[2]:
        render_analysis_tab()
    with tabs[3]:
        render_results_tab()
    with tabs[4]:
        render_export_tab()


if __name__ == "__main__":
    main()

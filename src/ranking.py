"""Ranking, filtering and candidate search.

Every successfully processed candidate is ranked — nothing is hidden. Files that
could not be processed stay in the result set too, flagged with their reason, so
a recruiter can see exactly what happened to each upload.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd

from .config import NOT_MENTIONED, RECOMMENDATIONS, SCORE_CATEGORIES
from .scoring import CandidateEvaluation
from .utils import normalize_key, phrase_similarity, truncate

# Ordering used when a recommendation is the sort key.
RECOMMENDATION_ORDER = {name: index for index, name in enumerate(RECOMMENDATIONS)}


def rank_candidates(evaluations: Sequence[CandidateEvaluation]) -> List[CandidateEvaluation]:
    """Rank by overall score, then mandatory coverage, then relevant experience.

    Score alone leaves too many ties at scale, so mandatory alignment and
    demonstrated experience break them — both are stronger hiring signals than a
    fractional score difference.
    """
    processed = [e for e in evaluations if e.ok]
    others = [e for e in evaluations if not e.ok]

    processed.sort(
        key=lambda e: (
            -e.overall_score,
            -e.mandatory_coverage(),
            -(e.total_years or 0.0),
            -e.percentage_for("relevant_experience"),
            normalize_key(e.candidate_name),
        )
    )
    for position, evaluation in enumerate(processed, start=1):
        evaluation.rank = position

    # Unprocessed files keep rank 0 and sort to the bottom, grouped by status.
    others.sort(key=lambda e: (e.status != "duplicate", e.file_name))
    for evaluation in others:
        evaluation.rank = 0
    return processed + others


def to_dataframe(evaluations: Sequence[CandidateEvaluation]) -> pd.DataFrame:
    """Flatten evaluations into the recruiter-facing table."""
    rows: List[dict] = []
    for evaluation in evaluations:
        row = {
            "Rank": evaluation.rank or None,
            "Candidate Name": evaluation.candidate_name,
            "Resume": evaluation.file_name,
            "Overall Score": evaluation.overall_score if evaluation.ok else None,
            "Recommendation": evaluation.recommendation,
            "Status": evaluation.status,
            "Years of Experience": evaluation.total_years,
            "Relevant Experience": evaluation.relevant_experience,
        }
        for key, label in SCORE_CATEGORIES.items():
            score = evaluation.score_for(key)
            row[label] = score.display() if score else "-"
            row[f"_{key}_pct"] = score.percentage if score else 0.0
        row.update({
            "Mandatory Met": evaluation.mandatory_met_count(),
            "Mandatory Missed": evaluation.mandatory_missed_count(),
            "Mandatory Requirements Missed": "; ".join(evaluation.mandatory_requirements_missed) or "None",
            "Required Skills Matched": "; ".join(evaluation.required_skills_match) or "None",
            "Preferred Skills Matched": "; ".join(evaluation.preferred_skills_match) or "None",
            "Key Strengths": " | ".join(evaluation.key_strengths),
            "Key Concerns": " | ".join(evaluation.key_concerns),
            "Missing Information": " | ".join(evaluation.missing_information),
            "Education": "; ".join(evaluation.education) or NOT_MENTIONED,
            "Certifications": "; ".join(evaluation.certifications) or NOT_MENTIONED,
            "Location": evaluation.location,
            "Notice Period": evaluation.notice_period,
            "Shortlisted": evaluation.shortlisted,
            "Interview Status": evaluation.interview_status,
            "Recruiter Notes": evaluation.recruiter_notes,
            "Recruiter Final Decision": evaluation.recruiter_decision,
            "Error": evaluation.error,
        })
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_candidates(evaluations: Sequence[CandidateEvaluation], query: str,
                      threshold: float = 0.5) -> List[CandidateEvaluation]:
    """Search candidates by meaning, not just by literal substring.

    A query such as "Google Ads" also returns candidates whose resumes say
    "AdWords" or "paid search", because the query is matched with the same
    contextual engine used for scoring. A literal substring hit always counts.
    """
    query = (query or "").strip()
    if not query:
        return list(evaluations)

    query_key = normalize_key(query)
    scored: List[tuple[float, CandidateEvaluation]] = []

    for evaluation in evaluations:
        haystack = _search_corpus(evaluation)
        literal = 1.0 if query_key and query_key in normalize_key(" ".join(haystack)) else 0.0
        semantic = max((phrase_similarity(query, unit) for unit in haystack), default=0.0)
        relevance = max(literal, semantic)
        if relevance >= threshold:
            scored.append((relevance, evaluation))

    scored.sort(key=lambda pair: (-pair[0], pair[1].rank or 9999))
    return [evaluation for _, evaluation in scored]


def _search_corpus(evaluation: CandidateEvaluation) -> List[str]:
    """The text a candidate is searched against."""
    corpus: List[str] = [
        evaluation.candidate_name,
        evaluation.file_name,
        evaluation.relevant_experience,
        *evaluation.skills,
        *evaluation.job_titles,
        *evaluation.companies,
        *evaluation.education,
        *evaluation.certifications,
        *evaluation.evidence,
        *evaluation.key_strengths,
        *evaluation.required_skills_match,
        *evaluation.preferred_skills_match,
    ]
    for verdict in evaluation.requirement_verdicts:
        corpus.extend(verdict.evidence)
    return [item for item in corpus if item]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def filter_candidates(
    evaluations: Sequence[CandidateEvaluation],
    *,
    query: str = "",
    score_range: Optional[tuple[float, float]] = None,
    recommendations: Optional[Iterable[str]] = None,
    min_experience: Optional[float] = None,
    required_skill: str = "",
    mandatory_status: str = "All",
    industry_query: str = "",
    education_query: str = "",
    name_query: str = "",
    interview_statuses: Optional[Iterable[str]] = None,
    shortlisted_only: bool = False,
    include_unprocessed: bool = True,
) -> List[CandidateEvaluation]:
    """Apply the recruiter's filter panel. Low scorers are never hidden by default."""
    results = list(evaluations)

    if not include_unprocessed:
        results = [e for e in results if e.ok]

    if query:
        results = search_candidates(results, query)

    if name_query:
        name_key = normalize_key(name_query)
        results = [e for e in results if name_key in normalize_key(e.candidate_name)]

    if score_range:
        low, high = score_range
        results = [e for e in results if not e.ok or low <= e.overall_score <= high]

    recommendations = list(recommendations or [])
    if recommendations:
        results = [e for e in results if e.recommendation in recommendations]

    if min_experience is not None and min_experience > 0:
        # A candidate whose resume does not state experience is kept, flagged
        # rather than silently dropped — the information is missing, not absent.
        results = [e for e in results if e.total_years is None or e.total_years >= min_experience]

    if required_skill:
        results = [e for e in results if _matches_skill(e, required_skill)]

    if mandatory_status == "All mandatory met":
        results = [e for e in results if e.ok and e.mandatory_missed_count() == 0]
    elif mandatory_status == "Any mandatory missed":
        results = [e for e in results if e.ok and e.mandatory_missed_count() > 0]

    if industry_query:
        results = [e for e in results if _matches_text(e, industry_query,
                                                       extra=[e.relevant_experience, *e.companies])]

    if education_query:
        education_key = normalize_key(education_query)
        results = [e for e in results
                   if education_key in normalize_key(" ".join(e.education + e.certifications))
                   or max((phrase_similarity(education_query, unit)
                           for unit in e.education + e.certifications), default=0.0) >= 0.6]

    interview_statuses = list(interview_statuses or [])
    if interview_statuses:
        results = [e for e in results if e.interview_status in interview_statuses]

    if shortlisted_only:
        results = [e for e in results if e.shortlisted]

    return results


def _matches_skill(evaluation: CandidateEvaluation, skill: str) -> bool:
    pool = (evaluation.skills + evaluation.required_skills_match
            + evaluation.preferred_skills_match + evaluation.evidence)
    if normalize_key(skill) in normalize_key(" ".join(pool)):
        return True
    return max((phrase_similarity(skill, unit) for unit in pool), default=0.0) >= 0.6


def _matches_text(evaluation: CandidateEvaluation, query: str, extra: Sequence[str] = ()) -> bool:
    pool = list(extra) + evaluation.evidence + evaluation.companies + evaluation.job_titles
    if normalize_key(query) in normalize_key(" ".join(pool)):
        return True
    return max((phrase_similarity(query, unit) for unit in pool), default=0.0) >= 0.55


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def build_summary(evaluations: Sequence[CandidateEvaluation], total_uploaded: int) -> Dict[str, object]:
    """Headline numbers for the dashboard and the Excel summary sheet."""
    processed = [e for e in evaluations if e.ok]
    scores = [e.overall_score for e in processed]
    failed = [e for e in evaluations if e.status in {"failed", "ocr_required"}]
    duplicates = [e for e in evaluations if e.status == "duplicate"]

    counts = {name: sum(1 for e in processed if e.recommendation == name) for name in RECOMMENDATIONS}
    # An unreadable file is still "Insufficient Information" from the recruiter's
    # point of view, so count it here too.
    counts["Insufficient Information"] += len(failed)

    top = sorted(processed, key=lambda e: -e.overall_score)[:5]
    return {
        "total_uploaded": total_uploaded,
        "processed": len(processed),
        "failed": len(failed),
        "duplicates": len(duplicates),
        "counts": counts,
        "average_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "highest_score": round(max(scores), 1) if scores else 0.0,
        "lowest_score": round(min(scores), 1) if scores else 0.0,
        "top_candidates": [
            {"rank": e.rank, "name": e.candidate_name, "score": e.overall_score,
             "recommendation": e.recommendation, "file": e.file_name}
            for e in top
        ],
        "shortlisted": sum(1 for e in evaluations if e.shortlisted),
    }

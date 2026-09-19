"""Scoring model: category scores, weighting, and recommendation derivation.

Two rules drive everything here:
  1. Every significant score carries a reason and, where possible, the resume
     evidence behind it.
  2. The recommendation is NOT a pure function of the number — mandatory
     requirement evidence and information sufficiency can move it either way.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .config import (
    DEFAULT_WEIGHTS,
    NOT_MENTIONED,
    RECOMMENDATION_BANDS,
    RECOMMENDATIONS,
    SCORE_CATEGORIES,
)
from .utils import truncate


@dataclass
class CategoryScore:
    """One scored category with its explanation and supporting evidence."""

    key: str
    label: str
    score: float
    max_score: float
    reasoning: str = ""
    evidence: List[str] = field(default_factory=list)

    @property
    def percentage(self) -> float:
        return round(100.0 * self.score / self.max_score, 1) if self.max_score else 0.0

    def display(self) -> str:
        return f"{self.score:g}/{self.max_score:g}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RequirementVerdict:
    """The requirement -> evidence -> relevance -> score chain for one requirement."""

    requirement: str
    category: str
    mode: str                     # Mandatory | Preferred
    met: bool
    relevance: float              # 0-1
    evidence: List[str] = field(default_factory=list)
    reasoning: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CandidateEvaluation:
    """The complete, exportable evaluation of one candidate."""

    file_name: str
    candidate_name: str
    status: str = "processed"          # processed | failed | ocr_required | duplicate
    error: str = ""
    overall_score: float = 0.0
    recommendation: str = "Insufficient Information"
    recommendation_reason: str = ""
    analysis_method: str = "heuristic"

    relevant_experience: str = NOT_MENTIONED
    total_years: Optional[float] = None
    years_source: str = ""
    location: str = NOT_MENTIONED
    notice_period: str = NOT_MENTIONED

    category_scores: List[CategoryScore] = field(default_factory=list)
    requirement_verdicts: List[RequirementVerdict] = field(default_factory=list)

    required_skills_match: List[str] = field(default_factory=list)
    preferred_skills_match: List[str] = field(default_factory=list)
    mandatory_requirements_met: List[str] = field(default_factory=list)
    mandatory_requirements_missed: List[str] = field(default_factory=list)
    key_strengths: List[str] = field(default_factory=list)
    key_concerns: List[str] = field(default_factory=list)
    missing_information: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)

    education: List[str] = field(default_factory=list)
    certifications: List[str] = field(default_factory=list)
    job_titles: List[str] = field(default_factory=list)
    companies: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)

    # Recruiter-owned fields — never written by the analyzer.
    recruiter_notes: str = ""
    shortlisted: bool = False
    interview_status: str = "Not Reviewed"
    recruiter_decision: str = ""

    rank: int = 0
    duplicate_of: str = ""
    resume_sha: str = ""

    # -- convenience accessors ---------------------------------------------
    @property
    def ok(self) -> bool:
        return self.status == "processed"

    def score_for(self, key: str) -> Optional[CategoryScore]:
        for cs in self.category_scores:
            return_value = cs if cs.key == key else None
            if return_value is not None:
                return return_value
        return None

    def display_score(self, key: str) -> str:
        cs = self.score_for(key)
        return cs.display() if cs else "-"

    def percentage_for(self, key: str) -> float:
        cs = self.score_for(key)
        return cs.percentage if cs else 0.0

    def mandatory_met_count(self) -> int:
        return len(self.mandatory_requirements_met)

    def mandatory_missed_count(self) -> int:
        return len(self.mandatory_requirements_missed)

    def mandatory_coverage(self) -> float:
        total = self.mandatory_met_count() + self.mandatory_missed_count()
        return round(self.mandatory_met_count() / total, 4) if total else 1.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["mandatory_coverage"] = self.mandatory_coverage()
        return data


# ---------------------------------------------------------------------------
# Weighting
# ---------------------------------------------------------------------------

def normalise_weights(weights: Optional[Dict[str, float]]) -> Dict[str, float]:
    """Return a usable weight map, falling back to defaults for bad input."""
    if not weights:
        return dict(DEFAULT_WEIGHTS)
    cleaned: Dict[str, float] = {}
    for key in SCORE_CATEGORIES:
        try:
            cleaned[key] = max(0.0, float(weights.get(key, DEFAULT_WEIGHTS[key])))
        except (TypeError, ValueError):
            cleaned[key] = float(DEFAULT_WEIGHTS[key])
    total = sum(cleaned.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    if abs(total - 100.0) > 0.01:
        # Rescale proportionally so the report always totals 100.
        cleaned = {k: round(v * 100.0 / total, 2) for k, v in cleaned.items()}
    return cleaned


def build_category_scores(
    ratios: Dict[str, float],
    weights: Dict[str, float],
    reasoning: Dict[str, str],
    evidence: Optional[Dict[str, List[str]]] = None,
) -> List[CategoryScore]:
    """Turn per-category 0-1 ratios into weighted, explained category scores."""
    weights = normalise_weights(weights)
    evidence = evidence or {}
    out: List[CategoryScore] = []
    for key, label in SCORE_CATEGORIES.items():
        max_score = round(float(weights[key]), 2)
        ratio = min(max(float(ratios.get(key, 0.0) or 0.0), 0.0), 1.0)
        out.append(CategoryScore(
            key=key,
            label=label,
            score=round(ratio * max_score, 1),
            max_score=max_score,
            reasoning=reasoning.get(key, "") or "No reasoning recorded for this category.",
            evidence=[truncate(e, 300) for e in evidence.get(key, [])][:4],
        ))
    return out


def total_score(category_scores: List[CategoryScore]) -> float:
    return round(sum(cs.score for cs in category_scores), 1)


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

def _band_for(score: float) -> str:
    for threshold, label in RECOMMENDATION_BANDS:
        if score >= threshold:
            return label
    return "Weak Match"


def derive_recommendation(
    score: float,
    mandatory_met: List[str],
    mandatory_missed: List[str],
    evidence_count: int,
    resume_chars: int,
    llm_recommendation: str = "",
) -> tuple[str, str]:
    """Combine score, mandatory evidence and information sufficiency.

    Returns ``(recommendation, reason)``. The score sets a starting band; the
    mandatory-requirement picture then adjusts it, so the outcome never rests on
    the number alone.
    """
    reasons: List[str] = []

    # 1. Not enough information to judge anyone fairly.
    if resume_chars < 400 or evidence_count == 0:
        return ("Insufficient Information",
                "The resume did not contain enough readable detail to evaluate against the JD. "
                "Manual review recommended.")

    total_mandatory = len(mandatory_met) + len(mandatory_missed)
    coverage = (len(mandatory_met) / total_mandatory) if total_mandatory else 1.0

    # 2. Start from the score band, or from the model's own call when it gave one.
    if llm_recommendation in RECOMMENDATIONS and llm_recommendation != "Insufficient Information":
        recommendation = llm_recommendation
        reasons.append(f"Model assessment: {llm_recommendation}")
    else:
        recommendation = _band_for(score)
        reasons.append(f"Weighted score of {score:g}/100 falls in the '{recommendation}' band")

    order = ["Weak Match", "Partial Match", "Match", "Strong Match"]
    position = order.index(recommendation) if recommendation in order else 0

    # 3. Mandatory requirements adjust the band in both directions.
    if total_mandatory:
        if coverage >= 0.999:
            reasons.append(f"Meets all {total_mandatory} mandatory requirement(s)")
            if score >= 72 and position < 3:
                position += 1
                reasons.append("Upgraded: full mandatory coverage with a strong weighted score")
        elif coverage >= 0.75:
            reasons.append(
                f"Meets {len(mandatory_met)} of {total_mandatory} mandatory requirements; "
                f"missing: {', '.join(truncate(m, 70) for m in mandatory_missed[:3])}")
            position = min(position, 2)  # cap at "Match"
        elif coverage >= 0.5:
            reasons.append(
                f"Meets only {len(mandatory_met)} of {total_mandatory} mandatory requirements")
            position = min(position, 1)  # cap at "Partial Match"
        else:
            reasons.append(
                f"Misses most mandatory requirements ({len(mandatory_missed)} of {total_mandatory} not evidenced)")
            position = 0

    recommendation = order[position]
    return recommendation, ". ".join(reasons) + "."


def ratios_from_breakdown(breakdown: Dict[str, float], weights: Dict[str, float]) -> Dict[str, float]:
    """Convert a model's absolute score_breakdown back into 0-1 ratios.

    The model is told each category's maximum, but a value can still arrive out
    of range, so everything is clamped.
    """
    weights = normalise_weights(weights)
    ratios: Dict[str, float] = {}
    for key in SCORE_CATEGORIES:
        max_score = float(weights[key]) or 1.0
        try:
            value = float(breakdown.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        ratios[key] = min(max(value / max_score, 0.0), 1.0)
    return ratios


def rubric_table(weights: Dict[str, float]) -> str:
    """Render the category maximums for injection into the scoring prompt."""
    weights = normalise_weights(weights)
    lines = [f"  - {label}: maximum {weights[key]:g} points"
             for key, label in SCORE_CATEGORIES.items()]
    lines.append(f"  - TOTAL: {sum(weights.values()):g} points")
    return "\n".join(lines)

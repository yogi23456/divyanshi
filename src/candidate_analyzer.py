"""Candidate analysis: compare one resume against one JD and explain the result.

Two analysis paths share the same output contract (``CandidateEvaluation``):

  * LLM path — prompts the configured provider with the JD, the resume and the
    scoring rubric, and validates the strict-JSON response.
  * Evidence path — a deterministic, offline engine that walks the same
    requirement -> evidence -> relevance -> score chain using contextual phrase
    matching. It is the fallback when no provider is configured, and it is also
    what repairs an LLM response that arrives incomplete.

Neither path invents information: a requirement with no supporting text in the
resume is reported as missing, not guessed at.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .config import NOT_MENTIONED, RECOMMENDATIONS, SCORE_CATEGORIES, Settings
from .jd_parser import JDRequirement, ParsedJD
from .llm_client import LLMClient, LLMError
from .resume_parser import ParsedResume
from .scoring import (
    CandidateEvaluation,
    RequirementVerdict,
    build_category_scores,
    derive_recommendation,
    normalise_weights,
    ratios_from_breakdown,
    rubric_table,
    total_score,
)
from .utils import (
    as_list,
    best_evidence,
    normalize_key,
    or_not_mentioned,
    phrase_similarity,
    truncate,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# A requirement counts as met at this relevance or above.
MET_THRESHOLD = 0.60
# Evidence below this is too weak to quote at all.
EVIDENCE_THRESHOLD = 0.45
# A skill that only ever appears in a skills list, never in a described
# responsibility, cannot score above this. Keyword stuffing must not pay.
UNDEMONSTRATED_SKILL_CAP = 0.60


# ---------------------------------------------------------------------------
# Resume evidence index
# ---------------------------------------------------------------------------

@dataclass
class _ResumeIndex:
    """Resume text split by evidence strength.

    ``demonstrated`` holds sentences describing what the candidate actually did.
    ``listed`` holds bare skills-list entries, which are weaker evidence.
    """

    demonstrated: List[str] = field(default_factory=list)
    listed: List[str] = field(default_factory=list)
    all_units: List[str] = field(default_factory=list)
    education_units: List[str] = field(default_factory=list)


def _build_index(resume: ParsedResume) -> _ResumeIndex:
    demonstrated: List[str] = []
    seen: set[str] = set()

    for unit in resume.responsibilities:
        key = normalize_key(unit)
        if key and key not in seen:
            seen.add(key)
            demonstrated.append(unit)

    # Experience, projects and summary describe real work, so they are strong
    # evidence even when no action verb was detected.
    for section in ("experience", "projects", "summary", "achievements"):
        for unit in _section_units(resume, section):
            key = normalize_key(unit)
            if key and key not in seen:
                seen.add(key)
                demonstrated.append(unit)

    listed = [s for s in resume.skills if s]
    education_units = list(resume.education) + list(resume.certifications)
    if not education_units:
        education_units = [u for u in resume.evidence_units
                           if re.search(r"\b(degree|university|college|institute|b\.?tech|mba|bachelor|master)\b", u, re.I)]

    return _ResumeIndex(
        demonstrated=demonstrated,
        listed=listed,
        all_units=resume.evidence_units,
        education_units=education_units,
    )


def _section_units(resume: ParsedResume, section: str) -> List[str]:
    from .utils import split_evidence_units

    body = resume.sections.get(section, "")
    return split_evidence_units(body) if body else []


# ---------------------------------------------------------------------------
# Requirement evaluation (the evidence engine)
# ---------------------------------------------------------------------------

def _evaluate_location_requirement(req: JDRequirement, resume: ParsedResume) -> RequirementVerdict:
    """Location is judged against the candidate's stated location only.

    Only reached when the JD explicitly states a location requirement. When the
    resume does not state a location, the requirement is unmet *for lack of
    information* — which the reasoning says plainly rather than implying the
    candidate is in the wrong city.
    """
    if not resume.location:
        return RequirementVerdict(
            requirement=req.text, category="location", mode=req.mode, met=False, relevance=0.0,
            evidence=[],
            reasoning=f"The JD states a location requirement but the resume does not state a "
                      f"location. Current location: {NOT_MENTIONED}. Confirm with the candidate.")

    # Compare in this direction on purpose: we ask "is the candidate's location
    # named in the JD requirement?", not "does the candidate's one-word location
    # cover every word of the requirement sentence?".
    relevance = phrase_similarity(resume.location, req.text)
    if re.search(r"\bremote\b", req.text, re.I):
        relevance = max(relevance, 0.8)
    # A willingness-to-relocate statement anywhere in the resume also counts.
    relocate = [u for u in resume.evidence_units
                if re.search(r"\b(relocat\w+|open to (relocation|moving)|willing to relocate)\b", u, re.I)]
    if relocate:
        relevance = max(relevance, 0.7)

    if relevance >= MET_THRESHOLD:
        reasoning = f"Resume states location '{resume.location}', which matches the JD requirement."
        if relocate:
            reasoning += f" Resume also indicates relocation willingness: \"{truncate(relocate[0], 120)}\""
        evidence = [f"Location: {resume.location}"] + ([truncate(relocate[0], 200)] if relocate else [])
    else:
        reasoning = (f"Resume states location '{resume.location}', which does not match the JD "
                     f"location requirement, and no relocation willingness is stated.")
        evidence = [f"Location: {resume.location}"]

    return RequirementVerdict(requirement=req.text, category="location", mode=req.mode,
                              met=relevance >= MET_THRESHOLD, relevance=round(relevance, 3),
                              evidence=evidence, reasoning=reasoning)


def _evaluate_requirement(req: JDRequirement, index: _ResumeIndex,
                          resume: Optional[ParsedResume] = None) -> RequirementVerdict:
    """Walk requirement -> evidence -> relevance -> score for one requirement."""
    if req.category == "location" and resume is not None:
        return _evaluate_location_requirement(req, resume)

    strong = best_evidence(req.text, index.demonstrated, limit=2)
    strong_score = strong[0][1] if strong else 0.0

    listed_score = max((phrase_similarity(req.text, s) for s in index.listed), default=0.0)
    listed_hits = [s for s in index.listed if phrase_similarity(req.text, s) >= 0.8]

    pool = index.education_units if req.category in {"education", "certification"} else index.all_units
    fallback = best_evidence(req.text, pool, limit=2)
    fallback_score = fallback[0][1] if fallback else 0.0

    if strong_score >= max(listed_score, fallback_score):
        relevance = strong_score
        evidence = [text for text, _ in strong]
        reasoning = (f"Demonstrated in described work: matched resume evidence at "
                     f"{relevance:.0%} contextual relevance.")
    elif fallback_score >= listed_score:
        relevance = fallback_score
        evidence = [text for text, _ in fallback]
        reasoning = f"Supporting evidence found elsewhere in the resume ({relevance:.0%} contextual relevance)."
    else:
        # Only a skills-list mention — capped, and the reason says why.
        relevance = min(listed_score, UNDEMONSTRATED_SKILL_CAP)
        evidence = [f"Skills section: {listed_hits[0]}"] if listed_hits else []
        reasoning = ("Listed in the skills section but not demonstrated in any described "
                     "responsibility, so the score is capped.")

    if relevance < EVIDENCE_THRESHOLD:
        relevance = round(relevance, 3)
        return RequirementVerdict(
            requirement=req.text, category=req.category, mode=req.mode, met=False,
            relevance=relevance, evidence=[],
            reasoning=f"{NOT_MENTIONED} — no supporting evidence found for this requirement.")

    return RequirementVerdict(
        requirement=req.text, category=req.category, mode=req.mode,
        met=relevance >= MET_THRESHOLD, relevance=round(relevance, 3),
        evidence=[truncate(e, 300) for e in evidence], reasoning=reasoning)


def _mean(values: Sequence[float]) -> float:
    values = [v for v in values]
    return sum(values) / len(values) if values else 0.0


def _experience_ratio(resume: ParsedResume, jd: ParsedJD, verdicts: List[RequirementVerdict]) -> Tuple[float, str, List[str]]:
    """Score relevant experience: role relevance first, duration second."""
    notes: List[str] = []
    evidence: List[str] = []

    exp_verdicts = [v for v in verdicts if v.category in {"experience", "responsibility"}]
    role_verdicts = exp_verdicts or [v for v in verdicts if v.mode == "Mandatory"]
    role_ratio = _mean([v.relevance for v in role_verdicts]) if role_verdicts else 0.0

    # Title relevance against the JD title / responsibilities.
    title_ratio = 0.0
    if resume.job_titles:
        probe = jd.title or " ".join(jd.responsibilities[:3])
        if probe:
            title_ratio = max(phrase_similarity(probe, t) for t in resume.job_titles)
            best_title = max(resume.job_titles, key=lambda t: phrase_similarity(probe, t))
            notes.append(f"Most relevant title held: {best_title}")

    for verdict in sorted(role_verdicts, key=lambda v: -v.relevance)[:3]:
        evidence.extend(verdict.evidence[:1])

    # Duration, measured only against a minimum the JD actually states.
    duration_ratio = None
    if jd.min_years is not None:
        if resume.total_years is None:
            duration_ratio = 0.5
            notes.append(f"JD asks for {jd.min_years:g}+ years; the resume does not state total "
                         f"experience and it could not be derived from employment dates.")
        else:
            duration_ratio = min(resume.total_years / jd.min_years, 1.0) if jd.min_years else 1.0
            if resume.total_years >= jd.min_years:
                notes.append(f"{resume.total_years:g} years of experience ({resume.years_source}) "
                             f"meets the JD minimum of {jd.min_years:g}.")
            else:
                notes.append(f"{resume.total_years:g} years of experience ({resume.years_source}) "
                             f"is below the JD minimum of {jd.min_years:g}.")
    elif resume.total_years is not None:
        notes.append(f"{resume.total_years:g} years of experience ({resume.years_source}). "
                     f"The JD states no minimum, so duration does not affect this score.")
    else:
        notes.append("The JD states no minimum experience and the resume does not state a total, "
                     "so this score reflects demonstrated relevance only.")

    signals = [(role_ratio, 0.55), (title_ratio, 0.20)]
    if duration_ratio is not None:
        signals.append((duration_ratio, 0.25))
    weight_sum = sum(w for _, w in signals)
    ratio = sum(value * weight for value, weight in signals) / weight_sum if weight_sum else 0.0

    reasoning = (f"Role relevance {role_ratio:.0%}"
                 + (f", title relevance {title_ratio:.0%}" if resume.job_titles else "")
                 + (f", duration fit {duration_ratio:.0%}" if duration_ratio is not None else "")
                 + ". " + " ".join(notes))
    return min(ratio, 1.0), reasoning.strip(), evidence


def _education_ratio(resume: ParsedResume, jd: ParsedJD, verdicts: List[RequirementVerdict]) -> Tuple[float, str, List[str]]:
    edu_verdicts = [v for v in verdicts if v.category in {"education", "certification"}]
    has_education = bool(resume.education)

    if not edu_verdicts:
        if has_education:
            return (1.0,
                    "The JD states no specific education requirement, and the resume documents a "
                    f"qualification ({truncate(resume.education[0], 90)}). No basis to deduct.",
                    resume.education[:2])
        return (0.7,
                "The JD states no specific education requirement and the resume does not clearly "
                "state a qualification. Scored neutrally rather than penalised.",
                [])

    ratio = _mean([v.relevance for v in edu_verdicts])
    met = [v for v in edu_verdicts if v.met]
    missed = [v for v in edu_verdicts if not v.met]
    parts = [f"Matched {len(met)} of {len(edu_verdicts)} education/certification requirement(s)."]
    if missed:
        parts.append("Not evidenced: " + "; ".join(truncate(v.requirement, 80) for v in missed[:3]) + ".")
    evidence: List[str] = []
    for verdict in edu_verdicts:
        evidence.extend(verdict.evidence[:1])
    if resume.certifications:
        parts.append(f"Certifications on file: {truncate('; '.join(resume.certifications[:3]), 160)}.")
    return ratio, " ".join(parts), evidence[:3]


def _industry_ratio(resume: ParsedResume, jd: ParsedJD, index: _ResumeIndex,
                    verdicts: List[RequirementVerdict]) -> Tuple[float, str, List[str]]:
    industry_verdicts = [v for v in verdicts if v.category == "industry"]
    targets = [v.requirement for v in industry_verdicts] or list(jd.industry_requirements)

    if not targets:
        return (0.7,
                "The JD does not state an industry or domain requirement, so this category is "
                "scored neutrally rather than penalised.",
                [])

    ratios: List[float] = []
    evidence: List[str] = []
    matched: List[str] = []
    for target in targets:
        hits = best_evidence(target, index.demonstrated or index.all_units, limit=1)
        # Companies carry domain signal too (e.g. a known edtech employer).
        company_score = max((phrase_similarity(target, c) for c in resume.companies), default=0.0)
        score = max(hits[0][1] if hits else 0.0, company_score)
        ratios.append(score)
        if score >= MET_THRESHOLD:
            matched.append(truncate(target, 70))
            if hits:
                evidence.append(hits[0][0])

    ratio = _mean(ratios)
    if matched:
        reasoning = f"Domain evidence found for: {'; '.join(matched[:3])}."
    elif ratio >= 0.25:
        reasoning = (f"Only indirect/adjacent domain evidence ({ratio:.0%} relevance) for: "
                     + "; ".join(truncate(t, 70) for t in targets[:3])
                     + ". No explicit experience in the domain the JD asks for.")
    else:
        reasoning = ("No resume evidence of the industry/domain the JD asks for: "
                     + "; ".join(truncate(t, 70) for t in targets[:3]) + ".")
    return ratio, reasoning, evidence[:3]


def _jd_alignment_ratio(verdicts: List[RequirementVerdict], jd: ParsedJD,
                        index: _ResumeIndex) -> Tuple[float, str, List[str]]:
    """How well the candidate's overall profile lines up with the JD as a whole."""
    if not verdicts:
        return 0.0, "No JD requirements were available to compare against.", []

    coverage = _mean([v.relevance for v in verdicts])
    resp_targets = jd.responsibilities[:10]
    resp_scores: List[float] = []
    evidence: List[str] = []
    for target in resp_targets:
        hits = best_evidence(target, index.demonstrated or index.all_units, limit=1)
        resp_scores.append(hits[0][1] if hits else 0.0)
        if hits and hits[0][1] >= MET_THRESHOLD:
            evidence.append(hits[0][0])

    if resp_scores:
        ratio = 0.6 * coverage + 0.4 * _mean(resp_scores)
        reasoning = (f"Overall requirement coverage {coverage:.0%}; evidence aligns with "
                     f"{sum(1 for s in resp_scores if s >= MET_THRESHOLD)} of {len(resp_scores)} "
                     f"key responsibilities in the JD.")
    else:
        ratio = coverage
        reasoning = f"Overall requirement coverage {coverage:.0%} across {len(verdicts)} JD requirements."
    return min(ratio, 1.0), reasoning, evidence[:3]


def analyze_with_evidence_engine(resume: ParsedResume, jd: ParsedJD,
                                 weights: Dict[str, float]) -> CandidateEvaluation:
    """Deterministic, fully explainable analysis with no model call."""
    index = _build_index(resume)
    active = jd.active_requirements()
    verdicts = [_evaluate_requirement(req, index, resume) for req in active]

    mandatory = [v for v in verdicts if v.mode == "Mandatory"]
    preferred = [v for v in verdicts if v.mode == "Preferred"]
    skill_mandatory = [v for v in mandatory if v.category in {"skill", "certification", "experience"}] or mandatory

    required_ratio = _mean([v.relevance for v in skill_mandatory])
    preferred_ratio = _mean([v.relevance for v in preferred]) if preferred else 0.7

    exp_ratio, exp_reason, exp_evidence = _experience_ratio(resume, jd, verdicts)
    edu_ratio, edu_reason, edu_evidence = _education_ratio(resume, jd, verdicts)
    ind_ratio, ind_reason, ind_evidence = _industry_ratio(resume, jd, index, verdicts)
    jd_ratio, jd_reason, jd_evidence = _jd_alignment_ratio(verdicts, jd, index)

    met_count = sum(1 for v in skill_mandatory if v.met)
    required_reason = (
        f"Evidenced {met_count} of {len(skill_mandatory)} required skill/experience requirement(s). "
        + ("Not evidenced: " + "; ".join(truncate(v.requirement, 70) for v in skill_mandatory if not v.met)[:400] + "."
           if met_count < len(skill_mandatory) else "All required items are supported by resume evidence."))

    preferred_met = sum(1 for v in preferred if v.met)
    preferred_reason = (
        f"Evidenced {preferred_met} of {len(preferred)} preferred requirement(s)." if preferred
        else "The JD lists no preferred requirements, so this category is scored neutrally.")

    ratios = {
        "relevant_experience": exp_ratio,
        "required_skills": required_ratio,
        "jd_alignment": jd_ratio,
        "industry_relevance": ind_ratio,
        "education": edu_ratio,
        "preferred_skills": preferred_ratio,
    }
    reasoning = {
        "relevant_experience": exp_reason,
        "required_skills": required_reason,
        "jd_alignment": jd_reason,
        "industry_relevance": ind_reason,
        "education": edu_reason,
        "preferred_skills": preferred_reason,
    }
    evidence_map = {
        "relevant_experience": exp_evidence,
        "required_skills": [e for v in skill_mandatory if v.met for e in v.evidence[:1]][:3],
        "jd_alignment": jd_evidence,
        "industry_relevance": ind_evidence,
        "education": edu_evidence,
        "preferred_skills": [e for v in preferred if v.met for e in v.evidence[:1]][:3],
    }

    category_scores = build_category_scores(ratios, weights, reasoning, evidence_map)
    score = total_score(category_scores)

    evaluation = CandidateEvaluation(
        file_name=resume.file_name,
        candidate_name=resume.candidate_name,
        analysis_method="evidence-engine",
        overall_score=score,
        category_scores=category_scores,
        requirement_verdicts=verdicts,
        mandatory_requirements_met=[truncate(v.requirement, 160) for v in mandatory if v.met],
        mandatory_requirements_missed=[truncate(v.requirement, 160) for v in mandatory if not v.met],
        required_skills_match=[truncate(v.requirement, 120) for v in mandatory if v.met
                               and v.category in {"skill", "certification"}],
        preferred_skills_match=[truncate(v.requirement, 120) for v in preferred if v.met],
        resume_sha=resume.text_sha,
    )
    _fill_common_fields(evaluation, resume, jd, verdicts)

    evaluation.key_strengths = _build_strengths(verdicts, resume, jd)
    evaluation.key_concerns = _build_concerns(verdicts, resume, jd)
    evaluation.evidence = _collect_evidence(verdicts)

    evaluation.recommendation, evaluation.recommendation_reason = derive_recommendation(
        score=score,
        mandatory_met=evaluation.mandatory_requirements_met,
        mandatory_missed=evaluation.mandatory_requirements_missed,
        evidence_count=len(evaluation.evidence),
        resume_chars=resume.char_count,
    )
    return evaluation


def _fill_common_fields(evaluation: CandidateEvaluation, resume: ParsedResume,
                        jd: ParsedJD, verdicts: List[RequirementVerdict]) -> None:
    """Populate the resume-derived fields both analysis paths share."""
    evaluation.total_years = resume.total_years
    evaluation.years_source = resume.years_source
    evaluation.education = resume.education or []
    evaluation.certifications = resume.certifications or []
    evaluation.job_titles = resume.job_titles or []
    evaluation.companies = resume.companies or []
    evaluation.skills = resume.skills or []

    # Location is only relevant when the JD explicitly asks for it.
    if jd.requires_location():
        evaluation.location = or_not_mentioned(resume.location)
    else:
        evaluation.location = resume.location or NOT_MENTIONED
    evaluation.notice_period = or_not_mentioned(resume.notice_period)

    if not evaluation.relevant_experience or evaluation.relevant_experience == NOT_MENTIONED:
        evaluation.relevant_experience = _describe_experience(resume)

    evaluation.missing_information = _build_missing_information(resume, jd, verdicts)


def _describe_experience(resume: ParsedResume) -> str:
    if resume.total_years is not None:
        head = f"{resume.total_years:g} years ({resume.years_source})"
    else:
        head = f"Total years: {NOT_MENTIONED}"
    if resume.job_titles:
        head += f" · Most recent relevant title: {resume.job_titles[0]}"
    if resume.companies:
        head += f" · {truncate(', '.join(resume.companies[:3]), 90)}"
    return head


def _build_missing_information(resume: ParsedResume, jd: ParsedJD,
                               verdicts: List[RequirementVerdict]) -> List[str]:
    """Only genuine information gaps — never speculation about the candidate."""
    missing: List[str] = []
    if resume.total_years is None:
        missing.append(f"Total years of experience: {NOT_MENTIONED}")
    if not resume.education:
        missing.append(f"Education / qualifications: {NOT_MENTIONED}")
    if not resume.skills:
        missing.append(f"Dedicated skills section: {NOT_MENTIONED}")
    if jd.requires_location() and not resume.location:
        missing.append(f"Current location (the JD states a location requirement): {NOT_MENTIONED}")
    if not resume.notice_period:
        missing.append(f"Notice period: {NOT_MENTIONED}")
    if not resume.name_extracted:
        missing.append("Candidate name could not be extracted from the resume.")

    silent = [v for v in verdicts if v.mode == "Mandatory" and not v.evidence]
    for verdict in silent[:5]:
        missing.append(f"No resume content addressing: {truncate(verdict.requirement, 120)}")
    return missing


def _build_strengths(verdicts: List[RequirementVerdict], resume: ParsedResume, jd: ParsedJD) -> List[str]:
    strengths: List[str] = []
    strong = sorted([v for v in verdicts if v.met], key=lambda v: -v.relevance)
    for verdict in strong[:5]:
        quote = f" Evidence: \"{truncate(verdict.evidence[0], 150)}\"" if verdict.evidence else ""
        strengths.append(f"{truncate(verdict.requirement, 110)} — {verdict.relevance:.0%} relevance.{quote}")
    if jd.min_years is not None and resume.total_years is not None and resume.total_years >= jd.min_years:
        strengths.insert(0, f"Meets the JD minimum of {jd.min_years:g} years with "
                            f"{resume.total_years:g} years ({resume.years_source}).")
    return strengths[:6] or ["No standout strengths were evidenced against this JD."]


def _build_concerns(verdicts: List[RequirementVerdict], resume: ParsedResume, jd: ParsedJD) -> List[str]:
    concerns: List[str] = []
    missed_mandatory = [v for v in verdicts if v.mode == "Mandatory" and not v.met]
    for verdict in missed_mandatory[:5]:
        detail = ("no supporting evidence in the resume" if not verdict.evidence
                  else f"only partial evidence ({verdict.relevance:.0%} relevance)")
        concerns.append(f"Mandatory requirement not met — {truncate(verdict.requirement, 110)}: {detail}.")
    if jd.min_years is not None:
        if resume.total_years is None:
            concerns.append(f"The JD requires {jd.min_years:g}+ years but the resume does not state "
                            f"total experience — verify with the candidate.")
        elif resume.total_years < jd.min_years:
            concerns.append(f"Experience gap: {resume.total_years:g} years against the JD minimum "
                            f"of {jd.min_years:g}.")
    weak_preferred = [v for v in verdicts if v.mode == "Preferred" and not v.met]
    if weak_preferred:
        concerns.append("Preferred requirements not evidenced: "
                        + truncate("; ".join(v.requirement for v in weak_preferred[:3]), 220))
    return concerns[:6] or ["No material concerns were identified against this JD."]


def _collect_evidence(verdicts: List[RequirementVerdict]) -> List[str]:
    """Evidence quotes, tagged with the requirement they support."""
    out: List[str] = []
    seen: set[str] = set()
    for verdict in sorted(verdicts, key=lambda v: (-v.relevance)):
        for quote in verdict.evidence[:1]:
            key = normalize_key(quote)
            if key and key not in seen:
                seen.add(key)
                out.append(f"[{truncate(verdict.requirement, 60)}] {quote}")
    return out[:12]


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------

def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _build_analysis_prompt(resume: ParsedResume, jd: ParsedJD, weights: Dict[str, float]) -> str:
    rubric = _load_prompt("scoring_prompt.txt").replace("{{CATEGORY_TABLE}}", rubric_table(weights))
    return (_load_prompt("resume_analysis.txt")
            .replace("{{SCORING_RUBRIC}}", rubric)
            .replace("{{JD_BLOCK}}", jd.prompt_view())
            .replace("{{FILE_NAME}}", resume.file_name)
            .replace("{{CANDIDATE_NAME}}", resume.candidate_name)
            .replace("{{RESUME_TEXT}}", truncate(resume.text, 18000)))


def analyze_with_llm(resume: ParsedResume, jd: ParsedJD, weights: Dict[str, float],
                     client: LLMClient) -> CandidateEvaluation:
    """Prompt the configured model and validate its strict-JSON response."""
    payload = client.complete_json(_build_analysis_prompt(resume, jd, weights),
                                   purpose=f"analyze:{resume.file_name}")
    if not payload:
        raise LLMError("The model did not return parseable JSON for this resume.")

    weights = normalise_weights(weights)
    breakdown = payload.get("score_breakdown") or {}
    if not isinstance(breakdown, dict):
        breakdown = {}
    ratios = ratios_from_breakdown(breakdown, weights)

    model_reasoning = payload.get("category_reasoning") or {}
    if not isinstance(model_reasoning, dict):
        model_reasoning = {}
    reasoning = {key: str(model_reasoning.get(key) or "").strip() or
                 f"Model scored {SCORE_CATEGORIES[key]} at {ratios.get(key, 0):.0%} of its maximum."
                 for key in SCORE_CATEGORIES}

    # The evidence engine still runs: it supplies per-requirement traceability
    # and quotes the model may not have surfaced.
    index = _build_index(resume)
    verdicts = [_evaluate_requirement(req, index, resume) for req in jd.active_requirements()]
    evidence_map: Dict[str, List[str]] = {key: [] for key in SCORE_CATEGORIES}
    for verdict in verdicts:
        if not verdict.evidence:
            continue
        bucket = {"skill": "required_skills", "certification": "education", "education": "education",
                  "industry": "industry_relevance", "experience": "relevant_experience",
                  "responsibility": "jd_alignment", "location": "jd_alignment"}.get(verdict.category, "jd_alignment")
        if verdict.mode == "Preferred" and bucket == "required_skills":
            bucket = "preferred_skills"
        if len(evidence_map[bucket]) < 3:
            evidence_map[bucket].append(verdict.evidence[0])

    category_scores = build_category_scores(ratios, weights, reasoning, evidence_map)
    score = total_score(category_scores)

    model_name = str(payload.get("candidate_name") or "").strip()
    candidate_name = model_name if 1 < len(model_name) <= 60 else resume.candidate_name

    mandatory_met = as_list(payload.get("mandatory_requirements_met"))
    mandatory_missed = as_list(payload.get("mandatory_requirements_missed"))
    if not mandatory_met and not mandatory_missed:
        # Model skipped the field — fall back to engine verdicts so the
        # mandatory picture is never silently empty.
        mandatory_met = [truncate(v.requirement, 160) for v in verdicts if v.mode == "Mandatory" and v.met]
        mandatory_missed = [truncate(v.requirement, 160) for v in verdicts if v.mode == "Mandatory" and not v.met]

    llm_evidence = as_list(payload.get("evidence"))
    evaluation = CandidateEvaluation(
        file_name=resume.file_name,
        candidate_name=candidate_name,
        analysis_method=f"llm:{client.provider_name}/{client.model_name}",
        overall_score=score,
        category_scores=category_scores,
        requirement_verdicts=verdicts,
        required_skills_match=as_list(payload.get("required_skills_match")),
        preferred_skills_match=as_list(payload.get("preferred_skills_match")),
        mandatory_requirements_met=mandatory_met,
        mandatory_requirements_missed=mandatory_missed,
        key_strengths=as_list(payload.get("key_strengths")) or _build_strengths(verdicts, resume, jd),
        key_concerns=as_list(payload.get("key_concerns")) or _build_concerns(verdicts, resume, jd),
        evidence=llm_evidence or _collect_evidence(verdicts),
        relevant_experience=or_not_mentioned(str(payload.get("relevant_experience") or "")),
        resume_sha=resume.text_sha,
    )
    _fill_common_fields(evaluation, resume, jd, verdicts)

    model_missing = as_list(payload.get("missing_information"))
    for item in model_missing:
        if item not in evaluation.missing_information:
            evaluation.missing_information.append(item)

    llm_recommendation = str(payload.get("recommendation") or "").strip()
    llm_recommendation = llm_recommendation if llm_recommendation in RECOMMENDATIONS else ""
    evaluation.recommendation, evaluation.recommendation_reason = derive_recommendation(
        score=score,
        mandatory_met=evaluation.mandatory_requirements_met,
        mandatory_missed=evaluation.mandatory_requirements_missed,
        evidence_count=len(evaluation.evidence),
        resume_chars=resume.char_count,
        llm_recommendation=llm_recommendation,
    )
    return evaluation


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def analyze_candidate(resume: ParsedResume, jd: ParsedJD, weights: Dict[str, float],
                      client: Optional[LLMClient] = None) -> CandidateEvaluation:
    """Analyse one candidate, falling back to the evidence engine on LLM failure."""
    if not resume.ok:
        if resume.status == "duplicate":
            reason = (f"Duplicate of '{resume.duplicate_of}' — not analysed again. "
                      f"See the original entry for the evaluation.")
        elif resume.status == "ocr_required":
            reason = resume.error or "OCR required — the resume text could not be extracted."
        else:
            reason = resume.error or "The resume could not be processed."
        return CandidateEvaluation(
            file_name=resume.file_name,
            candidate_name=resume.candidate_name or "Unknown",
            status=resume.status,
            error=resume.error,
            recommendation="Insufficient Information",
            recommendation_reason=reason,
            duplicate_of=resume.duplicate_of,
        )

    if client is not None and client.is_llm:
        try:
            return analyze_with_llm(resume, jd, weights, client)
        except LLMError as exc:
            logger.warning("LLM analysis failed for %s (%s) — using the evidence engine.",
                           resume.file_name, exc)
            evaluation = analyze_with_evidence_engine(resume, jd, weights)
            evaluation.analysis_method = "evidence-engine (LLM call failed)"
            evaluation.key_concerns.append(f"Automated note: the LLM call failed ({truncate(str(exc), 120)}); "
                                           f"this evaluation came from the offline evidence engine.")
            return evaluation
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected analysis failure for %s", resume.file_name)
            evaluation = analyze_with_evidence_engine(resume, jd, weights)
            evaluation.analysis_method = "evidence-engine (analysis error)"
            evaluation.key_concerns.append(f"Automated note: analysis error ({truncate(str(exc), 120)}).")
            return evaluation

    return analyze_with_evidence_engine(resume, jd, weights)


def analyze_batch(
    resumes: Sequence[ParsedResume],
    jd: ParsedJD,
    weights: Dict[str, float],
    client: Optional[LLMClient] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    settings: Optional[Settings] = None,
    skip_duplicates: bool = True,
) -> List[CandidateEvaluation]:
    """Analyse many candidates concurrently.

    One failing resume never loses the rest: every worker returns an evaluation,
    and unexpected exceptions are converted into a failed evaluation record.
    """
    weights = normalise_weights(weights)
    to_process: List[ParsedResume] = []
    results: List[CandidateEvaluation] = []

    for resume in resumes:
        if not resume.ok or (skip_duplicates and resume.status == "duplicate"):
            results.append(analyze_candidate(resume, jd, weights, client=None))
        else:
            to_process.append(resume)

    total = len(to_process)
    done = 0
    if progress:
        progress(len(results), len(resumes), "Starting analysis")

    max_workers = max(1, (settings.concurrency if settings else 4)) if client and client.is_llm else 1
    if total == 0:
        return results

    if max_workers == 1:
        for resume in to_process:
            results.append(_safe_analyze(resume, jd, weights, client))
            done += 1
            if progress:
                progress(len(results), len(resumes), resume.file_name)
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_safe_analyze, r, jd, weights, client): r for r in to_process}
        for future in as_completed(futures):
            results.append(future.result())
            done += 1
            if progress:
                progress(len(results), len(resumes), futures[future].file_name)
    return results


def _safe_analyze(resume: ParsedResume, jd: ParsedJD, weights: Dict[str, float],
                  client: Optional[LLMClient]) -> CandidateEvaluation:
    try:
        return analyze_candidate(resume, jd, weights, client)
    except Exception as exc:  # noqa: BLE001 - a worker must never take down the batch
        logger.exception("Analysis worker crashed for %s", resume.file_name)
        return CandidateEvaluation(
            file_name=resume.file_name,
            candidate_name=resume.candidate_name or "Unknown",
            status="failed",
            error=f"Analysis failed: {str(exc)[:200]}",
            recommendation="Insufficient Information",
            recommendation_reason="The candidate could not be analysed. Retry this resume.",
        )

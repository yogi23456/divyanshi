"""Job Description parsing: pull out the requirements a candidate is judged on.

The heuristic pass always runs, so the recruiter immediately sees an editable
requirement list. When an LLM provider is configured, ``enrich_with_llm``
refines the classification. Either way the recruiter has the final say via the
Mandatory / Preferred / Ignore control in the UI.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .config import NOT_MENTIONED
from .resume_parser import ResumeParseError, extract_text
from .utils import (
    as_list,
    dewrap_lines,
    content_hash,
    extract_min_years,
    normalize_key,
    normalize_text,
    split_evidence_units,
    strip_bullets,
    truncate,
)

logger = logging.getLogger(__name__)

# --- Signal words ----------------------------------------------------------

MANDATORY_MARKERS = [
    "must have", "must-have", "must possess", "must be", "required", "requirement",
    "mandatory", "essential", "is a must", "minimum", "at least", "should have",
    "you have", "we require", "non-negotiable", "proven", "demonstrated",
]

PREFERRED_MARKERS = [
    "preferred", "preferable", "preferably", "good to have", "nice to have",
    "nice-to-have", "bonus", "plus", "advantage", "advantageous", "desirable",
    "ideally", "would be great", "a plus", "added advantage", "familiarity with",
    "exposure to",
]

_MANDATORY_RE = re.compile("|".join(re.escape(m) for m in MANDATORY_MARKERS), re.I)
_PREFERRED_RE = re.compile("|".join(re.escape(m) for m in PREFERRED_MARKERS), re.I)

# Headings that put every bullet beneath them into one bucket.
_SECTION_HEADINGS = {
    "mandatory": re.compile(
        r"^\s*(must[\s-]?haves?|required\s+(skills|qualifications|experience)|requirements|"
        r"minimum\s+(qualifications|requirements)|essential\s+(skills|criteria)|"
        r"what\s+you(?:'ll)?\s+need|who\s+you\s+are|qualifications|skills\s+and\s+qualifications)"
        r"(\s+(and|&|/)\s+\w+)*\s*:?\s*$", re.I),
    "preferred": re.compile(
        r"^\s*(preferred(\s+(skills|qualifications|requirements))?|good\s+to\s+have|nice\s+to\s+have|"
        r"bonus\s+points?|desirable|added\s+advantage|what\s+would\s+set\s+you\s+apart)"
        r"(\s*(/|,|and|&)\s*(good\s+to\s+have|nice\s+to\s+have|bonus|desirable))*\s*:?\s*$", re.I),
    "responsibilities": re.compile(
        r"^\s*(key\s+)?(responsibilities|what\s+you(?:'ll)?\s+do|role\s+overview|"
        r"the\s+role|job\s+description|duties)\s*:?\s*$", re.I),
    "education": re.compile(r"^\s*(education(al)?\s*(requirements|qualifications)?)\s*:?\s*$", re.I),
    "location": re.compile(r"^\s*(location|work\s+location|where\s+you(?:'ll)?\s+work|"
                           r"work\s+arrangement)\s*:?\s*$", re.I),
    "about": re.compile(r"^\s*(about\s+(us|the\s+role|the\s+company)|company\s+overview|benefits|"
                        r"perks|what\s+we\s+offer|compensation|why\s+join)\s*:?\s*$", re.I),
}

# Boilerplate that is never a candidate requirement.
_NOISE_RE = re.compile(
    r"\b(equal\s+opportunity|we\s+offer|benefits|perks|salary|ctc|compensation|apply\s+now|"
    r"send\s+your\s+resume|about\s+us|our\s+mission|founded\s+in|headquartered)\b", re.I)

_EDU_RE = re.compile(
    r"\b(degree|b\.?tech|b\.?e\.?|b\.?sc|b\.?com|b\.?a\b|bba|bca|mca|m\.?tech|m\.?sc|mba|pgdm|"
    r"ph\.?d|doctorate|bachelor|master|graduate|diploma|post\s*graduate|engineering\s+background)\b", re.I)
_EXP_RE = re.compile(r"\b(\d{1,2}\s*\+?\s*(years|yrs)|years\s+of\s+experience|experience\s+in|"
                     r"background\s+in|track\s+record)\b", re.I)
_INDUSTRY_RE = re.compile(
    r"\b(industry|domain|sector|b2b|b2c|saas|edtech|fintech|e-?commerce|healthcare|"
    r"manufacturing|bfsi|retail|telecom|gaming|logistics|media|startup|enterprise)\b", re.I)
_CERT_RE = re.compile(r"\b(certified|certification|certificate|credential|licen[cs]e)\b", re.I)
_LOCATION_RE = re.compile(
    r"\b(on-?site|in-?office|work\s+from\s+office|wfo|hybrid|remote|relocat\w*|based\s+(in|out\s+of)|"
    r"willing\s+to\s+travel|location)\b", re.I)
_SKILL_HINT_RE = re.compile(
    r"\b(proficien\w*|hands[\s-]?on|expertise|skilled|knowledge\s+of|working\s+knowledge|"
    r"familiar\w*|strong\s+command|ability\s+to\s+use|tools?|platform|stack)\b", re.I)


@dataclass
class JDRequirement:
    """One requirement extracted from the JD, as the recruiter will see it."""

    id: str
    text: str
    category: str = "skill"      # skill | experience | education | industry | certification | location | responsibility
    mode: str = "Mandatory"      # Mandatory | Preferred | Ignore
    source: str = "heuristic"    # heuristic | llm | recruiter
    rationale: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParsedJD:
    """The full structured view of a Job Description."""

    file_name: str = ""
    text: str = ""
    title: str = ""
    status: str = "processed"
    error: str = ""
    requirements: List[JDRequirement] = field(default_factory=list)
    min_years: Optional[float] = None
    required_skills: List[str] = field(default_factory=list)
    preferred_skills: List[str] = field(default_factory=list)
    education_requirements: List[str] = field(default_factory=list)
    industry_requirements: List[str] = field(default_factory=list)
    location_requirements: List[str] = field(default_factory=list)
    responsibilities: List[str] = field(default_factory=list)
    enrichment: str = "heuristic"
    sha: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "processed"

    def active_requirements(self) -> List[JDRequirement]:
        return [r for r in self.requirements if r.mode != "Ignore"]

    def mandatory(self) -> List[JDRequirement]:
        return [r for r in self.requirements if r.mode == "Mandatory"]

    def preferred(self) -> List[JDRequirement]:
        return [r for r in self.requirements if r.mode == "Preferred"]

    def requires_location(self) -> bool:
        """Location is only ever scored when the JD explicitly states it."""
        return bool(self.location_requirements)

    def prompt_view(self, char_limit: int = 9000) -> str:
        """Compact JD representation handed to the LLM."""
        lines = [f"JOB TITLE: {self.title or 'Not stated'}"]
        if self.min_years is not None:
            lines.append(f"MINIMUM YEARS OF EXPERIENCE: {self.min_years:g}")
        for label, items in (
            ("MANDATORY REQUIREMENTS", [r.text for r in self.mandatory()]),
            ("PREFERRED REQUIREMENTS", [r.text for r in self.preferred()]),
            ("EDUCATION REQUIREMENTS", self.education_requirements),
            ("INDUSTRY / DOMAIN REQUIREMENTS", self.industry_requirements),
            ("LOCATION REQUIREMENTS (stated in JD)", self.location_requirements),
            ("KEY RESPONSIBILITIES", self.responsibilities[:12]),
        ):
            if items:
                lines.append(f"\n{label}:")
                lines.extend(f"- {truncate(i, 240)}" for i in items)
        lines.append("\nFULL JD TEXT:\n" + truncate(self.text, char_limit))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Heuristic extraction
# ---------------------------------------------------------------------------

def _guess_title(text: str) -> str:
    labelled = re.search(r"^\s*(?:job\s*title|position|role|designation)\s*[:\-]\s*(.+)$",
                         text, re.I | re.MULTILINE)
    if labelled:
        return truncate(labelled.group(1).strip(), 120)
    for line in [ln.strip() for ln in text.split("\n") if ln.strip()][:5]:
        if 3 < len(line) <= 90 and not _NOISE_RE.search(line) and not line.endswith(":"):
            return truncate(strip_bullets(line), 120)
    return ""


def _classify_category(text: str) -> str:
    if _EDU_RE.search(text):
        return "education"
    if _CERT_RE.search(text):
        return "certification"
    if _LOCATION_RE.search(text):
        return "location"
    if _INDUSTRY_RE.search(text):
        return "industry"
    if _EXP_RE.search(text):
        return "experience"
    if _SKILL_HINT_RE.search(text):
        return "skill"
    return "skill"


def _classify_mode(text: str, section: str) -> tuple[str, str]:
    """Return ``(mode, rationale)`` for one requirement line.

    Explicit wording in the line beats the section it sits under, so a
    "preferred" bullet inside a Requirements block is still Preferred.
    """
    pref = _PREFERRED_RE.search(text)
    mand = _MANDATORY_RE.search(text)
    if pref and (not mand or pref.start() <= mand.start()):
        return "Preferred", f"JD wording: '{pref.group(0)}'"
    if mand:
        return "Mandatory", f"JD wording: '{mand.group(0)}'"
    if section == "preferred":
        return "Preferred", "Listed under a preferred / nice-to-have section"
    if section == "mandatory":
        return "Mandatory", "Listed under a required / must-have section"
    if section == "responsibilities":
        return "Preferred", "Derived from the responsibilities section, not stated as mandatory"
    # No explicit signal: do not auto-reject anyone over it.
    return "Preferred", "No explicit mandatory wording in the JD"


def _iter_requirement_lines(text: str):
    """Yield ``(line, section)`` for every requirement-shaped line in the JD."""
    section = "general"
    non_empty_seen = 0
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        non_empty_seen += 1
        probe = strip_bullets(line).strip(" :-–—")
        heading_hit = None
        if len(probe) <= 70:
            for name, pattern in _SECTION_HEADINGS.items():
                if pattern.match(probe):
                    heading_hit = name
                    break
        if heading_hit:
            section = heading_hit
            continue
        if section == "about":
            continue
        # The first two lines are the job title and the company/location banner.
        if non_empty_seen <= 2 and section == "general":
            continue
        clean = strip_bullets(line).strip(" -–—\t")
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) < 12 or len(clean) > 320:
            continue
        if _NOISE_RE.search(clean):
            continue
        yield clean, section


def parse_jd_text(text: str, file_name: str = "") -> ParsedJD:
    """Extract structure from raw JD text using deterministic heuristics."""
    # Re-join PDF-wrapped lines first, so one requirement stays one requirement.
    text = dewrap_lines(normalize_text(text))
    jd = ParsedJD(file_name=file_name, text=text, sha=content_hash(text))
    if len(text) < 60:
        jd.status = "failed"
        jd.error = "Job Description is empty or too short to extract requirements from."
        return jd

    jd.title = _guess_title(text)
    jd.min_years = extract_min_years(text)

    seen: set[str] = set()
    index = 0
    for line, section in _iter_requirement_lines(text):
        key = normalize_key(line)
        if not key or key in seen:
            continue
        seen.add(key)

        category = _classify_category(line)
        mode, rationale = _classify_mode(line, section)

        if section == "responsibilities":
            jd.responsibilities.append(line)
            # Only label it a bare responsibility when the JD gave no explicit
            # mandatory/preferred wording of its own.
            if rationale.startswith("Derived from the responsibilities"):
                category = "responsibility"
        elif section == "location":
            category = "location"

        index += 1
        jd.requirements.append(JDRequirement(
            id=f"R{index:03d}", text=line, category=category, mode=mode, rationale=rationale))

    # Roll requirements up into the themed buckets used by scoring.
    for req in jd.requirements:
        if req.category == "education":
            jd.education_requirements.append(req.text)
        elif req.category == "industry":
            jd.industry_requirements.append(req.text)
        elif req.category == "location":
            jd.location_requirements.append(req.text)
        if req.category in {"skill", "certification"}:
            (jd.required_skills if req.mode == "Mandatory" else jd.preferred_skills).append(req.text)

    if not jd.responsibilities:
        jd.responsibilities = [r.text for r in jd.requirements if r.category == "responsibility"]

    if not jd.requirements:
        jd.status = "failed"
        jd.error = "No requirements could be identified in this Job Description."
    return jd


def parse_jd_file(file_name: str, data: bytes) -> ParsedJD:
    """Read a JD from PDF / DOCX / TXT bytes."""
    try:
        raw_text, _pages, _method = extract_text(file_name, data)
    except ResumeParseError as exc:
        return ParsedJD(file_name=file_name, status="failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected JD parse failure for %s", file_name)
        return ParsedJD(file_name=file_name, status="failed",
                        error=f"Unexpected error while reading the JD: {str(exc)[:160]}")
    return parse_jd_text(raw_text, file_name=file_name)


# ---------------------------------------------------------------------------
# Optional LLM refinement
# ---------------------------------------------------------------------------

_JD_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "jd_extraction.txt"


def enrich_with_llm(jd: ParsedJD, client) -> ParsedJD:
    """Refine the heuristic requirement list using the configured LLM.

    Falls back silently to the heuristic result on any failure — JD parsing must
    never block the workflow.
    """
    if not jd.ok or client is None or not getattr(client, "is_llm", False):
        return jd

    try:
        template = _JD_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.warning("JD prompt template missing at %s", _JD_PROMPT_PATH)
        return jd

    prompt = template.replace("{{JD_TEXT}}", truncate(jd.text, 14000))
    try:
        payload = client.complete_json(prompt, purpose="jd_extraction")
    except Exception as exc:  # noqa: BLE001
        logger.warning("JD LLM enrichment failed, keeping heuristic result: %s", exc)
        return jd
    if not payload:
        return jd

    merged: List[JDRequirement] = []
    seen: set[str] = set()
    index = 0

    def add(text_value: str, category: str, mode: str, rationale: str) -> None:
        nonlocal index
        text_value = re.sub(r"\s+", " ", str(text_value or "")).strip()
        if len(text_value) < 4:
            return
        key = normalize_key(text_value)
        if key in seen:
            return
        seen.add(key)
        index += 1
        merged.append(JDRequirement(id=f"R{index:03d}", text=truncate(text_value, 300),
                                    category=category, mode=mode, source="llm", rationale=rationale))

    for item in payload.get("mandatory_requirements") or []:
        if isinstance(item, dict):
            add(item.get("requirement") or item.get("text"), item.get("category") or "skill",
                "Mandatory", item.get("evidence") or "Identified as mandatory in the JD")
        else:
            add(item, _classify_category(str(item)), "Mandatory", "Identified as mandatory in the JD")

    for item in payload.get("preferred_requirements") or []:
        if isinstance(item, dict):
            add(item.get("requirement") or item.get("text"), item.get("category") or "skill",
                "Preferred", item.get("evidence") or "Identified as preferred in the JD")
        else:
            add(item, _classify_category(str(item)), "Preferred", "Identified as preferred in the JD")

    if not merged:
        return jd

    # Keep any heuristic requirement the model did not surface, so nothing in the
    # JD silently disappears.
    for req in jd.requirements:
        key = normalize_key(req.text)
        if key not in seen:
            seen.add(key)
            index += 1
            merged.append(JDRequirement(id=f"R{index:03d}", text=req.text, category=req.category,
                                        mode=req.mode, source=req.source, rationale=req.rationale))

    jd.requirements = merged
    jd.title = str(payload.get("job_title") or jd.title).strip()[:120]

    llm_years = payload.get("minimum_years_experience")
    if isinstance(llm_years, (int, float)) and 0 < float(llm_years) < 50:
        jd.min_years = float(llm_years)

    jd.education_requirements = as_list(payload.get("education_requirements")) or jd.education_requirements
    jd.industry_requirements = as_list(payload.get("industry_requirements")) or jd.industry_requirements
    jd.location_requirements = as_list(payload.get("location_requirements")) or jd.location_requirements
    jd.responsibilities = as_list(payload.get("responsibilities")) or jd.responsibilities

    jd.required_skills = [r.text for r in jd.mandatory() if r.category in {"skill", "certification"}]
    jd.preferred_skills = [r.text for r in jd.preferred() if r.category in {"skill", "certification"}]
    jd.enrichment = "llm"
    return jd

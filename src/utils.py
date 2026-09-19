"""Shared text helpers: normalisation, hashing, name extraction, evidence lookup.

Nothing in here invents information. Every helper either returns something it
found in the source text, or returns ``None`` / an empty result so the caller can
record the field as "Not mentioned in resume".
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from rapidfuzz import fuzz

from .config import NOT_MENTIONED

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"[ \t\x0b\f\r]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_BULLET_RE = re.compile(r"^[\s]*[•●▪◦‣⁃∙*\-–—o]\s+", re.MULTILINE)


def normalize_text(text: str) -> str:
    """Collapse whitespace and unify unicode without dropping content."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(" ", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


def strip_bullets(text: str) -> str:
    return _BULLET_RE.sub("", text)


def normalize_key(text: str) -> str:
    """Lowercase alphanumeric form used for matching and de-duplication."""
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = re.sub(r"[^a-z0-9+#.\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def content_hash(data: bytes | str) -> str:
    """Stable hash used for the extraction cache and duplicate detection."""
    if isinstance(data, str):
        data = data.encode("utf-8", errors="ignore")
    return hashlib.sha256(data).hexdigest()


def text_fingerprint(text: str) -> str:
    """Hash of the *meaningful* text, so the same resume saved twice as PDF and
    DOCX (or re-exported with different metadata) still collides."""
    return content_hash(normalize_key(text)[:20000])


def safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._\- ]+", "_", (name or "file").strip())
    name = re.sub(r"\s+", "_", name)
    return name[:120] or "file"


# ---------------------------------------------------------------------------
# Sentence / line handling — the unit of "evidence"
# ---------------------------------------------------------------------------

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")


def split_evidence_units(text: str) -> List[str]:
    """Split a resume into candidate evidence snippets (bullets and sentences)."""
    units: List[str] = []
    for raw in _SENT_SPLIT_RE.split(text or ""):
        unit = strip_bullets(raw).strip(" -–—\t")
        unit = re.sub(r"\s+", " ", unit).strip()
        if len(unit) < 12:
            continue
        if len(unit) > 400:
            unit = unit[:397].rstrip() + "..."
        units.append(unit)
    # preserve order, drop duplicates
    seen: set[str] = set()
    out: List[str] = []
    for unit in units:
        key = normalize_key(unit)
        if key and key not in seen:
            seen.add(key)
            out.append(unit)
    return out


_BULLET_START_RE = re.compile(r"^\s*(?:[\u2022\u25cf\u25aa\u25e6\u2023\u2043\u2219*\-\u2013\u2014]|\d+[.)])\s+")


def dewrap_lines(text: str) -> str:
    """Re-join lines that a PDF or a fixed-width layout split mid-sentence.

    PDF extraction wraps a long bullet across several lines, which would
    otherwise turn one requirement into several fragments. A line is joined to
    the next when it has no terminal punctuation and the next line reads as a
    continuation rather than a new bullet or heading.
    """
    lines = (text or "").split("\n")
    out: List[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            out.append(line)
            continue
        if not out or not out[-1].strip():
            out.append(line)
            continue
        previous = out[-1].rstrip()
        continues = (
            not re.search(r"[.!?:;]$", previous)
            and not previous.endswith(",")
            and len(previous) > 30
            and not _BULLET_START_RE.match(line)
            and bool(re.match(r"^[a-z(]", line.strip()))
        )
        if continues:
            out[-1] = f"{previous} {line.strip()}"
        else:
            out.append(line)
    return "\n".join(out)


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


# ---------------------------------------------------------------------------
# Equivalence / synonym knowledge
# ---------------------------------------------------------------------------
# Used so a candidate is not penalised purely because the JD and the resume use
# different words for the same thing. Matching still needs contextual evidence —
# these groups only tell the matcher that two phrases *may* be equivalent.

EQUIVALENCE_GROUPS: List[List[str]] = [
    ["performance marketing", "paid marketing", "paid acquisition", "paid media", "growth marketing",
     "demand generation", "user acquisition", "paid ads", "digital acquisition"],
    ["google ads", "adwords", "google adwords", "sem", "search engine marketing", "paid search",
     "google search ads", "pmax", "performance max"],
    ["meta ads", "facebook ads", "fb ads", "instagram ads", "social ads", "paid social"],
    ["b2b saas", "enterprise saas", "saas", "software as a service", "b2b software"],
    ["b2c", "consumer", "d2c", "direct to consumer"],
    ["seo", "search engine optimization", "organic search"],
    ["crm", "customer relationship management", "salesforce", "hubspot"],
    ["ga4", "google analytics", "google analytics 4", "web analytics"],
    ["cro", "conversion rate optimization", "landing page optimization", "a/b testing", "ab testing",
     "experimentation", "split testing"],
    ["roas", "return on ad spend", "cac", "customer acquisition cost", "cpl", "cost per lead",
     "cpa", "cost per acquisition", "unit economics"],
    ["sql", "mysql", "postgresql", "postgres", "relational database", "rdbms"],
    ["python", "py"],
    ["javascript", "js", "ecmascript", "typescript", "ts"],
    ["machine learning", "ml", "deep learning", "predictive modeling", "predictive modelling"],
    ["nlp", "natural language processing", "text mining", "llm", "large language models"],
    ["aws", "amazon web services", "ec2", "s3", "gcp", "google cloud", "azure", "cloud infrastructure"],
    ["kubernetes", "k8s", "container orchestration", "docker", "containerization"],
    ["ci/cd", "cicd", "continuous integration", "continuous deployment", "jenkins", "github actions"],
    ["rest api", "restful api", "api development", "web services", "microservices"],
    ["react", "reactjs", "react.js", "front end", "frontend"],
    ["node", "nodejs", "node.js", "back end", "backend"],
    ["edtech", "education technology", "online learning", "e-learning", "upskilling"],
    ["fintech", "financial services", "banking", "payments", "bfsi", "nbfc"],
    ["ecommerce", "e-commerce", "online retail", "marketplace", "retail tech"],
    ["healthcare", "healthtech", "medtech", "pharma", "life sciences", "clinical"],
    ["team lead", "team leadership", "people management", "managed a team", "led a team",
     "mentored", "line management"],
    ["stakeholder management", "cross functional", "cross-functional", "business partnering"],
    ["agile", "scrum", "kanban", "sprint"],
    ["project management", "program management", "delivery management", "pmo"],
    ["data analysis", "data analytics", "business intelligence", "bi", "reporting", "dashboarding",
     "tableau", "power bi", "looker"],
    ["copywriting", "content writing", "ad copy", "creative writing", "messaging"],
    ["email marketing", "lifecycle marketing", "crm marketing", "drip campaigns", "marketing automation"],
    ["mba", "master of business administration", "pgdm", "post graduate diploma in management"],
    ["b.tech", "btech", "b.e.", "be", "bachelor of engineering", "bachelor of technology",
     "bachelors in engineering", "engineering degree"],
    ["m.tech", "mtech", "m.s.", "ms", "master of technology", "masters in engineering"],
    ["bachelor", "bachelors", "bachelor's", "undergraduate", "ug", "graduate degree", "b.com",
     "bcom", "b.a", "ba", "b.sc", "bsc", "bba", "bca", "b.tech", "btech", "b.e", "any discipline"],
    ["master", "masters", "master's", "postgraduate", "pg", "post graduate", "m.com", "m.a",
     "m.sc", "msc", "mca", "mba", "pgdm"],
    ["enrolment", "enrollment", "enrolments", "signups", "sign ups", "conversions", "admissions"],
    ["budget", "ad spend", "spends", "media spend", "monthly budget"],
    ["mentor", "mentored", "mentoring", "coached", "managed a team", "led a team", "team of"],
    ["reporting", "reported", "dashboards", "weekly reporting", "reports"],
]

_EQUIV_INDEX: Dict[str, int] = {}
for _gid, _group in enumerate(EQUIVALENCE_GROUPS):
    for _phrase in _group:
        _EQUIV_INDEX.setdefault(normalize_key(_phrase), _gid)


def equivalence_group(phrase: str) -> Optional[int]:
    return _EQUIV_INDEX.get(normalize_key(phrase))


def expand_equivalents(phrase: str) -> List[str]:
    """Return the phrase plus any known equivalent wordings."""
    key = normalize_key(phrase)
    gid = _EQUIV_INDEX.get(key)
    if gid is None:
        return [key] if key else []
    return sorted({key, *(normalize_key(p) for p in EQUIVALENCE_GROUPS[gid])})


# ---------------------------------------------------------------------------
# Phrase matching (fuzzy + equivalence aware)
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on",
    "or", "the", "to", "with", "you", "your", "our", "we", "they", "this", "that", "will", "must",
    "have", "has", "had", "should", "can", "able", "experience", "experienced", "working", "work",
    "years", "year", "yrs", "strong", "good", "excellent", "knowledge", "understanding", "skills",
    "ability", "plus", "etc", "including", "across", "using", "use", "role", "candidate",
    # Requirement filler: these words appear in nearly every JD bullet and would
    # otherwise dilute the concepts that actually matter.
    "proven", "expertise", "demonstrated", "hands", "on", "solid", "deep", "track", "record",
    "command", "proficient", "proficiency", "familiar", "familiarity", "exposure", "required",
    "requirement", "mandatory", "essential", "preferred", "preferably", "nice", "bonus",
    "advantage", "desirable", "ideally", "min", "minimum", "least", "over", "more", "than",
    "own", "owns", "owning", "owned", "build", "building", "built", "run", "running", "ran",
    "drive", "driving", "drove", "well", "very", "highly", "great", "new", "new",
}


def content_tokens(text: str) -> List[str]:
    return [t for t in normalize_key(text).split() if t and t not in _STOPWORDS and len(t) > 1]


def phrase_present(phrase: str, haystack_key: str) -> bool:
    """True when any known wording of ``phrase`` appears literally in the text."""
    for variant in expand_equivalents(phrase):
        if not variant:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])", haystack_key):
            return True
    return False


@lru_cache(maxsize=4096)
def _concepts(phrase: str) -> Tuple[Tuple[str, float], ...]:
    """Break a requirement into weighted concepts.

    A "concept" is either a known domain phrase (mapped to its equivalence
    group, so any wording of it counts) or a leftover meaningful token. Domain
    phrases carry far more weight than loose tokens, so a match is driven by
    what the requirement is actually about rather than by its filler words.
    """
    key = normalize_key(phrase)
    if not key:
        return ()

    spans: List[Tuple[int, int, int]] = []  # (start, end, group id)
    for gid, group in enumerate(EQUIVALENCE_GROUPS):
        for member in group:
            member_key = normalize_key(member)
            if not member_key:
                continue
            match = re.search(rf"(?<![a-z0-9]){re.escape(member_key)}(?![a-z0-9])", key)
            if match:
                spans.append((match.start(), match.end(), gid))

    # Longest match wins where phrases overlap.
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    claimed: List[Tuple[int, int]] = []
    groups: List[int] = []
    for start, end, gid in spans:
        if any(start < c_end and end > c_start for c_start, c_end in claimed):
            continue
        claimed.append((start, end))
        if gid not in groups:
            groups.append(gid)

    covered = set()
    for start, end in claimed:
        covered.update(range(start, end))

    leftover = "".join(" " if i in covered else ch for i, ch in enumerate(key))
    tokens = [t for t in leftover.split() if t not in _STOPWORDS and len(t) > 1 and not t.isdigit()]

    concepts: List[Tuple[str, float]] = [(f"g{gid}", 2.0) for gid in groups]
    concepts.extend((f"t{token}", 0.5) for token in dict.fromkeys(tokens))
    return tuple(concepts)


def _concept_satisfied(concept: str, unit_key: str, unit_tokens: set) -> float:
    """How well one concept is evidenced by one snippet (0-1)."""
    if concept.startswith("g"):
        gid = int(concept[1:])
        for member in EQUIVALENCE_GROUPS[gid]:
            member_key = normalize_key(member)
            if member_key and re.search(
                rf"(?<![a-z0-9]){re.escape(member_key)}(?![a-z0-9])", unit_key
            ):
                return 1.0
        return 0.0

    token = concept[1:]
    if token in unit_tokens:
        return 1.0
    # Tolerate inflections ("market"/"marketing", "analyse"/"analysis").
    best = max((fuzz.ratio(token, u) for u in unit_tokens), default=0)
    if best >= 88:
        return 0.75
    if best >= 78:
        return 0.4
    return 0.0


def phrase_similarity(phrase: str, unit: str) -> float:
    """0-1 contextual relevance between a requirement and one evidence snippet.

    This is deliberately not a keyword count. The requirement is decomposed into
    concepts; a concept counts as evidenced when the snippet expresses it in
    *any* known wording. That is what lets "managed paid acquisition for
    enterprise SaaS products" satisfy "experience in B2B SaaS performance
    marketing" without the two sharing a single phrase.
    """
    unit_key = normalize_key(unit)
    if not unit_key:
        return 0.0

    concepts = _concepts(phrase)
    if not concepts:
        return 0.0

    unit_tokens = set(unit_key.split())
    total_weight = sum(weight for _, weight in concepts)
    earned = sum(weight * _concept_satisfied(concept, unit_key, unit_tokens)
                 for concept, weight in concepts)
    score = earned / total_weight if total_weight else 0.0

    # A requirement made up only of generic tokens (no domain concept at all)
    # cannot reach full confidence on token overlap alone.
    if not any(c.startswith("g") for c, _ in concepts):
        score *= 0.85
    return round(min(score, 1.0), 4)


def best_evidence(phrase: str, units: Sequence[str], limit: int = 2) -> List[Tuple[str, float]]:
    """Return the highest-similarity evidence snippets for a requirement."""
    scored = [(u, phrase_similarity(phrase, u)) for u in units]
    scored = [(u, s) for u, s in scored if s >= 0.45]
    scored.sort(key=lambda x: (-x[1], len(x[0])))
    return scored[:limit]


# ---------------------------------------------------------------------------
# Experience parsing
# ---------------------------------------------------------------------------

_YEARS_PATTERNS = [
    re.compile(r"(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:to|-|–|—)\s*(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years|yrs|year)", re.I),
    re.compile(r"(\d{1,2}(?:\.\d)?)\s*\+\s*(?:years|yrs|year)", re.I),
    re.compile(r"(?:minimum|min\.?|at\s+least|over|more\s+than)\s+(\d{1,2}(?:\.\d)?)\s*(?:years|yrs|year)", re.I),
    re.compile(r"(\d{1,2}(?:\.\d)?)\s*(?:years|yrs|year)s?\s+(?:of\s+)?(?:relevant\s+|total\s+|overall\s+|professional\s+|hands[- ]on\s+)?experience", re.I),
]


def extract_min_years(text: str) -> Optional[float]:
    """Smallest 'minimum years of experience' figure stated in the text."""
    if not text:
        return None
    candidates: List[float] = []
    for pattern in _YEARS_PATTERNS:
        for match in pattern.finditer(text):
            try:
                candidates.append(float(match.group(1)))
            except (TypeError, ValueError):
                continue
    return min(candidates) if candidates else None


def extract_total_years(text: str) -> Optional[float]:
    """Years of experience a resume *states about itself* (never inferred)."""
    if not text:
        return None
    explicit = re.search(
        r"(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years|yrs)\b[^.\n]{0,40}?(?:experience|exp\b)", text, re.I
    )
    if explicit:
        try:
            return float(explicit.group(1))
        except ValueError:
            pass
    explicit2 = re.search(r"(?:experience|exp)\s*[:\-]?\s*(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years|yrs)", text, re.I)
    if explicit2:
        try:
            return float(explicit2.group(1))
        except ValueError:
            pass
    return None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_DATE_RANGE_RE = re.compile(
    r"([A-Za-z]{3,9})?\.?\s*(\d{4})\s*(?:-|–|—|to|until|till)\s*(present|current|now|today|([A-Za-z]{3,9})?\.?\s*(\d{4}))",
    re.I,
)


def estimate_years_from_dates(text: str, today: Optional[date] = None) -> Optional[float]:
    """Derive tenure from explicit employment date ranges found in the resume.

    Overlapping ranges are merged so parallel roles are not double counted. This
    reads only dates that are literally present — nothing is assumed.
    """
    if not text:
        return None
    today = today or date.today()
    spans: List[Tuple[int, int]] = []  # months since year 0
    for match in _DATE_RANGE_RE.finditer(text):
        start_month_name, start_year, end_raw, end_month_name, end_year = match.groups()
        try:
            s_year = int(start_year)
        except (TypeError, ValueError):
            continue
        if not (1950 <= s_year <= today.year):
            continue
        s_month = _MONTHS.get((start_month_name or "").lower()[:3], 1)
        if end_raw and end_raw.lower().strip() in {"present", "current", "now", "today"}:
            e_year, e_month = today.year, today.month
        else:
            try:
                e_year = int(end_year)
            except (TypeError, ValueError):
                continue
            e_month = _MONTHS.get((end_month_name or "").lower()[:3], 12)
        if not (1950 <= e_year <= today.year + 1):
            continue
        start = s_year * 12 + s_month
        end = e_year * 12 + e_month
        if end <= start:
            continue
        spans.append((start, end))

    if not spans:
        return None
    spans.sort()
    merged: List[List[int]] = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    months = sum(end - start for start, end in merged)
    return round(months / 12.0, 1)


# ---------------------------------------------------------------------------
# Candidate name extraction
# ---------------------------------------------------------------------------

_NAME_LINE_BLOCKLIST = re.compile(
    r"(resume|curriculum\s*vitae|\bcv\b|profile|summary|objective|contact|phone|email|address|"
    r"linkedin|github|portfolio|experience|education|skills|projects|certification|@|http|www\.|\d{4})",
    re.I,
)
_NAME_LABEL_RE = re.compile(r"^\s*(?:name)\s*[:\-]\s*(.+)$", re.I | re.MULTILINE)
# Words that appear in title-cased prose lines but never in a person's name.
_NAME_WORD_BLOCKLIST = re.compile(
    r"\b(manager|engineer|developer|analyst|scientist|consultant|specialist|director|executive|"
    r"marketing|marketer|professional|lead|senior|junior|intern|officer|architect|designer|"
    r"business|technology|solutions|services|systems|limited|private|university|institute|"
    r"college|company|team|years|experience|india|remote|hybrid)\b", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def extract_candidate_name(text: str, fallback: str) -> Tuple[str, bool]:
    """Best-effort name extraction.

    Returns ``(name, was_extracted)``. Processing must never fail just because a
    name could not be found — the caller passes a fallback such as
    ``"Candidate 001"``.
    """
    if not text:
        return fallback, False

    labelled = _NAME_LABEL_RE.search(text)
    if labelled:
        candidate = _clean_name(labelled.group(1))
        if candidate:
            return candidate, True

    # Look at the first few non-empty lines — resumes almost always lead with
    # the candidate's name.
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()][:8]
    for line in lines:
        if _NAME_LINE_BLOCKLIST.search(line):
            # A section heading means the header block is over — stop guessing.
            if re.match(r"^\s*(profile|summary|objective|experience|education|skills)\b", line, re.I):
                break
            continue
        candidate = _clean_name(line)
        if candidate:
            return candidate, True

    # Fall back to a name-shaped local part of the email address.
    email = _EMAIL_RE.search(text)
    if email:
        local = re.split(r"[._\-0-9]+", email.group(0).split("@")[0])
        parts = [p.capitalize() for p in local if len(p) > 1 and p.isalpha()]
        if len(parts) >= 2:
            return " ".join(parts[:3]), True

    return fallback, False


def _clean_name(raw: str) -> Optional[str]:
    raw = re.sub(r"[•|,]", " ", raw or "")
    raw = re.sub(r"\s+", " ", raw).strip(" -–—:")
    if not raw or len(raw) > 60:
        return None
    words = raw.split()
    if not (1 < len(words) <= 5):
        return None
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z'.\-]*", w) for w in words):
        return None
    # Real names are capitalised (or fully upper-case). A line starting with a
    # lower-case word is prose, not a name.
    if not raw.isupper() and not all(w[0].isupper() for w in words):
        return None
    if _NAME_WORD_BLOCKLIST.search(raw):
        return None
    # ALL-CAPS names are common; title-case them for display.
    if raw.isupper():
        raw = raw.title()
    return raw


# ---------------------------------------------------------------------------
# JSON helpers for LLM output
# ---------------------------------------------------------------------------

def extract_json_object(raw: str) -> Optional[dict]:
    """Pull the first complete JSON object out of a model response.

    Tolerates markdown fences and leading prose, which some providers add even
    when asked for strict JSON.
    """
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start != -1:
        depth, in_string, escape = 0, False, False
        for idx in range(start, len(text)):
            char = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start: idx + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def as_list(value) -> List[str]:
    """Coerce an arbitrary LLM field into a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if isinstance(value, dict):
        value = list(value.values())
    out: List[str] = []
    for item in value if isinstance(value, Iterable) else []:
        if isinstance(item, dict):
            item = " — ".join(str(v) for v in item.values() if v)
        item = re.sub(r"\s+", " ", str(item)).strip()
        if item and item.lower() not in {"none", "n/a", "null", "-"}:
            out.append(item)
    return out


def or_not_mentioned(value: Optional[str]) -> str:
    value = (value or "").strip()
    return value if value else NOT_MENTIONED

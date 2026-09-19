"""Central configuration: scoring weights, category metadata and runtime settings.

Everything that a recruiter can tune lives here so the rest of the code has a
single source of truth. API keys are read from the environment only — never
hardcoded, never persisted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent

# --- Scoring categories -----------------------------------------------------
# key -> (human label, default weight out of 100)
SCORE_CATEGORIES: Dict[str, str] = {
    "relevant_experience": "Relevant Experience",
    "required_skills": "Required Skills",
    "jd_alignment": "JD Alignment",
    "industry_relevance": "Industry / Domain Relevance",
    "education": "Education & Certifications",
    "preferred_skills": "Preferred Requirements",
}

DEFAULT_WEIGHTS: Dict[str, int] = {
    "relevant_experience": 25,
    "required_skills": 25,
    "jd_alignment": 20,
    "industry_relevance": 10,
    "education": 10,
    "preferred_skills": 10,
}

RECOMMENDATIONS: List[str] = [
    "Strong Match",
    "Match",
    "Partial Match",
    "Weak Match",
    "Insufficient Information",
]

INTERVIEW_STATUSES: List[str] = [
    "Not Reviewed",
    "Shortlisted",
    "Interview Scheduled",
    "Interviewed",
    "Rejected",
    "On Hold",
]

REQUIREMENT_MODES: List[str] = ["Mandatory", "Preferred", "Ignore"]

NOT_MENTIONED = "Not mentioned in resume"

# Score bands used as the *starting point* for a recommendation. The final
# recommendation is then adjusted by mandatory-requirement evidence, so it never
# depends on the number alone (see src/scoring.py).
RECOMMENDATION_BANDS = [
    (80, "Strong Match"),
    (65, "Match"),
    (45, "Partial Match"),
    (0, "Weak Match"),
]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass
class Settings:
    """Runtime settings, resolved from environment variables."""

    provider: str = field(default_factory=lambda: (os.getenv("LLM_PROVIDER") or "heuristic").strip().lower())
    model: str = field(default_factory=lambda: (os.getenv("LLM_MODEL") or "").strip())
    anthropic_api_key: str = field(default_factory=lambda: (os.getenv("ANTHROPIC_API_KEY") or "").strip())
    openai_api_key: str = field(default_factory=lambda: (os.getenv("OPENAI_API_KEY") or "").strip())
    anthropic_base_url: str = field(default_factory=lambda: (os.getenv("ANTHROPIC_BASE_URL") or "").strip())
    openai_base_url: str = field(default_factory=lambda: (os.getenv("OPENAI_BASE_URL") or "").strip())

    max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 4096))
    temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.0))
    timeout_seconds: int = field(default_factory=lambda: _env_int("LLM_TIMEOUT_SECONDS", 120))
    max_retries: int = field(default_factory=lambda: _env_int("LLM_MAX_RETRIES", 5))
    concurrency: int = field(default_factory=lambda: _env_int("LLM_CONCURRENCY", 4))
    requests_per_minute: int = field(default_factory=lambda: _env_int("LLM_REQUESTS_PER_MINUTE", 50))

    upload_dir: Path = field(default_factory=lambda: ROOT_DIR / (os.getenv("UPLOAD_DIR") or "uploads"))
    output_dir: Path = field(default_factory=lambda: ROOT_DIR / (os.getenv("OUTPUT_DIR") or "outputs"))
    cache_dir: Path = field(default_factory=lambda: ROOT_DIR / (os.getenv("CACHE_DIR") or ".cache"))

    def default_model(self) -> str:
        if self.model:
            return self.model
        if self.provider == "anthropic":
            return "claude-sonnet-5"
        if self.provider == "openai":
            return "gpt-4o"
        return "heuristic-v1"

    def has_credentials(self) -> bool:
        if self.provider == "anthropic":
            return bool(self.anthropic_api_key)
        if self.provider == "openai":
            return bool(self.openai_api_key)
        return True  # heuristic provider needs nothing

    def ensure_dirs(self) -> None:
        for path in (self.upload_dir, self.output_dir, self.cache_dir):
            path.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    """Build a fresh Settings object (re-reads env so .env edits apply on rerun)."""
    load_dotenv(override=False)
    settings = Settings()
    settings.ensure_dirs()
    return settings


def validate_weights(weights: Dict[str, float]) -> tuple[bool, str]:
    """Weights must cover every category and total exactly 100."""
    missing = [k for k in SCORE_CATEGORIES if k not in weights]
    if missing:
        return False, f"Missing weights for: {', '.join(missing)}"
    if any(float(weights[k]) < 0 for k in SCORE_CATEGORIES):
        return False, "Weights cannot be negative."
    total = round(sum(float(weights[k]) for k in SCORE_CATEGORIES), 4)
    if abs(total - 100.0) > 0.01:
        return False, f"Weights must total 100%. Current total: {total:g}%"
    return True, "Weights total 100%."

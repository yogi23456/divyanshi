"""Local storage: the extraction cache and candidate-data deletion.

Where candidate data lives (all of it inside the project directory, nothing is
sent anywhere except to the configured LLM provider during analysis):

  uploads/   the resume and JD files the recruiter uploaded
  .cache/    extracted resume TEXT, keyed by a hash of the file bytes
  outputs/   generated Excel and DOCX reports

``purge`` deletes any or all of these. The cache exists so re-running against a
new JD, or adding more resumes, does not re-parse or re-pay for work already done.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

from .config import Settings
from .resume_parser import ParsedResume

logger = logging.getLogger(__name__)

CACHE_VERSION = 1


class ExtractionCache:
    """Disk cache of extracted resume text, keyed by file-content hash.

    Stores only what is needed to re-run analysis without re-parsing. Nothing is
    written outside the configured cache directory.
    """

    def __init__(self, settings: Settings):
        self.dir = settings.cache_dir / "resume_text"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, content_sha: str) -> Path:
        return self.dir / f"{content_sha}.json"

    def get(self, content_sha: str) -> Optional[ParsedResume]:
        path = self._path(content_sha)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.debug("Discarding unreadable cache entry %s", path.name)
            return None
        if payload.get("_version") != CACHE_VERSION:
            return None
        payload.pop("_version", None)
        try:
            return ParsedResume(**payload)
        except TypeError:
            # The dataclass changed shape since this entry was written.
            return None

    def put(self, resume: ParsedResume) -> None:
        if not resume.content_sha:
            return
        payload = asdict(resume)
        payload["_version"] = CACHE_VERSION
        try:
            self._path(resume.content_sha).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write cache entry: %s", exc)

    def clear(self) -> int:
        removed = 0
        for path in self.dir.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def stats(self) -> Dict[str, int]:
        files = list(self.dir.glob("*.json"))
        return {"entries": len(files), "bytes": sum(f.stat().st_size for f in files)}


def save_upload(settings: Settings, file_name: str, data: bytes) -> Path:
    """Persist an uploaded file so it can be re-analysed without re-uploading."""
    from .utils import safe_filename

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    path = settings.upload_dir / safe_filename(file_name)
    path.write_bytes(data)
    return path


def directory_stats(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {"files": 0, "bytes": 0, "path": str(path)}
    files = [f for f in path.rglob("*") if f.is_file() and f.name != ".gitkeep"]
    return {"files": len(files), "bytes": sum(f.stat().st_size for f in files), "path": str(path)}


def purge(settings: Settings, uploads: bool = True, cache: bool = True,
          outputs: bool = False) -> Dict[str, int]:
    """Delete stored candidate data. Used by the UI's "Delete candidate data" control."""
    removed = {"uploads": 0, "cache": 0, "outputs": 0}

    targets: List[tuple[str, Path]] = []
    if uploads:
        targets.append(("uploads", settings.upload_dir))
    if outputs:
        targets.append(("outputs", settings.output_dir))

    for key, directory in targets:
        if not directory.exists():
            continue
        for item in directory.iterdir():
            if item.name == ".gitkeep":
                continue
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
                removed[key] += 1
            except OSError as exc:
                logger.warning("Could not delete %s: %s", item, exc)

    if cache:
        removed["cache"] = ExtractionCache(settings).clear()
    return removed

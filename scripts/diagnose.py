"""Environment and file diagnostic.

Run this when a file will not parse and it is not obvious why. It reports which
extraction engines are available and walks one file through the real pipeline.

    python scripts/diagnose.py                          # environment only
    python scripts/diagnose.py path/to/your_file.pdf    # environment + that file
"""

from __future__ import annotations

import io
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def line(label: str, value: object) -> None:
    print(f"  {label:<26} {value}")


def main() -> int:
    print("=" * 70)
    print("DIAGNOSTIC — AI Resume Review & Candidate Ranking")
    print("=" * 70)

    print("\nSystem")
    line("Python", sys.version.split()[0])
    line("Platform", f"{platform.system()} {platform.release()} ({platform.machine()})")
    line("Running from", ROOT)

    print("\nExtraction engines")
    engines: dict[str, str] = {}
    try:
        import pymupdf
        engines["pymupdf"] = getattr(pymupdf, "__version__", "?")
    except ImportError:
        try:
            import fitz as pymupdf
            engines["pymupdf (as fitz)"] = getattr(pymupdf, "__version__", "?")
        except ImportError:
            engines["pymupdf"] = "NOT INSTALLED"
    for name, module in [("pdfplumber", "pdfplumber"), ("python-docx", "docx"),
                         ("openpyxl", "openpyxl"), ("pandas", "pandas"),
                         ("streamlit", "streamlit"), ("rapidfuzz", "rapidfuzz")]:
        try:
            mod = __import__(module)
            engines[name] = getattr(mod, "__version__", "installed")
        except Exception as exc:  # noqa: BLE001
            engines[name] = f"NOT USABLE ({type(exc).__name__})"
    for name, version in engines.items():
        line(name, version)

    print("\nOCR (optional — only needed for scanned PDFs)")
    try:
        import pytesseract
        from PIL import Image  # noqa: F401
        try:
            line("tesseract binary", pytesseract.get_tesseract_version())
        except Exception:  # noqa: BLE001
            line("tesseract binary", "NOT INSTALLED (scanned PDFs will be flagged)")
    except ImportError:
        line("pytesseract / Pillow", "not installed (scanned PDFs will be flagged)")

    print("\nLLM provider")
    from src.config import get_settings
    from src.llm_client import build_client
    settings = get_settings()
    client = build_client(settings)
    line("LLM_PROVIDER", settings.provider)
    line("Engine in use", client.describe())

    if len(sys.argv) < 2:
        print("\nNo file given. To test one:")
        print("  python scripts/diagnose.py samples/job_description.pdf")
        return 0

    target = Path(sys.argv[1]).expanduser()
    print(f"\nFile: {target}")
    if not target.exists():
        print("  FILE NOT FOUND — check the path.")
        return 1

    data = target.read_bytes()
    line("size", f"{len(data):,} bytes")
    line("suffix", target.suffix.lower() or "(none)")

    if target.suffix.lower() == ".pdf":
        print("\n  Raw text layer, per engine:")
        try:
            try:
                import pymupdf
            except ImportError:
                import fitz as pymupdf
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                text = "".join(p.get_text("text") for p in doc)
            line("  pymupdf", f"{len(text.strip()):,} chars over {len(text.splitlines())} lines")
        except Exception as exc:  # noqa: BLE001
            line("  pymupdf", f"FAILED: {exc}")
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                text2 = "".join((p.extract_text() or "") for p in pdf.pages)
            line("  pdfplumber", f"{len(text2.strip()):,} chars")
        except Exception as exc:  # noqa: BLE001
            line("  pdfplumber", f"FAILED: {exc}")
        print("  (0 chars from BOTH means it is genuinely a scan and needs OCR)")

    print("\n  Through the real pipeline:")
    from src.jd_parser import parse_jd_file
    from src.resume_parser import parse_resume

    parsed = parse_resume(target.name, data, "Candidate 001")
    line("  as a RESUME", f"{parsed.status} via {parsed.extraction_method or '-'}; "
                          f"{parsed.char_count:,} chars")
    if parsed.error:
        line("    reason", parsed.error)
    if parsed.ok:
        line("    name", parsed.candidate_name)
        line("    skills found", len(parsed.skills))

    jd = parse_jd_file(target.name, data)
    line("  as a JD", f"{jd.status}; {len(jd.requirements)} requirement(s)")
    if jd.error:
        line("    reason", jd.error)
    if jd.ok:
        line("    title", jd.title or "(not detected)")
        line("    mandatory", len(jd.mandatory()))
        line("    preferred", len(jd.preferred()))

    print("\nPaste this whole output back if you need help.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

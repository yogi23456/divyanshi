# AI Resume Review & Candidate Ranking

An internal recruiter tool. Upload one Job Description and a batch of resumes,
and get a ranked candidate list where **every score traces back to evidence** in
the JD and the resume.

Built for the case where a recruiter has to justify a decision: for any
candidate you can see which requirement was matched, the exact resume sentence
that matched it, how relevant that evidence was judged to be, and what that
contributed to the score.

---

## Contents

1. [What it does](#what-it-does)
2. [Installation](#installation)
3. [Environment variables](#environment-variables)
4. [Starting the application](#starting-the-application)
5. [Using the app](#using-the-app)
6. [How scoring works](#how-scoring-works)
7. [Configuring scoring weights](#configuring-scoring-weights)
8. [Exporting to Excel](#exporting-to-excel)
9. [Exporting to DOCX](#exporting-to-docx)
10. [Changing the LLM provider](#changing-the-llm-provider)
11. [Where candidate data is stored](#where-candidate-data-is-stored)
12. [Deleting candidate data](#deleting-candidate-data)
13. [Bias and fairness](#bias-and-fairness)
14. [Error handling](#error-handling)
15. [Performance](#performance)
16. [Project structure](#project-structure)
17. [Testing](#testing)

---

## What it does

- **Reads a JD** (PDF / DOCX / TXT) and separates **mandatory** from
  **preferred** requirements, showing the JD wording behind each classification.
  You can override any requirement to Mandatory, Preferred or Ignore.
- **Bulk-processes resumes** (PDF / DOCX / TXT), extracting name, experience,
  skills, education, certifications, job titles, companies and responsibilities.
  Detects duplicates, scanned PDFs, corrupted and password-protected files.
- **Analyses every candidate** against the JD using contextual matching, not
  keyword counting. "Managed paid acquisition for enterprise SaaS products" is
  recognised as evidence for "experience in B2B SaaS performance marketing".
- **Scores out of 100** across six configurable categories, with a written
  reason and a resume quote behind each one.
- **Ranks every processed candidate** — low scorers are never hidden.
- **Search and filter** semantically: searching "Google Ads" also returns
  candidates whose resumes say "AdWords" or "paid search".
- **Records recruiter decisions** (notes, shortlist, interview status, final
  decision) and carries them into both exports.
- **Exports** a three-sheet Excel workbook and a written DOCX evaluation report.

---

## Quick start

Requires **Python 3.10+**. From the project folder:

```bash
# macOS / Linux
./run.sh
```
```bat
REM Windows
run.bat
```

That creates a virtual environment on first run, installs everything, generates
the sample data, and opens the app at **<http://localhost:8501>**. Press Ctrl+C
to stop it. Subsequent runs reuse the environment and start in seconds.

The app works immediately with no API key — it runs on the built-in offline
analyzer. Add a key to `.env` later to switch to an LLM (see
[Changing the LLM provider](#changing-the-llm-provider)).

To try it straight away, upload `samples/job_description.pdf` in step 1 and
everything in `samples/resumes/` in step 2.

---

## Installation

Prefer to do it by hand, or already have an environment? Requires **Python 3.10+**.

```bash
git clone <your-repo-url>
cd divyanshi

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### Optional: OCR for scanned resumes

Image-based PDFs need Tesseract in addition to the Python packages:

```bash
# Debian / Ubuntu
sudo apt-get install tesseract-ocr
# macOS
brew install tesseract
# Windows: https://github.com/UB-Mannheim/tesseract/wiki
```

Without it, scanned PDFs are flagged **"OCR required"** rather than skipped
silently or guessed at. Nothing is ever invented for a resume that could not be
read.

Both paths are covered by the test suite: `samples/resumes/scanned_resume.pdf`
is a real image-only PDF (asserted to have no text layer), and the suite checks
that OCR recovers the name, experience, skills, education and employers when
Tesseract is present, and that the file is flagged with empty text when it is not.

---

## Environment variables

Copy the template and fill in what you need:

```bash
cp .env.example .env
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `openai`, or `heuristic` (offline) |
| `LLM_MODEL` | provider default | Model id, e.g. `claude-sonnet-5`, `gpt-4o` |
| `ANTHROPIC_API_KEY` | – | Required when `LLM_PROVIDER=anthropic` |
| `OPENAI_API_KEY` | – | Required when `LLM_PROVIDER=openai` |
| `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` | – | Optional custom/proxy endpoint |
| `LLM_MAX_TOKENS` | `4096` | Response budget per analysis |
| `LLM_TEMPERATURE` | `0` | Keep at 0 for reproducible scoring |
| `LLM_TIMEOUT_SECONDS` | `120` | Per-request timeout |
| `LLM_MAX_RETRIES` | `5` | Retries with exponential backoff |
| `LLM_CONCURRENCY` | `4` | Resumes analysed in parallel |
| `LLM_REQUESTS_PER_MINUTE` | `50` | Client-side rate limit |
| `UPLOAD_DIR` / `OUTPUT_DIR` / `CACHE_DIR` | `uploads` / `outputs` / `.cache` | Storage locations |

**API keys are only ever read from the environment.** They are never hardcoded,
never written to disk by the app, and `.env` is gitignored.

### Running without an API key

Set `LLM_PROVIDER=heuristic` (or leave the key blank) and the app runs on its
built-in **evidence engine** — a deterministic, fully offline analyzer that
walks the same requirement → evidence → relevance → score chain. It makes no
network calls, which makes it useful for testing, for demos and for handling
resumes you would rather not send to a third party. The LLM path gives better
judgement on nuanced wording; the evidence engine is the reliable floor and is
also what catches an LLM response that arrives malformed.

---

## Starting the application

```bash
streamlit run app.py
```

Opens at <http://localhost:8501>.

To try it immediately with the bundled sample data:

```bash
python scripts/generate_samples.py   # creates samples/ (JD + 8 resumes)
streamlit run app.py
```

Then upload `samples/job_description.pdf` and everything in `samples/resumes/`.

---

## Using the app

The interface follows the workflow in five tabs.

### 1 · Job Description
Upload a PDF/DOCX/TXT JD, or paste the text. The app extracts the role, the
minimum years of experience, and every requirement — classified as **Mandatory**
or **Preferred** with the JD wording that justified it ("JD wording: 'must
have'").

A requirement with no explicit mandatory wording is classified **Preferred**, on
purpose: candidates should not be rejected against a requirement the JD never
actually established as mandatory.

Use the **requirement controls** table to change any requirement to Mandatory,
Preferred or Ignore, then re-run the analysis.

### 2 · Resumes
Upload resumes in bulk. You get counts for total uploaded, successfully
processed, failed, and duplicates, plus a per-file table showing the extraction
method, page count, detected name, and the reason for any failure.

Tick **Add to the existing batch** to append more resumes to a batch you have
already processed.

### 3 · Analyse
Confirm the weights, then run. A progress bar reports each resume as it
completes. A failure on one resume never discards results already finished, and
failed resumes can be retried on their own.

### 4 · Results
Dashboard metrics (Total, Strong Match, Match, Partial Match, Weak Match,
Insufficient Information), then the ranked table — sortable on any column and
filterable by score range, recommendation, minimum experience, required skill,
mandatory-requirement status, industry, education, candidate name, interview
status and shortlist state.

Open any candidate for the full evaluation: score breakdown with per-category
reasoning and resume quotes, why they match, missing or weak areas, the full
per-requirement verdict table, resume evidence, missing information, and the
recruiter decision form.

### 5 · Export
Generate and download the Excel workbook and the DOCX report.

---

## How scoring works

The engine never counts keywords. For every requirement it walks the same chain:

```
JD requirement  →  candidate evidence  →  relevance  →  score
```

**1. The requirement is broken into concepts.** A concept is either a known
domain phrase or a meaningful leftover token. Filler words that appear in nearly
every JD bullet ("proven", "strong command of", "hands-on") are discarded so
they cannot dilute the match.

**2. Each concept is matched against the resume by meaning.** A concept counts
as evidenced when the resume expresses it in *any* known wording. This is what
lets "managed paid acquisition for enterprise SaaS products" satisfy "experience
in B2B SaaS performance marketing" — the two share no phrase, but "paid
acquisition" ≡ "performance marketing" and "enterprise SaaS" ≡ "B2B SaaS".

**3. Evidence is weighted by strength.** A skill demonstrated in a described
responsibility scores higher than the same skill sitting in a skills list with
nothing behind it — a bare list mention is capped at 60%, and the reasoning says
so. Keyword stuffing does not pay.

**4. Category scores are weighted and totalled.**

| Category | Default weight |
| --- | --- |
| Relevant Experience | 25 |
| Required Skills | 25 |
| JD Alignment | 20 |
| Industry / Domain Relevance | 10 |
| Education & Certifications | 10 |
| Preferred Requirements | 10 |
| **Total** | **100** |

**5. The recommendation is not the score.** The weighted score sets a starting
band, and mandatory-requirement evidence then moves it:

| Situation | Effect |
| --- | --- |
| All mandatory requirements met + score ≥ 72 | Upgraded one band |
| 75–99% of mandatory requirements met | Capped at **Match** |
| 50–74% met | Capped at **Partial Match** |
| Under 50% met | Forced to **Weak Match** |
| Resume too sparse to judge | **Insufficient Information** |

So a candidate scoring 87 who misses a critical mandatory requirement is *not* a
Strong Match, and the stated reason says exactly why.

### Things the engine deliberately does not do

- **It does not penalise what the JD did not ask for.** No stated minimum
  experience means duration does not affect the score. No stated education
  requirement means a candidate is not marked down for it. Location is scored
  **only** when the JD explicitly states a location requirement.
- **It does not invent anything.** A field the resume does not contain is
  reported as `Not mentioned in resume` and listed under Missing Information.
  Missing information is reported as a *gap to verify*, never as a negative
  conclusion about the candidate.
- **It does not auto-reject.** Every processed candidate is ranked and visible.

---

## Configuring scoring weights

Open **Scoring weights** in the sidebar and adjust the six categories. The app
validates live that they total 100% and refuses to run an analysis until they
do. **Reset to defaults** restores the table above.

Weights apply to the next analysis run, and the weights used are recorded on the
Summary sheet of the Excel export and in the DOCX report, so a saved report
always says how it was scored.

To change the defaults permanently, edit `DEFAULT_WEIGHTS` in `src/config.py`.

---

## Exporting to Excel

**5 · Export → Generate Excel.** Written to `outputs/` and downloadable from the
browser.

| Sheet | Contents |
| --- | --- |
| **Candidate Ranking** | One row per candidate: rank, name, resume, overall score, recommendation, every category score, mandatory requirements missed, strengths, concerns, missing information, location, notice period, and the recruiter's own columns. |
| **Detailed Evaluation** | One row per candidate per category: score, max score, percentage, the reasoning, and the resume evidence — plus a mandatory-requirements roll-up per candidate. |
| **Summary** | JD details, upload/processed/failed/duplicate counts, recommendation breakdown, average / highest / lowest score, the weights used, and the top candidates. |

Formatting is built for scanning a long list: frozen header row, autofilter on
every column, auto-sized widths, wrapped text, a green→amber→red colour scale on
scores, colour-coded recommendations, and highlighting on any unmet mandatory
requirement. Unprocessed files appear greyed out with their reason rather than
being dropped.

---

## Exporting to DOCX

**5 · Export → Generate DOCX.** A written evaluation report containing:

- Cover page — role, batch statistics, recommendation breakdown, weights used
- Overall ranking table
- A section per candidate: score breakdown with per-category reasoning,
  relevant experience, key strengths, key concerns, mandatory requirements met
  and missed, missing information, resume evidence quotes, and a recruiter notes
  block
- A closing table of every file that could not be evaluated, with the reason

For large batches, set **Detailed sections to include** to limit the
per-candidate write-ups to the top N (the ranking table still covers everyone).

---

## Changing the LLM provider

The provider sits behind one interface (`src/llm_client.py`), so switching is a
config change:

```bash
# .env
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o
OPENAI_API_KEY=sk-...
```

Supported out of the box: `anthropic`, `openai` (including any
OpenAI-compatible endpoint via `OPENAI_BASE_URL`), and `heuristic` (offline).

**Adding a new provider** takes two steps in `src/llm_client.py`:

```python
class MyProvider(_Provider):
    name = "myprovider"

    def __init__(self, settings):
        super().__init__(settings)
        self._client = ...                      # build your SDK client

    def complete(self, prompt: str, system: str = "") -> str:
        return ...                              # return the raw text response
```

then register it in `_build_provider`. Rate limiting, retries with exponential
backoff, JSON extraction and the malformed-response retry are handled by
`LLMClient` for every provider.

The prompts live in `prompts/` as plain text and can be edited without touching
Python:

| File | Purpose |
| --- | --- |
| `prompts/jd_extraction.txt` | Extract and classify JD requirements |
| `prompts/resume_analysis.txt` | Evaluate one candidate against the JD |
| `prompts/scoring_prompt.txt` | Scoring rubric injected into the analysis prompt |

---

## Where candidate data is stored

Everything stays inside the project directory on the machine running the app.
There is no database and no cloud storage.

| Location | Contents | Written when |
| --- | --- | --- |
| `uploads/` | The original JD and resume files | On upload, if "Save uploaded files" is ticked |
| `.cache/resume_text/` | Extracted resume **text**, keyed by a hash of the file bytes | On every resume parse |
| `outputs/` | Generated Excel and DOCX reports | When you export |

`uploads/`, `outputs/` and `.cache/` are all gitignored, so candidate data
cannot be committed by accident.

**What leaves the machine:** nothing, unless you configure an LLM provider. When
one is configured, the JD text and each resume's text are sent to that provider's
API during analysis, and nothing else. With `LLM_PROVIDER=heuristic` there are no
outbound calls at all.

Recruiter notes, shortlist flags and interview statuses live in the Streamlit
session and are written only into the files you export. Closing the browser
session clears them, so export before you finish.

The application logs processing status and error reasons. It does not log resume
contents or candidate personal details.

---

## Deleting candidate data

**From the app:** sidebar → **Data & privacy** → **Delete all candidate data**.
This deletes everything in `uploads/` and the whole extraction cache, and
optionally the generated reports in `outputs/` (tick the box first). The panel
shows the current file counts before you delete.

**From the command line:**

```bash
python -c "from src.config import get_settings; from src.storage import purge; \
           print(purge(get_settings(), uploads=True, cache=True, outputs=True))"
```

**Manually:** delete the `uploads/`, `.cache/` and `outputs/` directories.

To avoid storing resumes at all, untick **Save uploaded files to `uploads/`** in
step 2. Resumes are then held in memory for the session only. (The text cache
still fills, to avoid re-parsing; clear it from the sidebar, or set `CACHE_DIR`
to a temporary path.)

---

## Bias and fairness

Candidates are scored only on job-relevant qualifications and evidence. The
analysis prompt instructs the model explicitly, and the offline engine has no
mechanism to do otherwise:

> Ignore gender, religion, caste, race, ethnicity, nationality, age, disability,
> marital status, sexual orientation, political affiliation, photographs and
> appearance. Never infer any of these from a name, a college, a hometown, or
> any other proxy.

The scoring categories contain no field for any protected characteristic, and
the end-to-end test asserts that no protected term appears in any scoring
rationale or in the exported evidence.

This reduces one source of bias; it does not make the tool a substitute for
human judgement. Treat every output as a ranked shortlist for a recruiter to
review, not as a decision.

---

## Error handling

Every failure is reported against the specific file, with a reason, and never
takes down a batch:

| Case | Behaviour |
| --- | --- |
| Corrupted PDF | Flagged `failed` with the underlying reason |
| Password-protected PDF | Flagged `failed`, identified as password-protected |
| Scanned / image-based PDF | OCR attempted; if unavailable or unsuccessful, flagged `ocr_required` — never guessed |
| Empty or near-empty resume | Flagged `failed` — "no usable text could be extracted" |
| Unsupported format | Rejected with the list of supported formats |
| Missing candidate name | Falls back to `Candidate 001`; **processing always continues** |
| Duplicate resume | Flagged, linked to the original, not scored twice (detected across formats via a text fingerprint) |
| Line-wrapped bullets (PDF/OCR) | Re-joined before parsing, so evidence quotes are never cut off mid-sentence |
| LLM API failure | Retried with exponential backoff, then falls back to the offline engine, with a note on the evaluation |
| API rate limit | Client-side token-bucket limiter plus retry-with-backoff |
| Malformed LLM JSON | One stricter retry, then the offline engine |
| One resume crashing | Isolated to that candidate — completed results are kept |

Failed resumes can be retried on their own from step 3.

---

## Performance

Designed for batches of roughly 100–500 resumes.

- **Extraction cache** — resume text is cached by content hash, so re-running
  against a new JD, or adding resumes to a batch, re-parses nothing.
- **Concurrency** — LLM analysis runs in a thread pool (`LLM_CONCURRENCY`).
- **Rate limiting** — a token bucket keeps you under `LLM_REQUESTS_PER_MINUTE`.
- **Progress** — per-resume progress for both extraction and analysis.
- **Isolation** — one failure never loses completed results.

Measured on the offline engine (300 synthetic resumes, 4-core machine):

| Stage | Time |
| --- | --- |
| Parse 300 resumes | 0.4 s |
| Analyse 300 candidates | 11.3 s |
| Semantic search across 300 | 0.6 s |
| Excel export (300 rows) | 1.7 s |
| DOCX export (top 25 detailed) | 2.1 s |

With an LLM provider, analysis time is dominated by the API: roughly
`resumes ÷ concurrency × per-call latency`, bounded by your rate limit.

---

## Project structure

```
app.py                        Streamlit recruiter dashboard
run.sh / run.bat              One-command start (venv + install + launch)
requirements.txt
.env.example                  Configuration template
.gitignore                    Excludes .env, uploads/, outputs/, .cache/

src/
  config.py                   Weights, categories, settings, weight validation
  utils.py                    Normalisation, concept matching, name/date extraction
  resume_parser.py            PDF/DOCX/TXT extraction, OCR detection, field extraction
  jd_parser.py                JD requirement extraction and mandatory/preferred split
  llm_client.py               Provider abstraction, rate limiting, retries
  candidate_analyzer.py       LLM path + offline evidence engine, batch orchestration
  scoring.py                  Category scores, weighting, recommendation derivation
  ranking.py                  Ranking, semantic search, filters, summary
  export_excel.py             Three-sheet workbook
  export_docx.py              Evaluation report
  storage.py                  Extraction cache and data deletion

prompts/
  jd_extraction.txt           JD requirement extraction prompt
  resume_analysis.txt         Candidate evaluation prompt (strict JSON)
  scoring_prompt.txt          Scoring rubric

scripts/
  generate_samples.py         Build the sample JD and resumes
  e2e_test.py                 Full pipeline test
  diagnose.py                 Environment + single-file troubleshooting

samples/                      Sample JD + 8 resumes (incl. deliberate edge cases)
uploads/  outputs/  .cache/   Runtime data (gitignored)
```

---

## Troubleshooting

If a file will not parse and the reason is not obvious, run the diagnostic. It
reports which extraction engines are available and walks one file through the
real pipeline:

```bash
python scripts/diagnose.py                       # environment only
python scripts/diagnose.py path/to/your_file.pdf # environment + that file
```

Common cases:

| Symptom | Cause and fix |
| --- | --- |
| "looks like a scanned/image-based PDF" on a PDF whose text you *can* select | Both extraction engines found no text layer. Some PDF producers write text in a way neither engine reads. Re-save or re-export the file as a PDF (opening and re-printing to PDF usually fixes it), or install Tesseract to OCR it. |
| Same message on a genuine scan | Expected. Install Tesseract (see [Installation](#installation)) to read it. |
| "Corrupted or unreadable PDF" | The file is damaged or not really a PDF. Re-download or re-export it. |
| Streamlit asks for an email on first run | Its one-time welcome prompt. Press Enter with the field blank. `run.sh` / `run.bat` suppress it for you. |

## Testing

```bash
python scripts/generate_samples.py   # build the sample data
python scripts/e2e_test.py           # run the full pipeline
```

The suite runs 118 checks across configuration, JD parsing, resume parsing,
caching, analysis, scoring correctness, evidence traceability, weight
reconfiguration, OCR, search, filtering, ranking, bias, and both export formats.
It exits non-zero on any failure.

Notable assertions:

- Category scores sum to the overall score and respect their configured maximums
- Every met requirement is backed by evidence, and **every evidence quote is
  verified to exist in the resume text** — nothing is generated
- A corrupted PDF, an empty file and a near-empty resume are each flagged rather
  than crashing the batch
- A genuinely scanned PDF (image only, verified to have no text layer) is read
  via OCR and scored like any other candidate; with Tesseract absent the same
  file is flagged `ocr_required` with empty text, proving nothing is invented
- A resume with no extractable name still processes, as `Candidate NNN`
- A duplicate is detected across *different file formats*
- Re-weighting changes both the category maximums and the overall score
- No protected characteristic appears in any scoring rationale or exported evidence

Sample outputs are committed at `outputs/sample_candidate_ranking.xlsx` and
`outputs/sample_candidate_report.docx`.

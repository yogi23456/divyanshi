#!/usr/bin/env bash
# One-command start for macOS / Linux.
#   ./run.sh
# Creates a virtual environment on first run, installs dependencies, generates
# the sample data, then opens the app at http://localhost:8501
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python 3 was not found. Install it from https://www.python.org/downloads/"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "==> Creating virtual environment (first run only)..."
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing dependencies..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

if [ ! -f samples/job_description.pdf ]; then
  echo "==> Generating sample data..."
  python scripts/generate_samples.py
fi

if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo "==> Created .env — the app runs offline until you add an API key."
fi

echo
echo "==> Starting the app at http://localhost:8501  (press Ctrl+C to stop)"
echo
exec streamlit run app.py

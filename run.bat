@echo off
REM One-command start for Windows.
REM   run.bat
REM Creates a virtual environment on first run, installs dependencies,
REM generates the sample data, then opens the app at http://localhost:8501
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found. Install it from https://www.python.org/downloads/
  exit /b 1
)

if not exist .venv (
  echo ==^> Creating virtual environment ^(first run only^)...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

echo ==^> Installing dependencies...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

if not exist samples\job_description.pdf (
  echo ==^> Generating sample data...
  python scripts\generate_samples.py
)

if not exist .env if exist .env.example (
  copy /y .env.example .env >nul
  echo ==^> Created .env - the app runs offline until you add an API key.
)

echo.
echo ==^> Starting the app at http://localhost:8501  ^(press Ctrl+C to stop^)
echo.
streamlit run app.py

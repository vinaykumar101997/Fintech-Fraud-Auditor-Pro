# setup_windows.ps1 - build the local environment and run the offline pipeline.
#
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_windows.ps1
#
# Touches nothing on AWS. The offline pipeline needs no credentials.

$ErrorActionPreference = "Continue"

function Section($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Ok($text)      { Write-Host "  OK      $text" -ForegroundColor Green }
function Bad($text)     { Write-Host "  MISSING $text" -ForegroundColor Red }

Section "1. Project structure"
$required = @("config.py", "app.py", "requirements.txt", "utils", "scripts", "tests")
$missing = @()
foreach ($item in $required) {
    if (Test-Path $item) { Ok $item } else { Bad $item; $missing += $item }
}
if ($missing.Count -gt 0) {
    Write-Host "`nSTOP. Files are missing: $($missing -join ', ')" -ForegroundColor Red
    Write-Host "Extract the zip again, keeping its folder structure. Run this" -ForegroundColor Red
    Write-Host "script from the folder that contains config.py." -ForegroundColor Red
    exit 1
}

Section "2. Choose an interpreter"
# Python 3.14 is too new: several dependencies have no 3.14 wheels yet.
$chosen = $null
foreach ($version in @("3.12", "3.13", "3.11")) {
    & py -$version --version *> $null
    if ($LASTEXITCODE -eq 0) { $chosen = $version; break }
}
if (-not $chosen) {
    Write-Host "  No Python 3.11, 3.12 or 3.13 found." -ForegroundColor Red
    Write-Host "  Installed versions:" -ForegroundColor Yellow
    py -0
    Write-Host "`n  Install Python 3.12 from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "  Tick 'Add python.exe to PATH', then re-run this script." -ForegroundColor Red
    exit 1
}
Ok "Python $chosen"

Section "3. Rebuild the virtual environment"
if (Test-Path .venv) {
    Write-Host "  Removing the old .venv..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force .venv -ErrorAction SilentlyContinue
}
& py -$chosen -m venv .venv
$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "  venv creation failed." -ForegroundColor Red
    exit 1
}
& $py --version

Section "4. Install dependencies"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nInstall failed. Copy the conflict message above and report it." -ForegroundColor Red
    exit 1
}
Ok "dependencies installed"

Section "5. Generate the sample ledger"
& $py scripts/generate_sample_ledger.py

Section "6. Tests (offline, no AWS)"
& $py -m pytest tests/ -q
$testsPassed = ($LASTEXITCODE -eq 0)

Section "7. Funnel evaluation"
& $py scripts/evaluate.py

Section "Summary"
if ($testsPassed) {
    Write-Host "  Tests passed. Recall above should read 100%." -ForegroundColor Green
} else {
    Write-Host "  Tests FAILED. Paste the pytest output for diagnosis." -ForegroundColor Red
}
Write-Host ""
Write-Host "  Activate the environment with:  .\.venv\Scripts\Activate.ps1"
Write-Host "  Launch the app with:            streamlit run app.py"
Write-Host "  (the app needs Bedrock and Qdrant; the steps above do not)"
Write-Host ""

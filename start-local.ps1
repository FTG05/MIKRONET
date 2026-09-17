# Runs the merged MikroNet + Lending + Bibi Payment app on this PC.
# Double-click this file (or right-click -> Run with PowerShell).
#
# First time only: it creates a venv and installs Flask/waitress into it,
# so you don't need anything installed globally except Python 3.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path "$root\venv")) {
    Write-Host "First run: creating venv and installing requirements..." -ForegroundColor Cyan
    python -m venv venv
    & "$root\venv\Scripts\pip.exe" install -r requirements.txt
}

$port = (Get-Content "$root\config.json" -Raw | ConvertFrom-Json).port
Write-Host "Starting on http://127.0.0.1:$port  (Ctrl+C to stop)" -ForegroundColor Green
& "$root\venv\Scripts\python.exe" "$root\app.py"

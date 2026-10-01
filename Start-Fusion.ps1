$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    uv sync --python 3.12 --locked
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
}
& '.venv/Scripts/python.exe' 'run.py'

# Set up the governed-stack demo: venv, the three MCP servers, mcpo, sample data.
# Re-runnable. Edit the three source paths below if your checkouts live elsewhere.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$sqlSteward = "C:\Projects\sql-steward"
$kqlSop     = "C:\Users\pawan\work\kql-sop"
$docSteward = "C:\Users\pawan\work\doc-steward"

foreach ($p in @($sqlSteward, $kqlSop, $docSteward)) {
    if (-not (Test-Path $p)) { throw "Source repo not found: $p (edit setup.ps1)" }
}

$python = "C:\Users\pawan\AppData\Local\Programs\Python\Python311\python.exe"
if (-not (Test-Path $python)) { $python = "py" }  # fall back to the launcher

Write-Host "Creating venv..." -ForegroundColor Cyan
& $python -m venv "$root\.venv"
$venvPy = "$root\.venv\Scripts\python.exe"

# This network sits behind an SSL-inspecting proxy; trust the PyPI hosts.
$trusted = @("--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org")

Write-Host "Installing the three servers + mcpo..." -ForegroundColor Cyan
& $venvPy -m pip install --quiet --upgrade pip @trusted
& $venvPy -m pip install --quiet @trusted -e $sqlSteward -e $kqlSop -e $docSteward mcpo

Write-Host "Seeding the sample database..." -ForegroundColor Cyan
& $venvPy "$root\data\sql\build_demo_db.py"

Write-Host "Rendering mcpo.config.json..." -ForegroundColor Cyan
& $venvPy "$root\scripts\render_mcpo_config.py"

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "  Verify governance:  .\.venv\Scripts\python.exe verify.py"
Write-Host "  Start the gateway:  .\.venv\Scripts\mcpo.exe --config mcpo.config.json --port 8765"
Write-Host "  Start Open WebUI:   docker compose up -d   (then http://localhost:3000)"

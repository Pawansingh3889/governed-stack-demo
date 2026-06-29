# Set up the governed-stack demo: venv, the three MCP servers, mcpo, sample data.
# Re-runnable. Edit the three source paths below if your checkouts live elsewhere.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$sqlSteward  = "C:\Projects\sql-steward"
$kqlSop      = "C:\Users\pawan\work\kql-sop"
$docSteward  = "C:\Users\pawan\work\doc-steward"
$schemaScout = "C:\Projects\schema-scout"

foreach ($p in @($sqlSteward, $kqlSop, $docSteward, $schemaScout)) {
    if (-not (Test-Path $p)) { throw "Source repo not found: $p (edit setup.ps1)" }
}

$python = "C:\Users\pawan\AppData\Local\Programs\Python\Python311\python.exe"
if (-not (Test-Path $python)) { $python = "py" }  # fall back to the launcher

Write-Host "Creating venv..." -ForegroundColor Cyan
& $python -m venv "$root\.venv"
$venvPy = "$root\.venv\Scripts\python.exe"

# This network sits behind an SSL-inspecting proxy; trust the PyPI hosts.
$trusted = @("--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org")

Write-Host "Installing the governed servers + mcpo..." -ForegroundColor Cyan
& $venvPy -m pip install --quiet --upgrade pip @trusted
& $venvPy -m pip install --quiet @trusted -e $sqlSteward -e $kqlSop -e $docSteward -e $schemaScout "mcp>=1.0" mcpo

Write-Host ""
Write-Host "Setup complete. The venv is ready; stack.py drives everything from here." -ForegroundColor Green
Write-Host "  Start the gateway:  .\.venv\Scripts\python.exe stack.py up"
Write-Host "  Check / verify:     .\.venv\Scripts\python.exe stack.py status   (and: verify)"
Write-Host "  Full chat UI:       .\.venv\Scripts\python.exe stack.py up --webui   (no Docker)"
Write-Host "  Stop:               .\.venv\Scripts\python.exe stack.py down"
Write-Host ""
Write-Host "To point a tool at real infra, copy stack.env.example to stack.env and edit it."

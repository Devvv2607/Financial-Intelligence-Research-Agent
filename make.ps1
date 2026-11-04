<#
.SYNOPSIS
    PowerShell twin of the Makefile, for Windows without GNU make.
.EXAMPLE
    .\make.ps1 check
    .\make.ps1 up
#>
param([Parameter(Position = 0)][string]$Target = "help")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Run a native command and stop on a non-zero exit code (PowerShell doesn't by default).
function Invoke-Step {
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @())
    Write-Host "> $Exe $($Arguments -join ' ')" -ForegroundColor DarkGray
    # Resolve to the real executable (npm -> npm.cmd, not the npm.ps1 shim, which
    # would misread splatted "--flags" as PowerShell parameters).
    $path = (Get-Command $Exe -CommandType Application | Select-Object -First 1).Source
    & $path @Arguments
    if ($LASTEXITCODE -ne 0) { throw "'$Exe $($Arguments -join ' ')' failed with exit code $LASTEXITCODE" }
}

$targets = [ordered]@{
    "install"   = @{ Help = "Install Python (uv) and frontend (npm) dependencies"; Run = {
            Invoke-Step uv @("sync")
            Invoke-Step npm @("--prefix", "frontend", "ci") } }
    "lint"      = @{ Help = "Ruff lint + format check"; Run = {
            Invoke-Step uv @("run", "ruff", "check", ".")
            Invoke-Step uv @("run", "ruff", "format", "--check", ".") } }
    "format"    = @{ Help = "Auto-fix lint and format"; Run = {
            Invoke-Step uv @("run", "ruff", "check", ".", "--fix")
            Invoke-Step uv @("run", "ruff", "format", ".") } }
    "typecheck" = @{ Help = "mypy (strict)"; Run = { Invoke-Step uv @("run", "mypy") } }
    "test"      = @{ Help = "Unit + agent tests (no live services)"; Run = { Invoke-Step uv @("run", "pytest") } }
    "web-check" = @{ Help = "Frontend lint + typecheck"; Run = {
            Invoke-Step npm @("--prefix", "frontend", "run", "lint")
            Invoke-Step npm @("--prefix", "frontend", "run", "typecheck") } }
    "check"     = @{ Help = "Everything CI runs, minus the frontend build"; Run = {
            foreach ($t in "lint", "typecheck", "test", "web-check") { & $targets[$t].Run } } }
    "up"        = @{ Help = "Build and start the full stack"; Run = { Invoke-Step docker @("compose", "up", "-d", "--build") } }
    "down"      = @{ Help = "Stop the stack (keeps data volumes)"; Run = { Invoke-Step docker @("compose", "down") } }
    "logs"      = @{ Help = "Tail stack logs"; Run = { Invoke-Step docker @("compose", "logs", "-f", "--tail=100") } }
    "ready"     = @{ Help = "Query the API readiness probe"; Run = {
            # curl.exe, not the PowerShell `curl` alias; a 503 is a valid answer here.
            & curl.exe -s -w "`nHTTP %{http_code}`n" http://localhost:8000/ready } }
    "dev-api"   = @{ Help = "Run the API on the host with reload"; Run = {
            Invoke-Step uv @("run", "uvicorn", "api.main:app", "--reload", "--port", "8000") } }
    "dev-web"   = @{ Help = "Run the frontend dev server"; Run = { Invoke-Step npm @("--prefix", "frontend", "run", "dev") } }
    "clean"     = @{ Help = "Remove caches and build output"; Run = {
            foreach ($p in ".mypy_cache", ".ruff_cache", ".pytest_cache", "frontend/.next") {
                if (Test-Path $p) { Remove-Item -Recurse -Force $p }
            } } }
}

if ($Target -eq "help" -or -not $targets.Contains($Target)) {
    if ($Target -ne "help") { Write-Host "Unknown target '$Target'." -ForegroundColor Red }
    Write-Host "Usage: .\make.ps1 <target>`n"
    foreach ($k in $targets.Keys) { "  {0,-12} {1}" -f $k, $targets[$k].Help }
    if ($Target -ne "help") { exit 1 }
    exit 0
}

& $targets[$Target].Run

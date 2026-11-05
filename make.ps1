<#
.SYNOPSIS
    PowerShell twin of the Makefile, for Windows without GNU make.
.EXAMPLE
    .\make.ps1 check
    .\make.ps1 up
    .\make.ps1 backup
    .\make.ps1 restore data\backups\fira-20261105-150000.dump -Database fira_restore_test
#>
param(
    [Parameter(Position = 0)][string]$Target = "help",
    [Parameter(Position = 1)][string]$File,  # restore: the .dump file
    [string]$Database                         # restore: target database (default: the live one)
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Run a native command and stop on a non-zero exit code (PowerShell doesn't by default).
function Invoke-Step {
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @())
    Write-Host "> $Exe $($Arguments -join ' ')" -ForegroundColor DarkGray
    # Resolve to the real executable (npm -> npm.cmd, not the npm.ps1 shim, which
    # would misread splatted "--flags" as PowerShell parameters).
    $path = (Get-Command $Exe -CommandType Application | Select-Object -First 1).Source
    # Judge native tools by exit code only. Under "Stop", Windows PowerShell 5.1 turns any
    # redirected stderr line (e.g. docker's progress output) into a terminating error.
    $ErrorActionPreference = "Continue"
    & $path @Arguments
    if ($LASTEXITCODE -ne 0) { throw "'$Exe $($Arguments -join ' ')' failed with exit code $LASTEXITCODE" }
}

# Run a shell command inside the postgres container (its env has POSTGRES_USER/_DB).
function Invoke-Container {
    param([Parameter(Mandatory)][string]$Command, [switch]$Capture)
    $args_ = @("compose", "exec", "-T", "postgres", "sh", "-c", $Command)
    if (-not $Capture) { Invoke-Step docker $args_; return }
    $ErrorActionPreference = "Continue"
    $out = (& docker @args_) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "'$Command' failed in the postgres container" }
    return $out.Trim()
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
    "backup"    = @{ Help = "pg_dump the whole database to data/backups/<db>-<timestamp>.dump"; Run = {
            # Dump inside the container, then copy the file out: piping pg_dump's binary
            # output through PowerShell 5.1 would re-encode and corrupt it.
            New-Item -ItemType Directory -Force "data/backups" | Out-Null
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $db = (Invoke-Container 'printf %s $POSTGRES_DB' -Capture)
            $out = "data/backups/$db-$stamp.dump"
            Invoke-Container 'pg_dump -U $POSTGRES_USER -d $POSTGRES_DB -Fc -f /tmp/fira-backup.dump'
            $tables = (Invoke-Container 'pg_restore --list /tmp/fira-backup.dump | grep -c TABLE.DATA' -Capture)
            Invoke-Step docker @("compose", "cp", "postgres:/tmp/fira-backup.dump", $out)
            Invoke-Container 'rm -f /tmp/fira-backup.dump'
            $size = "{0:N1} MB" -f ((Get-Item $out).Length / 1MB)
            Write-Host "Backup written: $out ($size, $tables tables with data)" -ForegroundColor Green } }
    "restore"   = @{ Help = "Restore a backup: restore <file> [-Database name] (default: the live DB, asks first)"; Run = {
            if (-not $File -or -not (Test-Path $File)) { throw "Usage: .\make.ps1 restore <file.dump> [-Database name]" }
            $live = (Invoke-Container 'printf %s $POSTGRES_DB' -Capture)
            $target = if ($Database) { $Database } else { $live }
            if ($target -notmatch '^[a-z_][a-z0-9_]*$') { throw "Database name must be lowercase letters, digits and _" }
            if ($target -eq $live) {
                # Replaces the live data: never without an explicit, typed confirmation.
                Write-Host "This REPLACES every table in the live database '$live' with $File." -ForegroundColor Yellow
                Write-Host "Stop the api first (docker compose stop api). Take a backup first if unsure."
                $answer = Read-Host "Type the database name to confirm"
                if ($answer -ne $live) { Write-Host "Cancelled."; return }
                $clean = "--clean --if-exists"
            } else {
                $exists = Invoke-Container "psql -U `$POSTGRES_USER -d postgres -tAc 'SELECT 1 FROM pg_database WHERE datname=`$`$$target`$`$'" -Capture
                if ($exists -eq "1") { throw "Database '$target' already exists; pick a new name (restoring into a scratch DB never overwrites)." }
                Invoke-Container "createdb -U `$POSTGRES_USER $target"
                $clean = ""
            }
            Invoke-Step docker @("compose", "cp", $File, "postgres:/tmp/fira-restore.dump")
            Invoke-Container "pg_restore -U `$POSTGRES_USER -d $target $clean --no-owner --exit-on-error /tmp/fira-restore.dump"
            Invoke-Container 'rm -f /tmp/fira-restore.dump'
            Write-Host "Restored $File into '$target'." -ForegroundColor Green } }
    "contracts" = @{ Help = "Export API contracts (contracts/*.json) and regenerate frontend types"; Run = {
            Invoke-Step uv @("run", "python", "-m", "api.contracts")
            Invoke-Step npm @("--prefix", "frontend", "run", "gen:types") } }
    "contracts-check" = @{ Help = "Fail if contracts or generated frontend types are stale (CI)"; Run = {
            Invoke-Step uv @("run", "python", "-m", "api.contracts", "--check")
            Invoke-Step npm @("--prefix", "frontend", "run", "gen:types")
            Invoke-Step git @("diff", "--exit-code", "--", "frontend/lib/generated") } }
    "migrate"   = @{ Help = "Apply database migrations"; Run = {
            Invoke-Step uv @("run", "alembic", "-c", "backend/alembic.ini", "upgrade", "head") } }
    "ingest-sample" = @{ Help = "Ingest the 10 agreed sample companies (+ headings-only experiment on 3)"; Run = {
            & $targets["migrate"].Run
            Invoke-Step uv @("run", "python", "-m", "ingestion", "run", "--sample", "10", "--experiment-tickers", "AAPL,INTC,XOM") } }
    "ingest-full" = @{ Help = "Ingest the whole universe (resumable)"; Run = {
            & $targets["migrate"].Run
            Invoke-Step uv @("run", "python", "-m", "ingestion", "run") } }
    "ingest-daily" = @{ Help = "Resume the full run; stops cleanly at the daily token/news budgets"; Run = {
            & $targets["migrate"].Run
            Invoke-Step uv @("run", "python", "-m", "ingestion", "run", "--daily") } }
    "ingest-progress" = @{ Help = "One line: companies done/remaining, tokens used today"; Run = {
            Invoke-Step uv @("run", "python", "-m", "ingestion", "progress") } }
    "review-export" = @{ Help = "Write the 30-row risk-tag review sheet (data/review/)"; Run = {
            Invoke-Step uv @("run", "python", "-m", "ingestion", "review", "export") } }
    "review-score" = @{ Help = "Score the model against your labels in the review sheet"; Run = {
            Invoke-Step uv @("run", "python", "-m", "ingestion", "review", "score") } }
    "ingest-report" = @{ Help = "Write the ingestion report for the sample"; Run = {
            Invoke-Step uv @("run", "python", "-m", "ingestion", "report", "--sample", "10") } }
    "tools-demo" = @{ Help = "Call every agent tool once against the local stores (agent model only)"; Run = {
            Invoke-Step uv @("run", "python", "-m", "agent.tools", "demo") } }
    "dev-api"   = @{ Help = "Run the API on the host with reload"; Run = {
            Invoke-Step uv @("run", "uvicorn", "api.main:app", "--reload", "--port", "8000", "--loop", "core.aio:selector_loop") } }
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

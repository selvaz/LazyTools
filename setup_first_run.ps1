# ============================================================================
# setup_first_run.ps1 - interactive first-run bootstrap for LazyTools' MCP
# server and connectors (search_cached/web_*, regime_*, telegram_*, ...).
#
# Run from PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\setup_first_run.ps1
#
# Idempotent: safe to re-run after pulling the repo on a new machine, or
# after rotating a key -- existing env vars are offered as defaults, not
# silently overwritten.
# ============================================================================
param(
    [string]$Python = "C:\ProgramData\spyder-6\python.exe",
    [string]$RegimeDbPath,
    [string]$NewsDbPath,
    [string]$AttachmentsDir,
    [switch]$SkipInstall,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Read-OptionalSecret($Prompt, $ExistingLabel) {
    $current = [Environment]::GetEnvironmentVariable($ExistingLabel, "User")
    if ($current) {
        $answer = Read-Host "$Prompt already set. Press Enter to keep it, or paste a new value"
    } else {
        $answer = Read-Host "$Prompt (press Enter to skip)"
    }
    if ($answer) {
        [Environment]::SetEnvironmentVariable($ExistingLabel, $answer, "User")
        Set-Item -Path "Env:$ExistingLabel" -Value $answer
        Write-Host "Set $ExistingLabel for current user."
    } elseif ($current) {
        Set-Item -Path "Env:$ExistingLabel" -Value $current
        Write-Host "Keeping existing $ExistingLabel."
    } else {
        Write-Host "Skipping $ExistingLabel."
    }
}

Write-Host ""
Write-Host "LazyTools first-run setup"
Write-Host "Repo: $Root"
Write-Host ""

if (!(Test-Path $Python)) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) {
        Write-Warning "Configured Python not found: $Python"
        $Python = $found.Source
        Write-Host "Using Python on PATH: $Python"
    } else {
        throw "Python not found. Install Python 3.11+ or pass -Python C:\path\python.exe"
    }
}

# --- Depot / cache / attachment paths ---------------------------------------
# These are each individually resolved by the owning package if left unset
# (LazyStats' resolve_depot_path, LazyCrawler's resolve_news_db_path) -- this
# script just persists a sensible per-machine default up front, the same way
# market-data-hub's own setup_first_run.ps1 persists MARKET_DATA_DB, so a
# fresh machine ends up fully wired without anyone hand-setting env vars.
if (!$RegimeDbPath) {
    $RegimeDbPath = Join-Path $env:USERPROFILE ".lazytools\regime_depot.db"
}
New-Item -ItemType Directory -Force -Path (Split-Path $RegimeDbPath -Parent) | Out-Null
[Environment]::SetEnvironmentVariable("LAZYTOOLS_REGIME_DB", $RegimeDbPath, "User")
Set-Item -Path Env:LAZYTOOLS_REGIME_DB -Value $RegimeDbPath
Write-Host "LAZYTOOLS_REGIME_DB=$RegimeDbPath"

if (!$NewsDbPath) {
    $crawlerSibling = Join-Path (Split-Path $Root -Parent) "LazyCrawler"
    if (Test-Path $crawlerSibling) {
        $NewsDbPath = Join-Path $crawlerSibling "news.db"
    }
}
if ($NewsDbPath) {
    [Environment]::SetEnvironmentVariable("LAZYCRAWLER_NEWS_DB", $NewsDbPath, "User")
    Set-Item -Path Env:LAZYCRAWLER_NEWS_DB -Value $NewsDbPath
    Write-Host "LAZYCRAWLER_NEWS_DB=$NewsDbPath"
} else {
    Write-Warning "No sibling LazyCrawler checkout found and -NewsDbPath not given -- search_cached/web_* will have no shared page cache (falls back to an empty, in-memory one) until LAZYCRAWLER_NEWS_DB is set. Re-run with -NewsDbPath if LazyCrawler lives somewhere else."
}

if (!$AttachmentsDir) {
    $AttachmentsDir = Join-Path $env:USERPROFILE ".lazytools\reports"
}
New-Item -ItemType Directory -Force -Path $AttachmentsDir | Out-Null
[Environment]::SetEnvironmentVariable("LAZYTOOLS_ATTACHMENTS_DIR", $AttachmentsDir, "User")
Set-Item -Path Env:LAZYTOOLS_ATTACHMENTS_DIR -Value $AttachmentsDir
Write-Host "LAZYTOOLS_ATTACHMENTS_DIR=$AttachmentsDir (telegram_send_document is confined to this directory)"

Read-OptionalSecret "DeepSeek API key (default model for the optimizer/report/stats specialist agents)" "DEEPSEEK_API_KEY"
Read-OptionalSecret "Telegram bot token" "TELEGRAM_BOT_TOKEN"
Read-OptionalSecret "Telegram chat id / @channel" "TELEGRAM_CHAT_ID"

# --- Ecosystem DB registry (lazytools.registry) -----------------------------
# These are each individually resolved by their owning repo if left unset
# (registry_status()/artifact_* fall back to "not configured", not an error),
# but the datahub/statistical MCP providers need MARKET_DATA_DB, and the
# artifact catalog is otherwise invisible to anyone running just this script.
Read-OptionalSecret "market-data-hub DB (needed by the datahub/statistical MCP providers)" "MARKET_DATA_DB"
Read-OptionalSecret "market-data-hub artifact catalog DB" "MARKET_DATA_ARTIFACTS_DB"
Read-OptionalSecret "LazyPulse artifact catalog DB" "PULSE_ARTIFACTS_DB"
Read-OptionalSecret "LazyCrawler artifact catalog DB" "CRAWLER_ARTIFACTS_DB"
Read-OptionalSecret "LazyPortfolio artifact catalog DB" "LAZYPORTFOLIO_ARTIFACTS_DB"

if (!$SkipInstall) {
    Write-Host ""
    Write-Host "Installing/updating LazyTools + extras (web, telegram, mcp, dev)..."
    & $Python -m ensurepip --upgrade
    & $Python -m pip install -e ".[web,telegram,mcp,test]"
}

Write-Host ""
Write-Host "Verifying MCP server configuration..."
& $Python -c @"
import os
try:
    from lazystats.regimes import resolve_depot_path
    print('regime depot ->', resolve_depot_path())
except ImportError:
    print('regime depot ->', '(lazystats not installed -- it is a separate sibling repo, not a LazyTools extra; regime tools stay unavailable until it is installed)')
from lazycrawler.config import resolve_news_db_path
print('news cache   ->', resolve_news_db_path() or '(unset -- falls back to :memory:)')
print('DEEPSEEK_API_KEY present:', bool(os.environ.get('DEEPSEEK_API_KEY')))
print('TELEGRAM_BOT_TOKEN present:', bool(os.environ.get('TELEGRAM_BOT_TOKEN')))
"@

if (!$SkipTests) {
    Write-Host ""
    Write-Host "Running test suite..."
    & $Python -m pytest -q
}

Write-Host ""
Write-Host "First-run setup complete."
Write-Host "Open a new PowerShell session to inherit saved user environment variables."
Write-Host "Start the MCP server with: python -m lazytools.mcp_server --allow-unsafe"
Write-Host "Pass --config <path.json> (or set LAZYTOOLS_MCP_CONFIG) to override several data_source paths from one file instead of per-env-var."

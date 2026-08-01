# Initialize the shared LazyTools operations catalog on Windows.
# Usage: powershell -ExecutionPolicy Bypass -File .\setup_operations.ps1
param(
    [string]$Python = "C:\ProgramData\spyder-6\python.exe",
    [string]$DataRoot = "",
    [switch]$SkipInstall
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
if (!(Test-Path $Python)) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (!$found) { throw "Python 3.11+ not found. Pass -Python C:\path\python.exe" }
    $Python = $found.Source
}
if (!$DataRoot) { $DataRoot = Join-Path $env:USERPROFILE ".lazytools" }
# A relative -DataRoot resolves fine right here (cwd is $Root, set above),
# but $db/$artifacts get persisted as permanent User environment variables
# below -- a later scheduled process has its own cwd and would resolve the
# same relative string to a different location. Resolve once, now.
$DataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$db = Join-Path $DataRoot "operations.sqlite"
$artifacts = Join-Path $DataRoot "artifacts"
New-Item -ItemType Directory -Force -Path $DataRoot, $artifacts | Out-Null
[Environment]::SetEnvironmentVariable("LAZYTOOLS_OPERATIONS_DB", $db, "User")
[Environment]::SetEnvironmentVariable("LAZYTOOLS_ARTIFACTS_DIR", $artifacts, "User")
Set-Item -Path Env:LAZYTOOLS_OPERATIONS_DB -Value $db
Set-Item -Path Env:LAZYTOOLS_ARTIFACTS_DIR -Value $artifacts
if (!$SkipInstall) {
    & $Python -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw "LazyTools installation failed." }
}
& $Python -m lazytools.operations init --db $db --artifacts $artifacts
if ($LASTEXITCODE -ne 0) { throw "Operations catalog initialization failed." }
Write-Host "Operations catalog ready. Open a new PowerShell session to inherit the saved environment."

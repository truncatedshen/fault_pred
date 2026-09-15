<#
    Install the Fault Prediction Platform on a machine that only has this bundle.

    .\deploy.ps1                                   # install into %LOCALAPPDATA% and verify
    .\deploy.ps1 -InstallDir D:\tools\fault        # choose the install directory
    .\deploy.ps1 -SkipVerify                       # install only

    Steps: locate the wheel, create a virtual environment, install the package with the
    MCP bridge (and Parquet support), install the Agent skill into the Codex skills
    directory, register the MCP server in Codex config.toml, then run the acceptance test.
#>
param(
    [string]$BundleDir = (Split-Path -Parent $PSScriptRoot),
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "fault-prediction-platform"),
    [string]$CodexHome = (Join-Path $env:USERPROFILE ".codex"),
    [int]$Port = 8765,
    [switch]$SkipSkill,
    [switch]$SkipMcp,
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"
function Write-Step($message) { Write-Host "`n== $message" -ForegroundColor Cyan }
function Write-Ok($message) { Write-Host "   $message" -ForegroundColor Green }

Write-Step "Checking Python"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "python was not found on PATH; install Python 3.11+ first" }
$version = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$version -lt [version]"3.11") { throw "Python 3.11+ is required, found $version" }
Write-Ok "python $version at $python"

Write-Step "Locating the wheel"
$wheel = Get-ChildItem -Path (Join-Path $BundleDir "dist") -Filter *.whl -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $wheel) {
    $wheel = Get-ChildItem -Path $BundleDir -Filter *.whl -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
}
if (-not $wheel) { throw "no wheel found under $BundleDir; pass -BundleDir <extracted bundle>" }
Write-Ok $wheel.FullName

Write-Step "Creating the virtual environment in $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $python -m venv (Join-Path $InstallDir ".venv")
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed" }
}
Write-Ok $venvPython

Write-Step "Installing the package (with the mcp and parquet extras)"
$spec = "$($wheel.FullName)[mcp,parquet]"
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install $spec --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Write-Ok "installed"

Write-Step "Copying the helper scripts"
$scriptTarget = Join-Path $InstallDir "scripts"
New-Item -ItemType Directory -Force -Path $scriptTarget | Out-Null
foreach ($name in @("verify_deploy.py", "mcp_smoke.py", "install_mcp_config.py")) {
    $source = Join-Path (Join-Path $BundleDir "scripts") $name
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $scriptTarget -Force }
}
Write-Ok $scriptTarget

if (-not $SkipSkill) {
    Write-Step "Installing the Agent skill into $CodexHome\skills"
    $skillSource = Join-Path (Join-Path $BundleDir "skills") "fault-prediction"
    if (-not (Test-Path -LiteralPath (Join-Path $skillSource "SKILL.md"))) {
        throw "skill not found in the bundle: $skillSource"
    }
    $skillTarget = Join-Path (Join-Path $CodexHome "skills") "fault-prediction"
    if (Test-Path -LiteralPath $skillTarget) { Remove-Item -LiteralPath $skillTarget -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $skillTarget | Out-Null
    Copy-Item -Path (Join-Path $skillSource "*") -Destination $skillTarget -Recurse -Force
    Write-Ok $skillTarget
}

if (-not $SkipMcp) {
    Write-Step "Registering the MCP server in $CodexHome\config.toml"
    & $venvPython (Join-Path $scriptTarget "install_mcp_config.py") `
        --config (Join-Path $CodexHome "config.toml") --python $venvPython --url "http://127.0.0.1:$Port"
    if ($LASTEXITCODE -ne 0) { throw "MCP configuration failed" }
}

Write-Step "Writing the start script"
$startScript = Join-Path $InstallDir "start.ps1"
@"
param([int]`$Port = $Port, [string]`$DataRoot = "$InstallDir\data")
`$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path `$DataRoot | Out-Null
Write-Host "Fault Studio: http://127.0.0.1:`$Port  (Ctrl+C stops)"
& "$venvPython" -m fault_platform serve --port `$Port --data-root `$DataRoot --artifact-cache-mb 512
"@ | Set-Content -LiteralPath $startScript -Encoding UTF8
Write-Ok $startScript

if (-not $SkipVerify) {
    Write-Step "Running the acceptance test"
    & $venvPython (Join-Path $scriptTarget "verify_deploy.py") --from-config `
        --config (Join-Path $CodexHome "config.toml") `
        --skill-dir (Join-Path $CodexHome "skills\fault-prediction") `
        --work-dir (Join-Path $InstallDir "verify")
    if ($LASTEXITCODE -ne 0) { throw "acceptance test failed; see the output above" }
}

Write-Host "`nDeployment finished." -ForegroundColor Green
Write-Host "  1. Start the service : $startScript"
Write-Host "  2. Open              : http://127.0.0.1:$Port"
Write-Host "  3. In Codex          : restart the session so the MCP server and skill are loaded"
Write-Host "  4. Re-verify anytime : $venvPython $scriptTarget\verify_deploy.py --from-config"

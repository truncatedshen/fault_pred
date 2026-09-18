<#
    Install the Fault Prediction Platform on a machine that only has this bundle.

    .\deploy.ps1                                     # install for Codex into %LOCALAPPDATA% and verify
    .\deploy.ps1 -Client opencode                    # install for OpenCode instead
    .\deploy.ps1 -Client both                        # register the MCP server + skill for both
    .\deploy.ps1 -InstallDir D:\tools\fault          # choose the install directory
    .\deploy.ps1 -SkipVerify                         # install only

    Steps: locate the wheel, create a virtual environment, install the package with the
    MCP bridge (and Parquet support), install the Agent skill into the client's skills
    directory, register the MCP server in the client's config, then run the acceptance test.
#>
param(
    [string]$BundleDir = (Split-Path -Parent $PSScriptRoot),
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "fault-prediction-platform"),
    [ValidateSet("codex", "opencode", "both")][string]$Client = "codex",
    [string]$CodexHome = (Join-Path $env:USERPROFILE ".codex"),
    [string]$OpencodeHome = (Join-Path $env:USERPROFILE ".config\opencode"),
    [int]$Port = 8765,
    [switch]$SkipSkill,
    [switch]$SkipMcp,
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"
function Write-Step($message) { Write-Host "`n== $message" -ForegroundColor Cyan }
function Write-Ok($message) { Write-Host "   $message" -ForegroundColor Green }
# 客户端 → (配置文件, skill 目录)。OpenCode 的路径来自官方文档。
function Get-ClientPaths($name) {
    if ($name -eq "codex") {
        return @{
            Config = (Join-Path $CodexHome "config.toml")
            Skill  = (Join-Path $CodexHome "skills\fault-prediction")
        }
    }
    return @{
        Config = (Join-Path $OpencodeHome "opencode.json")
        Skill  = (Join-Path $OpencodeHome "skills\fault-prediction")
    }
}
$clients = if ($Client -eq "both") { @("codex", "opencode") } else { @($Client) }

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
foreach ($name in @("verify_deploy.py", "mcp_smoke.py", "install_mcp_config.py", "install_skill.py")) {
    $source = Join-Path (Join-Path $BundleDir "scripts") $name
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $scriptTarget -Force }
}
Write-Ok $scriptTarget

if (-not $SkipSkill) {
    $skillSource = Join-Path (Join-Path $BundleDir "skills") "fault-prediction"
    if (-not (Test-Path -LiteralPath (Join-Path $skillSource "SKILL.md"))) {
        throw "skill not found in the bundle: $skillSource"
    }
    foreach ($name in $clients) {
        $paths = Get-ClientPaths $name
        Write-Step "Installing the Agent skill for $name into $($paths.Skill)"
        & $venvPython (Join-Path $scriptTarget "install_skill.py") `
            --client $name --source $skillSource --target $paths.Skill
        if ($LASTEXITCODE -ne 0) { throw "skill installation failed for $name" }
    }
}

if (-not $SkipMcp) {
    foreach ($name in $clients) {
        $paths = Get-ClientPaths $name
        Write-Step "Registering the MCP server for $name in $($paths.Config)"
        & $venvPython (Join-Path $scriptTarget "install_mcp_config.py") `
            --client $name --config $paths.Config --python $venvPython --url "http://127.0.0.1:$Port"
        if ($LASTEXITCODE -ne 0) { throw "MCP configuration failed for $name" }
    }
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
    foreach ($name in $clients) {
        $paths = Get-ClientPaths $name
        Write-Step "Running the acceptance test for $name"
        & $venvPython (Join-Path $scriptTarget "verify_deploy.py") --from-config --client $name `
            --config $paths.Config --skill-dir $paths.Skill `
            --work-dir (Join-Path $InstallDir "verify-$name")
        if ($LASTEXITCODE -ne 0) { throw "acceptance test failed for $name; see the output above" }
    }
}

Write-Host "`nDeployment finished." -ForegroundColor Green
Write-Host "  1. Start the service : $startScript"
Write-Host "  2. Open              : http://127.0.0.1:$Port"
Write-Host "  3. In $Client : restart the session so the MCP server and skill are loaded"
Write-Host "  4. Re-verify anytime : $venvPython $scriptTarget\verify_deploy.py --from-config --client $Client"
Write-Host "  5. Other clients     : rerun with -Client opencode / -Client both"

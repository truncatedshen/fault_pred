param([int]$Port = 8765, [string]$DataRoot = "examples/data")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw "Virtual environment missing. Run .\setup.ps1 first."
}
Write-Host "Fault Studio: http://127.0.0.1:$Port"
Write-Host "Press Ctrl+C to stop."
& $taskPython -m fault_platform serve --port $Port --data-root $DataRoot
exit $LASTEXITCODE

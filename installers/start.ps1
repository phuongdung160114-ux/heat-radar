param([switch]$NoBrowser,[switch]$NoAuto,[string]$DataDir,[int]$Port=8787)
$ErrorActionPreference='Stop'
$SourceRoot=Split-Path $PSScriptRoot -Parent
$HomeDir=Join-Path $env:LOCALAPPDATA 'AshareHeatRadar'
$LocalPython=Join-Path $SourceRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $LocalPython) {
    $Python=$LocalPython
    $App=Join-Path $SourceRoot 'run.py'
    if (-not $DataDir) { $DataDir=Join-Path $SourceRoot 'data' }
} else {
    $Python=Join-Path $HomeDir 'venv\Scripts\python.exe'
    $App=Join-Path $HomeDir 'app\run.py'
    if (-not $DataDir) { $DataDir=Join-Path $HomeDir 'data' }
}
if (-not (Test-Path $Python) -or -not (Test-Path $App)) {
    & (Join-Path $PSScriptRoot 'install.ps1')
    exit $LASTEXITCODE
}
$env:PYTHONUTF8='1';$env:PYTHONIOENCODING='utf-8'
$env:PYTHONDONTWRITEBYTECODE='1'
Set-Location -LiteralPath (Split-Path $App -Parent)
$RunArguments=@($App,'--data-dir',$DataDir,'--port',[string]$Port)
if ($NoBrowser) { $RunArguments+='--no-browser' }
if ($NoAuto) { $RunArguments+='--no-auto' }
& $Python @RunArguments
exit $LASTEXITCODE

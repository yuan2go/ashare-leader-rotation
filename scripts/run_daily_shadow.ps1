[CmdletBinding()]
param(
    [ValidateSet('Collect','Ingest','DailyAll','VerifyOffline','Status','Healthcheck')]
    [string]$Action = 'DailyAll',
    [string]$Date,
    [string]$Python,
    [string]$ExportPhase3dV1
)

$ErrorActionPreference = 'Stop'
$Repository = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $Python = Join-Path $Repository 'data/fetch_cache/runtimes/batch1-py311/Scripts/python.exe'
} else {
    $Python = (Get-Command $Python -ErrorAction Stop).Source
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python 3.11 was not found at $Python"
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$Collector = Join-Path $PSScriptRoot 'collect_forward_session.py'
$Runner = Join-Path $PSScriptRoot 'run_phase5a_multi_shadow.py'
$CollectArgs = @()
if ($Date) { $CollectArgs += @('--date', $Date) }
if ($ExportPhase3dV1) { $CollectArgs += @('--export-phase3d-v1', $ExportPhase3dV1) }

Push-Location -LiteralPath $Repository
try {
    if ($Action -in 'Collect','DailyAll') {
        & $Python -B -X utf8 $Collector @CollectArgs
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    switch ($Action) {
        'Ingest'        { & $Python -B -X utf8 $Runner --ingest-ready }
        'DailyAll'      { & $Python -B -X utf8 $Runner --ingest-ready }
        'VerifyOffline' { & $Python -B -X utf8 $Runner --verify-offline }
        'Status'        { & $Python -B -X utf8 $Runner --status }
        'Healthcheck'   { & $Python -B -X utf8 $Runner --healthcheck }
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

[CmdletBinding()]
param(
    [ValidateSet('Collect','Ingest','DailyAll','VerifyOffline','Status','Healthcheck','ServeApi')]
    [string]$Action = 'DailyAll',
    [ValidateSet('v7a','v5a')]
    [string]$Profile = 'v7a',
    [string]$Date,
    [string]$Python,
    [string]$ExportPhase3dV1,
    [ValidateRange(1,65535)]
    [int]$Port = 8000
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
$CollectArgs = @()
if ($Date) { $CollectArgs += @('--date', $Date) }
if ($ExportPhase3dV1) { $CollectArgs += @('--export-phase3d-v1', $ExportPhase3dV1) }

Push-Location -LiteralPath $Repository
try {
    & $Python -B -X utf8 -c 'from core.integrity import require_runtime_guard; require_runtime_guard()'
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($Profile -eq 'v7a') {
        if ($Action -eq 'ServeApi') {
            & $Python -B -X utf8 -m uvicorn backend.app.main:app --host 127.0.0.1 --port $Port
        } elseif ($Action -in 'Status','Healthcheck') {
            $Runner = Join-Path $PSScriptRoot 'run_phase7a_shadow.py'
            & $Python -B -X utf8 $Runner ('--' + $Action.ToLowerInvariant())
        } else {
            & $Python -B -X utf8 -m backend.app.services.ops_service --action $Action @CollectArgs
        }
        exit $LASTEXITCODE
    }
    if ($Action -eq 'ServeApi') {
        throw 'ServeApi requires the v7a profile'
    }
    $Runner = Join-Path $PSScriptRoot 'run_phase5a_multi_shadow.py'
    if ($Action -in 'Collect','DailyAll') {
        & $Python -B -X utf8 $Collector --profile v5a-continuation @CollectArgs
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    switch ($Action) {
        'Ingest'        { & $Python -B -X utf8 $Runner --continuation --ingest-ready }
        'DailyAll'      { & $Python -B -X utf8 $Runner --continuation --ingest-ready }
        'VerifyOffline' { & $Python -B -X utf8 $Runner --continuation --verify-offline }
        'Status'        { & $Python -B -X utf8 $Runner --continuation --status }
        'Healthcheck'   { & $Python -B -X utf8 $Runner --continuation --healthcheck }
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

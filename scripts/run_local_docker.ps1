[CmdletBinding()]
param(
    [ValidateSet('Up', 'Down', 'Rebuild', 'Status', 'Logs', 'Smoke')]
    [string]$Action = 'Up'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtime = Join-Path $repo 'data/fetch_cache/runtimes/batch1-py311/Scripts/python.exe'
if (Test-Path -LiteralPath $runtime -PathType Leaf) {
    $python = @($runtime)
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $python = @('py', '-3.11')
} else {
    $python = @('python')
}
$exe, $prefix = $python
# The Python launcher probes PATH, both Docker Desktop install scopes, and WSL.
# Resolve the repository explicitly so calls from other directories use the same Compose project.
Push-Location -LiteralPath $repo
try {
    & $exe @prefix -B -X utf8 (Join-Path $repo 'deploy/local_docker.py') $Action
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code

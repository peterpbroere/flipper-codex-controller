param([ValidatePattern('^(auto|COM[0-9]+)$')][string]$Port = 'auto', [switch]$Demo, [switch]$Background)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the project first; see README.md.' }
if ($Background) {
    $bridgeArgs = @('-m', 'agent_deck', 'background', 'start', '--port', $Port)
    if ($Demo) { $bridgeArgs += '--demo' }
    & $python @bridgeArgs
} else {
    $mode = if ($Demo) { 'demo' } else { 'run' }
    & $python -m agent_deck $mode --port $Port
}
exit $LASTEXITCODE

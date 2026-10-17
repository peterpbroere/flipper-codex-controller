$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the project first; see README.md.' }
$stateDir = Join-Path $repoRoot '.agent-deck'
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
Start-Process -FilePath $python -ArgumentList @('-m', 'agent_deck', 'gui') -WorkingDirectory $repoRoot -WindowStyle Normal -RedirectStandardOutput (Join-Path $stateDir 'companion.stdout.log') -RedirectStandardError (Join-Path $stateDir 'companion.stderr.log')

$ErrorActionPreference = "Stop"
$engineRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $engineRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
  throw "엔진 환경이 없습니다. 먼저 engine\.venv를 설치하세요."
}

if (-not $env:LOCALFORGE_BACKEND) {
  $env:LOCALFORGE_BACKEND = "mock"
}

Set-Location $engineRoot
& $pythonPath -m uvicorn app:app --host 127.0.0.1 --port 8000

param(
    [int]$Port = 8099,
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"
if (-not $DataDir) {
    $DataDir = Join-Path ([IO.Path]::GetTempPath()) ("webagent-perf-" + [guid]::NewGuid().ToString("N"))
}
$resolvedDataDir = [IO.Path]::GetFullPath($DataDir)
New-Item -ItemType Directory -Path $resolvedDataDir -Force | Out-Null

$env:WEBAGENT_PERF_TEST_MODE = "1"
$env:WEBAGENT_PERF_TEST_DATA_DIR = $resolvedDataDir
$env:WEBAGENT_CONFIG_SOURCE = "env"
$env:WEBAGENT_DB_MODE = "local"
$env:WEBAGENT_PORT = [string]$Port

Write-Host "Performance-test data: $resolvedDataDir"
Write-Host "Performance-test URL:  http://localhost:$Port/app"
python run.py

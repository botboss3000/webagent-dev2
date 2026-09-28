param(
    [Parameter(Position = 0)]
    [string]$Command = "refresh",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$scriptPath = Join-Path $PSScriptRoot "ast_index.py"
& uv run --script $scriptPath $Command @Rest
exit $LASTEXITCODE

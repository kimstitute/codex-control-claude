$cli = Join-Path $PSScriptRoot 'claude_control_cli.py'
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $cli @args
} else {
    & python $cli @args
}
exit $LASTEXITCODE

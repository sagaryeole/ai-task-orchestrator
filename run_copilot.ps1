param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$pythonCmd = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } `
    elseif (Get-Command python3 -ErrorAction SilentlyContinue) { "python3" } `
    elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } `
    else { throw "No Python interpreter found on PATH (tried 'python', 'python3', 'py')." }

& $pythonCmd orchestrator.py --config task-orchestrator.config.copilot.json @ExtraArgs

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$OrchestratorArgs
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$pythonCmd = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } `
    elseif (Get-Command python3 -ErrorAction SilentlyContinue) { "python3" } `
    elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } `
    else { throw "No Python interpreter found on PATH (tried 'python', 'python3', 'py')." }

while ($true) {
    & $pythonCmd orchestrator.py @OrchestratorArgs
    $code = $LASTEXITCODE

    if ($code -eq 0) {
        Write-Host "[supervisor] Orchestrator finished normally (all tasks done, or stopped on purpose). Not restarting."
        break
    }
    elseif ($code -eq 130) {
        Write-Host "[supervisor] Interrupted by Ctrl+C. Not restarting."
        break
    }
    else {
        Write-Host "[supervisor] Orchestrator exited unexpectedly (code $code). Restarting in 10s..."
        Start-Sleep -Seconds 10
    }
}

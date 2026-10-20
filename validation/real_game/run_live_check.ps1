# Execute the regular AutoAdv GUI against an already running game, then audit
# its observations. Does NOT build, start, control, or instrument the game.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('hook', 'reading', 'native', 'tts', 'mixed', 'stress')]
    [string] $Phase,
    [int] $StartLine = 0,
    [string] $Trace = ''
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if (-not $Trace) {
    $Trace = Join-Path $env:TEMP ('autoadv-real-' + [guid]::NewGuid().ToString('N') + '.jsonl')
}
if (Test-Path -LiteralPath $Trace) {
    throw "Refusing to overwrite a prior observation trace: $Trace"
}
$oldTrace = $env:AUTOADV_VALIDATION_TRACE
try {
    $env:AUTOADV_VALIDATION_TRACE = $Trace
    Push-Location $repo
    try {
        Write-Host "Game must already be running. Starting normal AutoAdv GUI. Trace: $Trace"
        python -m autoadv
        if ($LASTEXITCODE -ne 0) {
            throw "AutoAdv GUI returned exit code $LASTEXITCODE; no acceptance is possible"
        }
        $arguments = @('-m', 'validation.real_game.verify_trace', '--trace', $Trace, '--phase', $Phase)
        if ($StartLine -gt 0) {
            $arguments += @('--start-line', [string]$StartLine)
        }
        & python @arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Real game evidence failed verification (phase=$Phase, trace=$Trace)"
        }
        Write-Host "Verified AutoAdv observation phase: $Phase"
    } finally {
        Pop-Location
    }
} finally {
    $env:AUTOADV_VALIDATION_TRACE = $oldTrace
}

# Execute AutoAdv's ordinary GUI against the existing Ren'Py validation game.
# Optional SDK launch is environment setup, not a game-engine acceptance test.
# No game-side code issues advance inputs or supplies observations to AutoAdv.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('hook', 'reading', 'native', 'tts', 'mixed', 'stress')]
    [string] $Phase,
    [int] $StartLine = 0,
    [string] $Trace = '',
    [string] $Oracle = '',
    [string] $Report = '',
    [string] $RenPySdk = $env:AUTOADV_RENPY_SDK,
    [int] $MaxVoiceHoldMs = 2000
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if (-not $Trace) {
    $Trace = Join-Path $env:TEMP ('autoadv-real-' + [guid]::NewGuid().ToString('N') + '.jsonl')
}
if ($MaxVoiceHoldMs -le 0) { throw 'MaxVoiceHoldMs must be positive' }
if (-not $Report) { $Report = $Trace + '.result.json' }
if (Test-Path -LiteralPath $Report) {
    throw "Refusing to overwrite a prior validation report: $Report"
}
if (Test-Path -LiteralPath $Trace) {
    throw "Refusing to overwrite a prior observation trace: $Trace"
}

# Scene selection is an external fixture condition. It does not alter the
# game's dialogue advancement or AutoAdv's Hook/audio/controller/input path.
$scene = switch ($Phase) {
    'native' { 'native' }
    'tts'    { 'tts' }
    'stress' { 'stress' }
    'mixed'  { 'mixed' }
    default  { 'start' }
}
if ($RenPySdk -and $StartLine -eq 0) {
    $StartLine = switch ($scene) {
        'native' { 6 }
        'tts'    { 4 }
        default  { 1 }
    }
}
if ($RenPySdk -and -not $Oracle) {
    $Oracle = Join-Path $env:TEMP ('autoadv-game-' + [guid]::NewGuid().ToString('N') + '.jsonl')
}
if ($Phase -in @('native', 'mixed', 'stress') -and -not $Oracle) {
    throw "Phase $Phase requires game voice observations. Supply -Oracle or -RenPySdk."
}
if ($RenPySdk -and (Test-Path -LiteralPath $Oracle)) {
    throw "Refusing to reuse an existing game observation file: $Oracle"
}

$oldTrace = $env:AUTOADV_VALIDATION_TRACE
$oldOracle = $env:AUTOADV_GAME_ORACLE
$oldScene = $env:AUTOADV_VALIDATION_SCENARIO
$gameProcess = $null
try {
    if ($RenPySdk) {
        $sdk = (Resolve-Path -LiteralPath $RenPySdk).Path
        $pythonExe = Join-Path $sdk 'lib/py3-windows-x86_64/python.exe'
        $renpyScript = Join-Path $sdk 'renpy.py'
        $project = Join-Path $repo 'validation/real_game/renpy'
        if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf) -or
            -not (Test-Path -LiteralPath $renpyScript -PathType Leaf)) {
            throw "Ren'Py SDK runtime not found at $sdk"
        }
        $env:AUTOADV_GAME_ORACLE = $Oracle
        $env:AUTOADV_VALIDATION_SCENARIO = $scene
        # This is the existing game and Ren'Py's documented normal 'run'
        # command. It is neither a second game nor a game-side input driver.
        $gameProcess = Start-Process -FilePath $pythonExe -WorkingDirectory $sdk `
            -ArgumentList "`"$renpyScript`" `"$project`" run" -PassThru
        $deadline = [System.Diagnostics.Stopwatch]::StartNew()
        while (-not (Test-Path -LiteralPath $Oracle -PathType Leaf)) {
            if ($gameProcess.HasExited) {
                throw "Ren'Py game exited before writing its observation start record ($($gameProcess.ExitCode))"
            }
            if ($deadline.Elapsed.TotalSeconds -gt 40) {
                throw 'RenPy game did not start observation within 40 seconds'
            }
            Start-Sleep -Milliseconds 200
        }
        Write-Host "Existing Ren'Py validation game started (scene=$scene; observer=$Oracle)"
    } else {
        Write-Host 'Using an already running game; no game input is driven by the checker.'
    }

    $env:AUTOADV_VALIDATION_TRACE = $Trace
    Push-Location $repo
    try {
        # All advancement remains in the production GUI Controller.
        python -m autoadv
        if ($LASTEXITCODE -ne 0) {
            throw "AutoAdv GUI returned exit code $LASTEXITCODE; no acceptance is possible"
        }
        $arguments = @('-m', 'validation.real_game.verify_trace', '--trace', $Trace,
                       '--phase', $Phase, '--report', $Report,
                       '--max-voice-hold-ms', [string]$MaxVoiceHoldMs)
        if ($Oracle) { $arguments += @('--oracle', $Oracle) }
        if ($StartLine -gt 0) { $arguments += @('--start-line', [string]$StartLine) }
        & python @arguments
        Write-Host "AutoAdv validation report: $Report"
        if ($LASTEXITCODE -ne 0) {
            throw "Real game evidence failed verification (phase=$Phase, trace=$Trace)"
        }
        Write-Host "Verified AutoAdv observation phase: $Phase"
    } finally {
        Pop-Location
    }
} finally {
    $env:AUTOADV_VALIDATION_TRACE = $oldTrace
    $env:AUTOADV_GAME_ORACLE = $oldOracle
    $env:AUTOADV_VALIDATION_SCENARIO = $oldScene
    if ($null -ne $gameProcess -and -not $gameProcess.HasExited) {
        # Only the process launched by this invocation is eligible for cleanup.
        Stop-Process -Id $gameProcess.Id -ErrorAction SilentlyContinue
    }
}

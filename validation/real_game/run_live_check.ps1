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
    [switch] $Automated,
    [string] $Voice = '',
    [string] $IrodoriUrl = 'http://127.0.0.1:8088',
    [int] $TimeoutSeconds = 120,
    [int] $MaxVoiceHoldMs = 0,
    [switch] $UncertainFallback
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$scratch = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
if (-not $Trace) {
    $Trace = Join-Path $scratch ('autoadv-real-' + [guid]::NewGuid().ToString('N') + '.jsonl')
}
if ($MaxVoiceHoldMs -lt 0) { throw 'MaxVoiceHoldMs cannot be negative' }
if ($Automated -and -not $RenPySdk) {
    throw 'Automated real-game validation requires -RenPySdk to launch the existing game'
}
if ($Automated -and $TimeoutSeconds -lt 10) { throw 'Finite test timeout must be >= 10 seconds' }
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
        'native' { if ($Automated) { 10 } else { 6 } }
        'tts'    { 4 }
        default  { 1 }
    }
}
if ($RenPySdk -and $Phase -in @('native', 'mixed', 'stress') -and -not $Oracle) {
    $Oracle = Join-Path $scratch ('autoadv-game-' + [guid]::NewGuid().ToString('N') + '.jsonl')
}
if ($Phase -in @('native', 'mixed', 'stress') -and -not $Oracle) {
    throw "Phase $Phase requires game voice observations. Supply -Oracle or -RenPySdk."
}
if ($RenPySdk -and $Oracle -and (Test-Path -LiteralPath $Oracle)) {
    throw "Refusing to reuse an existing game observation file: $Oracle"
}

$oldTrace = $env:AUTOADV_VALIDATION_TRACE
$oldOracle = $env:AUTOADV_GAME_ORACLE
$oldScene = $env:AUTOADV_VALIDATION_SCENARIO
$oldGate = $env:AUTOADV_VALIDATION_CONTROL_FILE
$oldSession = $env:AUTOADV_VALIDATION_SESSION
$oldAppData = $env:APPDATA
$gate = Join-Path $scratch ('autoadv-gate-' + [guid]::NewGuid().ToString('N'))
$settingsDir = Join-Path $scratch ('autoadv-settings-' + [guid]::NewGuid().ToString('N'))
$gameProcess = $null
try {
    if ($Automated) {
        # Only the normal AutoAdv GUI uses this isolated settings root.
        # No user's saved game/voice/Hook preferences are overwritten.
        $null = New-Item -ItemType Directory -Path $settingsDir
        $env:APPDATA = $settingsDir
        $env:AUTOADV_VALIDATION_CONTROL_FILE = $gate
    }
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
        while ($Oracle -and -not (Test-Path -LiteralPath $Oracle -PathType Leaf)) {
            if ($gameProcess.HasExited) {
                throw "Ren'Py game exited before writing its observation start record ($($gameProcess.ExitCode))"
            }
            if ($deadline.Elapsed.TotalSeconds -gt 40) {
                throw 'RenPy game did not start observation within 40 seconds'
            }
            Start-Sleep -Milliseconds 200
        }
        Write-Host "Existing Ren'Py validation game launched (scene=$scene)"
    } else {
        Write-Host 'Using an already running game; no game input is driven by the checker.'
    }

    $env:AUTOADV_VALIDATION_TRACE = $Trace
    Push-Location $repo
    try {
        # All advancement remains in the production GUI Controller.
        if ($Automated) {
            $gameExe = (Resolve-Path -LiteralPath $pythonExe).Path
            $config = @{
                oracle = $Oracle; gate = $gate; executable = $gameExe;
                phase = $Phase; timeout_seconds = $TimeoutSeconds;
                dialogues = $(switch ($Phase) {
                    'hook' { 1 }; 'reading' { 3 }; 'native' { 3 };
                    'tts' { 3 }; 'mixed' { 19 }; 'stress' { 9 }
                });
                voice = $Voice; irodori_url = $IrodoriUrl;
                fallback = [bool]$UncertainFallback
            }
            if ($Phase -in @('native', 'mixed', 'stress')) {
                # Build only AutoAdv's process-audio bridge, not Luna or other suites.
                $helper = Join-Path $repo 'integrations/windows-loopback/bin/Release/net9.0-windows/AutoAdv.ProcessLoopback.dll'
                if (-not (Test-Path -LiteralPath $helper)) {
                    & dotnet build (Join-Path $repo 'integrations/windows-loopback/AutoAdv.ProcessLoopback.csproj') --configuration Release --nologo
                    if ($LASTEXITCODE -ne 0) { throw 'Real process loopback helper build failed' }
                }
                $modelRoot = & python -c 'from validation.real_game.prepare_asr_model import default_model_directory; print(default_model_directory())'
                if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve verified ASR model directory' }
                & python -m validation.real_game.prepare_asr_model --directory $modelRoot
                if ($LASTEXITCODE -ne 0) { throw 'Pinned production ASR model unavailable' }
                $config.helper = $helper
                $config.model = Join-Path $modelRoot 'model.int8.onnx'
                $config.tokens = Join-Path $modelRoot 'tokens.txt'
            }
            $env:AUTOADV_VALIDATION_SESSION = ConvertTo-Json -InputObject $config -Compress
        }
        python -m autoadv
        $guiExit = $LASTEXITCODE
        $arguments = @('-m', 'validation.real_game.verify_trace', '--trace', $Trace,
                       '--phase', $Phase, '--report', $Report)
        if ($MaxVoiceHoldMs -gt 0) { $arguments += @('--max-voice-hold-ms', [string]$MaxVoiceHoldMs) }
        if ($Oracle) { $arguments += @('--oracle', $Oracle) }
        if ($StartLine -gt 0) { $arguments += @('--start-line', [string]$StartLine) }
        if (Test-Path -LiteralPath $Trace) {
            & python @arguments
        } else {
            @{ok = $false; phase = $Phase; errors = @('GUI did not produce a product trace')} |
                ConvertTo-Json | Set-Content -LiteralPath $Report -Encoding utf8
            $LASTEXITCODE = 1
        }
        $auditExit = $LASTEXITCODE
        Write-Host "AutoAdv validation report: $Report"
        if ($guiExit -ne 0) {
            throw "AutoAdv GUI returned exit code $guiExit; game not verified"
        }
        $LASTEXITCODE = $auditExit
        if ($LASTEXITCODE -ne 0) {
            throw "Real game evidence failed verification (phase=$Phase, trace=$Trace)"
        }
        Write-Host "Collected and audited AutoAdv product observations (phase=$Phase). Check report advancement_observed and voice_safety_verified independently."
    } finally {
        Pop-Location
    }
} finally {
    $env:AUTOADV_VALIDATION_TRACE = $oldTrace
    $env:AUTOADV_GAME_ORACLE = $oldOracle
    $env:AUTOADV_VALIDATION_SCENARIO = $oldScene
    $env:AUTOADV_VALIDATION_CONTROL_FILE = $oldGate
    $env:AUTOADV_VALIDATION_SESSION = $oldSession
    $env:APPDATA = $oldAppData
    if ($null -ne $gameProcess -and -not $gameProcess.HasExited) {
        # Only the process launched by this invocation is eligible for cleanup.
        Stop-Process -Id $gameProcess.Id -ErrorAction SilentlyContinue
    }
}

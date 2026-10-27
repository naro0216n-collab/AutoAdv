# Execute AutoAdv's ordinary GUI against the existing Ren'Py validation game.
# Automated runs launch a prebuilt validation game. Fixture construction is separate.
# No game-side code issues advance inputs or supplies observations to AutoAdv.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('hook', 'reading', 'accuracy', 'tts', 'mixed', 'stress')]
    [string] $Phase,
    [string] $Trace = '',
    [string] $Evidence = '',
    [string] $Report = '',
    [string] $GameExecutable = $env:AUTOADV_VALIDATION_GAME,
    [switch] $Automated,
    [string] $Voice = '',
    [string] $IrodoriUrl = 'http://127.0.0.1:8088',
    [int] $TimeoutSeconds = 120,
    [switch] $UncertainFallback,
    [string] $PythonExecutable = 'python'
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$scratch = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
if (-not $Trace) {
    $Trace = Join-Path $scratch ('autoadv-real-' + [guid]::NewGuid().ToString('N') + '.jsonl')
}
if ($Automated -and -not $GameExecutable) {
    throw 'Automated real-game validation requires a prebuilt -GameExecutable'
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
    'accuracy' { 'accuracy' }
    'tts'    { 'tts' }
    'stress' { 'stress' }
    'mixed'  { 'mixed' }
    default  { 'start' }
}
# The fixture evidence file records authored case starts and direct Ren'Py voice-command facts.
# It does not inspect Hook state or AutoAdv decisions.
if ($GameExecutable -and $Phase -eq 'accuracy' -and -not $Evidence) {
    $Evidence = Join-Path $scratch ('autoadv-fixture-' + [guid]::NewGuid().ToString('N') + '.jsonl')
}
if ($Phase -eq 'accuracy' -and -not $Evidence) {
    throw "Phase $Phase requires fixture evidence. Supply -Evidence or launch the validation fixture."
}
if ($GameExecutable -and $Evidence -and (Test-Path -LiteralPath $Evidence)) {
    throw "Refusing to reuse an existing fixture evidence file: $Evidence"
}

$oldTrace = $env:AUTOADV_VALIDATION_TRACE
$oldEvidence = $env:AUTOADV_GAME_EVIDENCE
$oldScene = $env:AUTOADV_VALIDATION_SCENARIO
$oldGate = $env:AUTOADV_VALIDATION_CONTROL_FILE
$oldSession = $env:AUTOADV_VALIDATION_SESSION
$oldAppData = $env:APPDATA
$gate = Join-Path $scratch ('autoadv-gate-' + [guid]::NewGuid().ToString('N'))
$settingsDir = Join-Path $scratch ('autoadv-settings-' + [guid]::NewGuid().ToString('N'))
$gameProcess = $null

function Invoke-ValidationPython([string[]] $CommandArgs) {
    # Windows PowerShell 5.1 promotes native stderr to an ErrorRecord.  With
    # ErrorActionPreference=Stop that can terminate validation before the
    # native process exit code is observed, even for warnings emitted by Qt or
    # a native library.  Preserve stderr in the transcript and decide success
    # exclusively from the Python process exit code.
    $savedErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $PythonExecutable @CommandArgs 2>&1 | ForEach-Object { Write-Host ([string]$_) }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedErrorAction
    }
    return [int]$exitCode
}

try {
    if ($Automated) {
        # Only the normal AutoAdv GUI uses this isolated settings root.
        # No user's saved game/voice/Hook preferences are overwritten.
        $null = New-Item -ItemType Directory -Path $settingsDir
        $env:APPDATA = $settingsDir
        $env:AUTOADV_VALIDATION_CONTROL_FILE = $gate
    }
    $resolvedGameExe = ''
    if ($GameExecutable) {
        $resolvedGameExe = (Resolve-Path -LiteralPath $GameExecutable).Path
        if (-not (Test-Path -LiteralPath $resolvedGameExe -PathType Leaf)) {
            throw "Prebuilt validation game executable not found: $resolvedGameExe"
        }
        $env:AUTOADV_GAME_EVIDENCE = $Evidence
        $env:AUTOADV_VALIDATION_SCENARIO = $scene
        # Fixture build/packaging is deliberately outside this acceptance run.
        # This step only starts an already built game process.
        $gameProcess = Start-Process -FilePath $resolvedGameExe `
            -WorkingDirectory (Split-Path -Parent $resolvedGameExe) -PassThru
    }
    if ($null -ne $gameProcess) {
        $deadline = [System.Diagnostics.Stopwatch]::StartNew()
        while ($Evidence -and -not (Test-Path -LiteralPath $Evidence -PathType Leaf)) {
            if ($gameProcess.HasExited) {
                throw "Validation game exited before writing its fixture evidence start record ($($gameProcess.ExitCode))"
            }
            if ($deadline.Elapsed.TotalSeconds -gt 40) {
                throw 'Validation game did not start fixture evidence within 40 seconds'
            }
            Start-Sleep -Milliseconds 200
        }
        Write-Host "Validation game launched (scene=$scene)"
    } else {
        Write-Host 'Using an already running game; normal advancement remains product-owned.'
    }

    $env:AUTOADV_VALIDATION_TRACE = $Trace
    Push-Location $repo
    try {
        # Normal advancement remains in the production GUI Controller. Accuracy may add fixed-time authored Windows game inputs; watchdog recovery remains a separate escape hatch only when the fixture case does not change.
        if ($Automated) {
            $gameExe = $resolvedGameExe
            $config = @{
                evidence = $Evidence; gate = $gate; executable = $gameExe;
                phase = $Phase; timeout_seconds = $TimeoutSeconds;
                dialogues = $(switch ($Phase) {
                    'hook' { 1 }; 'reading' { 3 };
                    'accuracy' { @((Get-Content -LiteralPath (Join-Path $repo 'validation\real_game\ADVANCEMENT_ACCURACY_SCENARIO.json') -Raw -Encoding UTF8 | ConvertFrom-Json).lines).Count };
                    'tts' { 3 }; 'mixed' { 19 }; 'stress' { 9 }
                });
                continue_collection = ($Phase -eq 'accuracy');
                voice = $Voice; irodori_url = $IrodoriUrl;
                fallback = [bool](($Phase -eq 'accuracy') -or $UncertainFallback);
                force_advance_seconds = $(if ($Phase -eq 'accuracy') { 10 } else { 30 });
                case_watchdog_seconds = 12.0; final_observation_seconds = 12.0
            }
            $env:AUTOADV_VALIDATION_SESSION = ConvertTo-Json -InputObject $config -Compress
        }
        if ($Automated) {
            $guiExit = Invoke-ValidationPython @('-m', 'validation.real_game.gui_entry')
        } else {
            $guiExit = Invoke-ValidationPython @('-m', 'autoadv')
        }
        $arguments = @('-m', 'validation.real_game.verify_trace', '--trace', $Trace,
                       '--phase', $Phase, '--report', $Report)
        if ($Evidence) { $arguments += @('--evidence', $Evidence) }
        if (Test-Path -LiteralPath $Trace) {
            $auditExit = Invoke-ValidationPython $arguments
        } else {
            @{schema = 'autoadv-offline-verification-v1'; phase = $Phase; verdict = 'incomplete';
              evidence_issues = @('GUI did not produce a product trace')} |
                ConvertTo-Json | Set-Content -LiteralPath $Report -Encoding utf8
            $auditExit = 1
        }
        Write-Host "AutoAdv validation report: $Report"
        if ($guiExit -ne 0) {
            throw "AutoAdv GUI returned exit code $guiExit before evidence collection completed"
        }
        if ($auditExit -ne 0) {
            throw "Offline verification could not read collected evidence (phase=$Phase, trace=$Trace)"
        }
        Write-Host "Collected AutoAdv and fixture evidence and wrote offline comparison (phase=$Phase)."
    } finally {
        Pop-Location
    }
} finally {
    $env:AUTOADV_VALIDATION_TRACE = $oldTrace
    $env:AUTOADV_GAME_EVIDENCE = $oldEvidence
    $env:AUTOADV_VALIDATION_SCENARIO = $oldScene
    $env:AUTOADV_VALIDATION_CONTROL_FILE = $oldGate
    $env:AUTOADV_VALIDATION_SESSION = $oldSession
    $env:APPDATA = $oldAppData
    if ($null -ne $gameProcess -and -not $gameProcess.HasExited) {
        # Only the process launched by this invocation is eligible for cleanup.
        Stop-Process -Id $gameProcess.Id -ErrorAction SilentlyContinue
    }
}

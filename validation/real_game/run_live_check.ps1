# Execute AutoAdv's ordinary GUI against the existing Ren'Py validation game.
# Prebuilt fixture launch is supported without constructing the game in this run.
# The RenPySdk source-fixture path is retained only across the workflow migration checkpoint.
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
    [string] $GameExecutable = $env:AUTOADV_VALIDATION_GAME,
    [string] $RenPySdk = $env:AUTOADV_RENPY_SDK,
    [switch] $Automated,
    [string] $Voice = '',
    [string] $IrodoriUrl = 'http://127.0.0.1:8088',
    [string] $ProcessLoopbackHelper = '',
    [string] $AsrModel = '',
    [string] $AsrTokens = '',
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
if ($GameExecutable -and $RenPySdk) {
    throw 'Specify either -GameExecutable or -RenPySdk, not both'
}
if ($Automated -and -not ($GameExecutable -or $RenPySdk)) {
    throw 'Automated real-game validation requires -GameExecutable (or -RenPySdk for fixture-source development)'
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
if (($GameExecutable -or $RenPySdk) -and $StartLine -eq 0) {
    $StartLine = switch ($scene) {
        'native' { 6 }
        'tts'    { 4 }
        default  { 1 }
    }
}
# Text-mode SendInput without a following Hook needs existing game-side
# dismissal evidence to distinguish unhandled input from missed text capture.
# This is diagnostics only, not a new game-state acceptance criterion.
if (($GameExecutable -or $RenPySdk) -and ($Phase -in @('native', 'mixed', 'stress') -or
        ($Automated -and $Phase -eq 'reading')) -and -not $Oracle) {
    $Oracle = Join-Path $scratch ('autoadv-game-' + [guid]::NewGuid().ToString('N') + '.jsonl')
}
if ($Phase -in @('native', 'mixed', 'stress') -and -not $Oracle) {
    throw "Phase $Phase requires game voice observations. Supply -Oracle or launch the validation fixture."
}
if (($GameExecutable -or $RenPySdk) -and $Oracle -and (Test-Path -LiteralPath $Oracle)) {
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
    if ($Automated -and $Phase -eq 'native') {
        # The hosted Windows runner may have no usable render endpoint. Report
        # the OS prerequisite separately from game and AutoAdv acceptance.
        # This does not play audio, inspect game state, or alter the product path.
        $audioEnvironment = [ordered]@{}
        foreach ($serviceName in @('Audiosrv', 'AudioEndpointBuilder')) {
            $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
            $audioEnvironment[$serviceName] = if ($service) { [string]$service.Status } else { 'not_found' }
        }
        try {
            $audioEnvironment['sound_devices'] = @(
                Get-CimInstance -ClassName Win32_SoundDevice -ErrorAction Stop |
                    ForEach-Object { [ordered]@{ name = $_.Name; status = $_.Status } }
            )
        } catch {
            $audioEnvironment['sound_devices_error'] = $_.Exception.Message
        }
        try {
            $audioEnvironment['audio_endpoints'] = @(
                Get-PnpDevice -Class AudioEndpoint -ErrorAction Stop |
                    ForEach-Object { [ordered]@{ name = $_.FriendlyName; status = [string]$_.Status } }
            )
        } catch {
            $audioEnvironment['audio_endpoints_error'] = $_.Exception.Message
        }
        # PnP and Win32_SoundDevice do not enumerate the same objects as the
        # Core Audio API. Query the render endpoints through IMMDeviceEnumerator
        # itself before attributing silent Process Loopback PCM to the runner.
        try {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
public class AudioDeviceEnumeratorClass {}
[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioDeviceEnumerator {
    void EnumAudioEndpoints(int flow, uint mask, out IAudioDeviceCollection devices);
    void GetDefaultAudioEndpoint(int flow, int role, out IAudioDevice device);
}
[ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioDeviceCollection {
    void GetCount(out uint count);
    void Item(uint index, out IAudioDevice device);
}
[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioDevice {}
public static class AudioRenderProbe {
    public static string Inspect() {
        var enumerator = (IAudioDeviceEnumerator)new AudioDeviceEnumeratorClass();
        try {
            // eRender=0, DEVICE_STATE_ACTIVE=1, eMultimedia=1.
            IAudioDeviceCollection collection;
            enumerator.EnumAudioEndpoints(0, 1, out collection);
            uint count;
            try { collection.GetCount(out count); }
            finally { Marshal.ReleaseComObject(collection); }
            string defaultResult;
            try {
                IAudioDevice device;
                enumerator.GetDefaultAudioEndpoint(0, 1, out device);
                defaultResult = "present";
                Marshal.ReleaseComObject(device);
            } catch (COMException e) {
                defaultResult = "missing_hresult_0x" + ((uint)e.HResult).ToString("X8");
            }
            return "active_render_count=" + count + " default_multimedia_render=" + defaultResult;
        } finally { Marshal.ReleaseComObject(enumerator); }
    }
}
'@ -ErrorAction Stop
            $audioEnvironment['wasapi_render'] = [AudioRenderProbe]::Inspect()
        } catch {
            $audioEnvironment['wasapi_render_probe_error'] = $_.Exception.Message
        }
        Write-Host ('Windows render environment (diagnostic only): ' +
            (ConvertTo-Json -InputObject $audioEnvironment -Compress -Depth 4))
    }
    $resolvedGameExe = ''
    $sourceFixturePython = ''
    if ($GameExecutable) {
        $resolvedGameExe = (Resolve-Path -LiteralPath $GameExecutable).Path
        if (-not (Test-Path -LiteralPath $resolvedGameExe -PathType Leaf)) {
            throw "Prebuilt validation game executable not found: $resolvedGameExe"
        }
        $env:AUTOADV_GAME_ORACLE = $Oracle
        $env:AUTOADV_VALIDATION_SCENARIO = $scene
        # Fixture build/packaging is deliberately outside this acceptance run.
        # This step only starts an already built game process.
        $gameProcess = Start-Process -FilePath $resolvedGameExe `
            -WorkingDirectory (Split-Path -Parent $resolvedGameExe) -PassThru
    } elseif ($RenPySdk) {
        # Source-fixture launch remains available across the workflow migration
        # checkpoint; the prebuilt Live workflow replaces it in the next publish.
        $sdk = (Resolve-Path -LiteralPath $RenPySdk).Path
        $sourceFixturePython = Join-Path $sdk 'lib/py3-windows-x86_64/python.exe'
        $renpyScript = Join-Path $sdk 'renpy.py'
        $project = Join-Path $repo 'validation/real_game/renpy'
        if (-not (Test-Path -LiteralPath $sourceFixturePython -PathType Leaf) -or
            -not (Test-Path -LiteralPath $renpyScript -PathType Leaf)) {
            throw "Ren'Py SDK runtime not found at $sdk"
        }
        $resolvedGameExe = (Resolve-Path -LiteralPath $sourceFixturePython).Path
        $env:AUTOADV_GAME_ORACLE = $Oracle
        $env:AUTOADV_VALIDATION_SCENARIO = $scene
        $gameProcess = Start-Process -FilePath $sourceFixturePython -WorkingDirectory $sdk `
            -ArgumentList "`"$renpyScript`" `"$project`" run" -PassThru
    }
    if ($null -ne $gameProcess) {
        $deadline = [System.Diagnostics.Stopwatch]::StartNew()
        while ($Oracle -and -not (Test-Path -LiteralPath $Oracle -PathType Leaf)) {
            if ($gameProcess.HasExited) {
                throw "Validation game exited before writing its observation start record ($($gameProcess.ExitCode))"
            }
            if ($deadline.Elapsed.TotalSeconds -gt 40) {
                throw 'Validation game did not start observation within 40 seconds'
            }
            Start-Sleep -Milliseconds 200
        }
        Write-Host "Validation game launched (scene=$scene)"
    } else {
        Write-Host 'Using an already running game; no game input is driven by the checker.'
    }

    $env:AUTOADV_VALIDATION_TRACE = $Trace
    Push-Location $repo
    try {
        # All advancement remains in the production GUI Controller.
        if ($Automated) {
            $gameExe = $resolvedGameExe
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
                $externalAudioRuntime = ($ProcessLoopbackHelper -and $AsrModel -and $AsrTokens)
                if (-not $externalAudioRuntime -and $RenPySdk) {
                    # The source-fixture path keeps the current Live workflow valid
                    # until the workflow-only migration is published.
                    $ProcessLoopbackHelper = Join-Path $repo 'integrations/windows-loopback/bin/Release/net9.0-windows/AutoAdv.ProcessLoopback.dll'
                    if (-not (Test-Path -LiteralPath $ProcessLoopbackHelper)) {
                        & dotnet build (Join-Path $repo 'integrations/windows-loopback/AutoAdv.ProcessLoopback.csproj') --configuration Release --nologo
                        if ($LASTEXITCODE -ne 0) { throw 'Real process loopback helper build failed' }
                    }
                    $modelRoot = & python -c 'from validation.real_game.prepare_asr_model import default_model_directory; print(default_model_directory())'
                    if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve verified ASR model directory' }
                    & python -m validation.real_game.prepare_asr_model --directory $modelRoot
                    if ($LASTEXITCODE -ne 0) { throw 'Pinned production ASR model unavailable' }
                    $AsrModel = Join-Path $modelRoot 'model.int8.onnx'
                    $AsrTokens = Join-Path $modelRoot 'tokens.txt'
                }
                foreach ($dependency in @(
                    @{ name = 'Process Loopback helper'; path = $ProcessLoopbackHelper },
                    @{ name = 'ASR model'; path = $AsrModel },
                    @{ name = 'ASR tokens'; path = $AsrTokens }
                )) {
                    if (-not $dependency.path -or -not (Test-Path -LiteralPath $dependency.path -PathType Leaf)) {
                        throw ("Live audio prerequisite is unavailable: " + $dependency.name)
                    }
                }
                $config.helper = (Resolve-Path -LiteralPath $ProcessLoopbackHelper).Path
                $config.model = (Resolve-Path -LiteralPath $AsrModel).Path
                $config.tokens = (Resolve-Path -LiteralPath $AsrTokens).Path
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

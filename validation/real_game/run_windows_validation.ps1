# AutoAdv interactive Windows validation orchestrator.
# This file is the Git-owned implementation used by the thin .cmd bootstrap.
# Keep validation mechanics here so an existing bootstrap follows develop automatically.
[CmdletBinding()]
param(
    [ValidateSet('accuracy')]
    [string] $Phase = 'accuracy',
    [ValidateRange(1, 2147483647)]
    [int] $IssueNumber = 2,
    [switch] $NoUpload,
    [string] $WorkBase = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This launcher requires an interactive Windows desktop.'
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$repository = 'naro0216n-collab/AutoAdv'
if (-not $WorkBase) {
    $WorkBase = Join-Path (Split-Path -Parent $repo) '.autoadv-validation'
}
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$session = Join-Path $WorkBase ('runs\' + $stamp + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$cache = Join-Path $WorkBase 'cache'
$runtime = Join-Path $session 'validation-game'
$oldPath = $env:PATH
$oldVenv = $env:VIRTUAL_ENV
$oldRunnerTemp = $env:RUNNER_TEMP
$oldGhToken = $env:GH_TOKEN
$oldGithubToken = $env:GITHUB_TOKEN
$env:GH_TOKEN = $null
$env:GITHUB_TOKEN = $null
$results = New-Object 'System.Collections.Generic.List[object]'
$stage = 'initialize'
$errorText = $null
$script:GitHubCli = $null
$phaseNames = @('accuracy')
$startedUtc = [DateTime]::UtcNow.ToString('o')
$gitSha = $null
New-Item -ItemType Directory -Force -Path $session, $cache | Out-Null
$lastRunPointer = Join-Path $WorkBase 'last-run.txt'
$transcriptPath = Join-Path $session 'validation.log'
[IO.File]::WriteAllText($lastRunPointer, $session, [Text.UTF8Encoding]::new($false))
$transcriptStarted = $false
try {
    Start-Transcript -Path $transcriptPath -Force | Out-Null
    $transcriptStarted = $true
} catch {
    Write-Warning "Validation transcript could not be started: $($_.Exception.Message)"
}
Write-Host "Validation session: $session"
Write-Host "Persistent log: $transcriptPath"

function Assert-Command([string] $name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "$name is required. Install it before running this validation."
    }
}
function Invoke-Checked([string] $file, [string[]] $commandArgs) {
    & $file @commandArgs
    if ($LASTEXITCODE -ne 0) { throw "$file failed (exit code $LASTEXITCODE)" }
}
function Find-GitHubCli {
    $command = Get-Command 'gh.exe' -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { return [string]$command.Source }
    foreach ($folder in @(
        (Join-Path $env:ProgramFiles 'GitHub CLI'),
        (Join-Path $env:LOCALAPPDATA 'Programs\GitHub CLI')
    )) {
        $candidate = Join-Path $folder 'gh.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}
function Ensure-GitHubCli {
    $executable = Find-GitHubCli
    if ($executable) { return $executable }
    $winget = Get-Command 'winget.exe' -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'GitHub CLI not found; automatic installation needs Windows Package Manager (winget). Local results are preserved.'
    }
    Write-Host 'Installing GitHub CLI from the official WinGet package (first use only)...'
    & $winget.Source install --id GitHub.cli --exact --source winget --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { throw "GitHub CLI installation failed (exit code $LASTEXITCODE)" }
    $executable = Find-GitHubCli
    if (-not $executable) { throw 'GitHub CLI installed but not found in standard locations' }
    return $executable
}
function Ensure-GitHubLogin {
    if ($script:GitHubCli) { return }
    $script:GitHubCli = Ensure-GitHubCli
    $savedErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $script:GitHubCli auth status --hostname github.com 2>$null | Out-Null
        $authExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedErrorAction }
    if ($authExit -ne 0) {
        Write-Host 'First-use GitHub authorization: follow the browser prompt once.'
        & $script:GitHubCli auth login --hostname github.com --git-protocol https --web
        if ($LASTEXITCODE -ne 0) { throw 'GitHub browser authentication was not completed' }
    }
}
function Upload-Result([string] $root, [string] $sha, [string] $pythonExecutable) {
    # GitHub authentication remains a launcher concern, but the repository-local
    # Python reporter owns comment acquisition, selection, PATCH and verification.
    # Issue bodies never cross the PowerShell/Python stdout boundary.
    Ensure-GitHubLogin
    $statusPath = Join-Path $root 'upload-status.json'
    Push-Location $repo
    try {
        & $pythonExecutable -m validation.real_game.issue_reporting `
            --root $root --sha $sha --repository $repository --issue $IssueNumber `
            --gh $script:GitHubCli --status $statusPath
        if ($LASTEXITCODE -ne 0) {
            throw "Issue reporter failed (exit code $LASTEXITCODE); see $statusPath"
        }
    } finally { Pop-Location }
}
function Get-PrebuiltFixture([string] $destination) {
    $gameLines = @(& python -m validation.real_game.fixture_runtime stage `
        --runtime $destination --cache $cache --wait-seconds 600)
    if ($LASTEXITCODE -ne 0 -or $gameLines.Count -ne 1) {
        throw 'Prebuilt validation game staging failed'
    }
    return ([string]$gameLines[0]).Trim()
}
function Remove-CompletedRunResidue([string] $runsRoot, [string] $currentSession) {
    if (-not (Test-Path -LiteralPath $runsRoot -PathType Container)) { return }
    foreach ($run in @(Get-ChildItem -LiteralPath $runsRoot -Directory -ErrorAction SilentlyContinue)) {
        if ($run.FullName -eq $currentSession) { continue }
        # A summary is written only after the evidence run has released
        # its Python/game runtime. Preserve all evidence files and remove only
        # reproducible run environments from completed runs.
        if (-not (Test-Path -LiteralPath (Join-Path $run.FullName 'validation-summary.json') -PathType Leaf)) {
            continue
        }
        foreach ($name in @('venv', 'validation-game')) {
            $disposable = Join-Path $run.FullName $name
            if (Test-Path -LiteralPath $disposable) {
                Remove-Item -LiteralPath $disposable -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

try {
    $stage = 'prerequisites'
    Assert-Command 'git'
    Assert-Command 'py'
    $versionLines = @(& py -3.11 -c 'import sys;print(sys.version_info.major*100+sys.version_info.minor)')
    if ($LASTEXITCODE -ne 0 -or $versionLines.Count -ne 1 -or ([string]$versionLines[0]).Trim() -ne '311') {
        throw 'Python 3.11 is required (py -3.11)'
    }
    $gitShaLines = @(& git -C $repo rev-parse HEAD)
    if ($LASTEXITCODE -ne 0 -or $gitShaLines.Count -ne 1) { throw 'Cannot read validation checkout commit' }
    $gitSha = ([string]$gitShaLines[0]).Trim()
    if ($gitSha -notmatch '^[a-f0-9]{40}$') { throw 'Cannot pin the validation checkout commit' }

    $stage = 'runtime'
    $runtimeLines = @(& py -3.11 (Join-Path $repo 'scripts\prepare_windows_runtime.py') `
        --repo $repo --require-native --json)
    if ($LASTEXITCODE -ne 0 -or -not $runtimeLines.Count) {
        throw 'Standard AutoAdv Windows runtime preparation failed'
    }
    try { $runtimeInfo = ($runtimeLines | Select-Object -Last 1) | ConvertFrom-Json }
    catch { throw "Standard AutoAdv runtime result cannot be parsed: $($_.Exception.Message)" }
    $python = [string]$runtimeInfo.python
    $managedHelper = [string]$runtimeInfo.native.paths.helper_dll
    $managedAlignmentModel = [string]$runtimeInfo.native.paths.alignment_model
    $managedAlignmentVocab = [string]$runtimeInfo.native.paths.alignment_vocab
    foreach ($required in @($python, $managedHelper, $managedAlignmentModel, $managedAlignmentVocab)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Standard AutoAdv runtime asset missing: $required"
        }
    }
    $env:VIRTUAL_ENV = Split-Path -Parent (Split-Path -Parent $python)
    $env:PATH = (Split-Path -Parent $python) + ';' + $oldPath
    $env:RUNNER_TEMP = $session

    $stage = 'fixture'
    Push-Location $repo
    try { $gameExecutable = Get-PrebuiltFixture $runtime }
    finally { Pop-Location }

    foreach ($currentPhase in $phaseNames) {
        $trace = Join-Path $session "autoadv-$currentPhase.jsonl"
        $report = Join-Path $session "autoadv-$currentPhase.result.json"
        $phaseError = $null

        if ($currentPhase -eq 'accuracy') {
            $stage = 'prepare-accuracy'
            Push-Location $repo
            try {
                # Only fixture media remains validation-specific. Product alignment model,
                # G2P and Process Loopback come from the standard AutoAdv runtime.
                $mediaRoot = Join-Path $cache 'media'
                Invoke-Checked 'python' @('-m','validation.real_game.prepare_accuracy_assets',
                    '--cache',$mediaRoot,'--runtime',$runtime)
            } catch {
                $phaseError = "Accuracy setup failed: $($_.Exception.Message)"
                Write-Warning $phaseError
            } finally { Pop-Location }
        }

        if (-not $phaseError) {
            $stage = "validate-$currentPhase"
            Push-Location $repo
            try {
                # Keep the live-check in this PowerShell host so native/Python
                # diagnostics are part of the persistent transcript.  A nested
                # powershell.exe previously discarded the actual stderr and left
                # only an opaque "exited with 1" summary.
                $liveCheck = Join-Path $repo 'validation\real_game\run_live_check.ps1'
                $liveArgs = @{
                    Phase = $currentPhase
                    GameExecutable = $gameExecutable
                    Automated = $true
                    Trace = $trace
                    Report = $report
                    TimeoutSeconds = 360
                    PythonExecutable = $python
                }
                & $liveCheck @liveArgs
            } catch {
                $phaseError = [string]$_.Exception.Message
                Write-Warning "Phase ${currentPhase}: $phaseError"
            } finally { Pop-Location }
        }

        $verification = $null
        if (Test-Path -LiteralPath $report -PathType Leaf) {
            try { $verification = Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json }
            catch { $phaseError = "Verification report cannot be parsed: $($_.Exception.Message)" }
        }
        $results.Add([pscustomobject]@{
            phase = $currentPhase
            facts = if ($verification) { $verification.facts } else { $null }
            report_present = [bool]$verification
            runner_error = $phaseError
        })
    }
} catch {
    $errorText = "Stage ${stage}: $($_.Exception.Message)"
    Write-Warning $errorText
} finally {
    $env:PATH = $oldPath
    $env:VIRTUAL_ENV = $oldVenv
    $env:RUNNER_TEMP = $oldRunnerTemp
    $env:GH_TOKEN = $oldGhToken
    $env:GITHUB_TOKEN = $oldGithubToken
}

$summary = [ordered]@{
    schema = 'autoadv-local-validation-v4'
    repository = $repository
    branch = 'develop'
    commit = $gitSha
    started_utc = $startedUtc
    ended_utc = [DateTime]::UtcNow.ToString('o')
    phase_requested = $Phase
    phases = @($results.ToArray())
    launcher_error = $errorText
    notice = 'Git-owned product-path validation; the external CMD is only a bootstrap.'
}
$summaryPath = Join-Path $session 'validation-summary.json'
[IO.File]::WriteAllText($summaryPath, (ConvertTo-Json -InputObject $summary -Depth 8), [Text.UTF8Encoding]::new($false))
Write-Host "Local results: $session"
Write-Host (Get-Content -LiteralPath $summaryPath -Raw -Encoding UTF8)

$hasProductRun = (-not [string]::IsNullOrEmpty($gitSha)) -and ($results.Count -gt 0)
$uploadError = $null
if (-not $NoUpload -and $hasProductRun) {
    Write-Host "Uploading result to https://github.com/$repository/issues/$IssueNumber"
    try { Upload-Result $session $gitSha $python }
    catch {
        $uploadError = [string]$_.Exception.Message
        Write-Warning "GitHub upload failed: $uploadError"
        Write-Warning "Local results are preserved at $session"
    }
} elseif (-not $NoUpload -and -not $hasProductRun) {
    Write-Warning 'Upload skipped: product validation never started. The local summary remains available.'
}
$finalError = $null
if ($errorText) {
    $finalError = "AutoAdv validation setup failed: $errorText"
} else {
    $incompleteRuns = @($results.ToArray() | Where-Object {
        $_.runner_error -or $_.report_present -ne $true
    })
    if ($results.Count -ne $phaseNames.Count -or $incompleteRuns.Count -gt 0) {
        $finalError = "AutoAdv evidence collection could not complete ($($incompleteRuns.Count) runner/report failure(s)); see local/uploaded reports"
    }
}
if (-not $finalError) {
    Write-Host 'AutoAdv evidence collection completed for all requested phases; behavioral classification is in the offline verifier report.'
} else {
    Write-Warning $finalError
    Write-Warning "Persistent log: $transcriptPath"
}
if ($transcriptStarted) {
    try { Stop-Transcript | Out-Null }
    catch { Write-Warning "Validation transcript could not be closed cleanly: $($_.Exception.Message)" }
}
# The staged game is reproducible from the shared fixture cache and is not
# validation evidence. Keep logs/traces/reports only in the run directory.
Remove-Item -LiteralPath $runtime -Recurse -Force -ErrorAction SilentlyContinue
Remove-CompletedRunResidue (Join-Path $WorkBase 'runs') $session
if ($finalError) { throw $finalError }

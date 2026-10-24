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
function Invoke-GitHubIssueApi([string] $method, [string] $url, [string] $content = '') {
    Ensure-GitHubLogin
    $apiArgs = @('api','--method',$method,$url)
    $requestFile = $null
    try {
        if ($content) {
            $requestFile = Join-Path $session ('issue-request-' + [Guid]::NewGuid().ToString('N') + '.json')
            $json = ConvertTo-Json -InputObject @{ body = $content } -Compress -Depth 4
            [IO.File]::WriteAllText($requestFile, $json, ([Text.UTF8Encoding]::new($false)))
            $apiArgs += @('--input',$requestFile)
        }
        $response = @(& $script:GitHubCli @apiArgs)
        if ($LASTEXITCODE -ne 0) { throw "GitHub issue API failed ($method $url, exit code $LASTEXITCODE)" }
        return ($response -join "`n")
    } finally {
        if ($requestFile) { Remove-Item -LiteralPath $requestFile -Force -ErrorAction SilentlyContinue }
    }
}
function Set-IssueComment([long] $commentId, [string] $content) {
    # Normal validation never creates or repairs the fixed Issue surface.  It
    # can only PATCH a comment ID that the planner obtained from a complete,
    # unambiguous read of the existing Issue.
    $raw = Invoke-GitHubIssueApi 'PATCH' "repos/$repository/issues/comments/$commentId" $content
    $response = $raw | ConvertFrom-Json
    if (-not $response.html_url -or $response.html_url -notmatch '^https://github\.com/') {
        throw 'GitHub returned no valid issue comment URL'
    }
    Write-Host "Uploaded: $($response.html_url)"
}
function Get-IssueUploadPlan([string] $root, [string] $sha, [string] $pythonExecutable) {
    $commentsPath = Join-Path $session ('issue-comments-' + [Guid]::NewGuid().ToString('N') + '.json')
    try {
        Ensure-GitHubLogin
        # Keep GitHub's JSON opaque to Windows PowerShell 5.1.  Its array
        # unrolling semantics previously turned one comments page into a nested
        # System.Object[] and made marker absence indistinguishable from a parse
        # problem.  Python validates all paginated pages and every fixed marker.
        $apiArgs = @(
            'api', '--method', 'GET', '--paginate', '--slurp',
            "repos/$repository/issues/$IssueNumber/comments?per_page=100"
        )
        $responseLines = @(& $script:GitHubCli @apiArgs)
        if ($LASTEXITCODE -ne 0) {
            throw "GitHub issue API failed (GET comments, exit code $LASTEXITCODE)"
        }
        $raw = $responseLines -join "`n"
        if ([string]::IsNullOrWhiteSpace($raw)) {
            throw 'GitHub issue comments response was empty'
        }
        [IO.File]::WriteAllText($commentsPath, $raw, ([Text.UTF8Encoding]::new($false)))
        # issue_reporting is repository-local validation code, not part of the
        # installed autoadv package.  Upload runs after PATH/VIRTUAL_ENV are
        # restored, so execute it from the bootstrap checkout with the exact
        # managed-runtime Python prepared for this validation session.
        Push-Location $repo
        try {
            $planLines = @(& $pythonExecutable -m validation.real_game.issue_reporting `
                --root $root --sha $sha --comments $commentsPath)
            if ($LASTEXITCODE -ne 0) { throw "Issue report planner failed (exit code $LASTEXITCODE)" }
        } finally { Pop-Location }
        $planRaw = $planLines -join "`n"
        if ([string]::IsNullOrWhiteSpace($planRaw)) { throw 'Issue report planner returned no plan' }
        return ($planRaw | ConvertFrom-Json)
    } finally {
        Remove-Item -LiteralPath $commentsPath -Force -ErrorAction SilentlyContinue
    }
}
function Upload-Result([string] $root, [string] $sha, [string] $pythonExecutable) {
    # The Issue already owns four history comments and one baseline comment.
    # A normal run performs one history PATCH plus, only for an eligible
    # accuracy baseline, one baseline PATCH. Missing/duplicate/unreadable
    # markers fail closed and leave the Issue untouched.
    $plan = Get-IssueUploadPlan $root $sha $pythonExecutable
    foreach ($operation in @($plan.operations)) {
        [void](Set-IssueComment ([long]$operation.comment_id) ([string]$operation.body))
        if ($operation.role -eq 'history') {
            Write-Host "Validation history slot $($operation.slot) updated for run $($plan.run)"
        } elseif ($operation.role -eq 'baseline') {
            Write-Host "Validation baseline updated for run $($plan.run)"
        }
    }
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
        # A summary is written only after the measurement process has released
        # its Python/game runtime. Preserve all evidence files and remove only
        # reproducible legacy environments from completed runs.
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
                # Only fixture media remains measurement-specific. Product alignment model,
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
                $arguments = @(
                    '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
                    (Join-Path $repo 'validation\real_game\run_live_check.ps1'),
                    '-Phase',$currentPhase,
                    '-GameExecutable',$gameExecutable,
                    '-Automated','-MeasurementOnly','-Trace',$trace,'-Report',$report,
                    '-TimeoutSeconds','360'
                )
                & powershell.exe @arguments
                if ($LASTEXITCODE -ne 0) { throw "run_live_check.ps1 exited with $LASTEXITCODE" }
            } catch {
                $phaseError = [string]$_.Exception.Message
                Write-Warning "Phase ${currentPhase}: $phaseError"
            } finally { Pop-Location }
        }

        $measurement = $null
        if (Test-Path -LiteralPath $report -PathType Leaf) {
            try { $measurement = Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json }
            catch { $phaseError = "Verification report cannot be parsed: $($_.Exception.Message)" }
        }
        $results.Add([pscustomobject]@{
            phase = $currentPhase
            ok = if ($measurement) { $measurement.ok } else { $false }
            collection_valid = if ($measurement) { $measurement.collection_valid } else { $null }
            measurement_valid = if ($measurement) { $measurement.measurement_valid } else { $null }
            collection_complete = if ($measurement) { $measurement.collection_complete } else { $null }
            advancement_observed = if ($measurement) { $measurement.advancement_observed } else { $null }
            automatic_continuation_verified = if ($measurement) { $measurement.automatic_continuation_verified } else { $null }
            validation_recovery_inputs = if ($measurement) { $measurement.validation_recovery_inputs } else { $null }
            voice_safety_verified = if ($measurement) { $measurement.voice_safety_verified } else { $null }
            quality = if ($measurement) { $measurement.quality } else { $null }
            quality_issues = if ($measurement) { @($measurement.quality_issues) } else { @() }
            report_present = [bool]$measurement
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
    schema = 'autoadv-local-validation-v3'
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
    $unmeasurable = @($results.ToArray() | Where-Object {
        $_.runner_error -or $_.report_present -ne $true -or
        $_.measurement_valid -ne $true -or $_.collection_complete -ne $true
    })
    if ($results.Count -ne $phaseNames.Count -or $unmeasurable.Count -gt 0) {
        $finalError = "AutoAdv measurement could not complete ($($unmeasurable.Count) invalid/incomplete phase(s)); see local/uploaded reports"
    }
}
if (-not $finalError) {
    Write-Host 'AutoAdv measurement completed for all requested phases. Product accuracy and safety remain in the reported metrics.'
} else {
    Write-Warning $finalError
    Write-Warning "Persistent log: $transcriptPath"
}
if ($transcriptStarted) {
    try { Stop-Transcript | Out-Null }
    catch { Write-Warning "Validation transcript could not be closed cleanly: $($_.Exception.Message)" }
}
# The staged game is reproducible from the shared fixture cache and is not
# measurement evidence. Keep logs/traces/reports only in the run directory.
Remove-Item -LiteralPath $runtime -Recurse -Force -ErrorAction SilentlyContinue
Remove-CompletedRunResidue (Join-Path $WorkBase 'runs') $session
if ($finalError) { throw $finalError }

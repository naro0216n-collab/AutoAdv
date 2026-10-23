# AutoAdv interactive Windows validation orchestrator.
# This file is the Git-owned implementation used by the thin .cmd bootstrap.
# Keep validation mechanics here so an existing bootstrap follows develop automatically.
[CmdletBinding()]
param(
    [ValidateSet('all', 'reading', 'native')]
    [string] $Phase = 'all',
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
$venv = Join-Path $session 'venv'
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
$phaseNames = if ($Phase -eq 'all') { @('reading', 'native') } else { @($Phase) }
$startedUtc = [DateTime]::UtcNow.ToString('o')
$gitSha = $null
New-Item -ItemType Directory -Force -Path $session, $cache | Out-Null

function Assert-Command([string] $name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "$name is required. Install it before running this validation."
    }
}
function Invoke-Checked([string] $file, [string[]] $commandArgs) {
    & $file @commandArgs
    if ($LASTEXITCODE -ne 0) { throw "$file failed (exit code $LASTEXITCODE)" }
}
function Sanitize-For-PublicIssue([string] $content) {
    $content = [regex]::Replace($content, '(?i)[A-Z]:(?:\\){1,2}Users(?:\\){1,2}[^\\/\s\"'']+', '<USERPROFILE>')
    $content = [regex]::Replace($content, '(?i)(gh[pousr]_[a-z0-9_]{12,}|github_pat_[a-z0-9_]{12,})', '<REDACTED-TOKEN>')
    return $content
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
function Post-Comment([string] $content) {
    Ensure-GitHubLogin
    $requestFile = Join-Path $session ('issue-request-' + [Guid]::NewGuid().ToString('N') + '.json')
    try {
        $json = ConvertTo-Json -InputObject @{ body = $content } -Compress -Depth 4
        [IO.File]::WriteAllText($requestFile, $json, ([Text.UTF8Encoding]::new($false)))
        $url = "repos/$repository/issues/$IssueNumber/comments"
        $response = @(& $script:GitHubCli api --method POST $url --input $requestFile --jq '.html_url')
        if ($LASTEXITCODE -ne 0) { throw "GitHub issue upload failed (exit code $LASTEXITCODE)" }
        $commentUrl = [string]($response | Select-Object -Last 1)
        if ($commentUrl -notmatch '^https://github\.com/') { throw 'GitHub returned no valid issue comment URL' }
        Write-Host "Uploaded: $commentUrl"
    } finally {
        Remove-Item -LiteralPath $requestFile -Force -ErrorAction SilentlyContinue
    }
}
function Upload-Result([string] $root, [string] $sha) {
    $files = @(
        Get-ChildItem -LiteralPath $root -File | Where-Object {
            $_.Name -eq 'validation-summary.json' -or
            $_.Name -match '^autoadv-(reading|native)(\.result)?\.jsonl?$' -or
            $_.Name -match '^autoadv-game-[a-z0-9-]+\.jsonl$' -or
            $_.Name -match '^autoadv-native-[a-z0-9-]+\.jsonl$'
        } | Sort-Object Name
    )
    if ($files.Count -eq 0) { throw 'No verification summary or trace exists to upload' }
    $prefix = "<!-- autoadv-local-validation-v1 -->`nAutoAdv local validation | commit ``$sha`` | run ``$(Split-Path $root -Leaf)```n`n"
    $payload = $prefix
    $maxLength = 45000
    foreach ($file in $files) {
        $safe = Sanitize-For-PublicIssue ([IO.File]::ReadAllText($file.FullName, [Text.Encoding]::UTF8))
        $chunkSize = 28000
        $parts = [Math]::Max(1, [int][Math]::Ceiling($safe.Length / [double]$chunkSize))
        $language = if ($file.Extension -eq '.json') { 'json' } else { 'jsonl' }
        for ($i = 0; $i -lt $parts; $i++) {
            $offset = $i * $chunkSize
            $slice = $safe.Substring($offset, [Math]::Min($chunkSize, $safe.Length - $offset))
            $label = "### ``$($file.Name)``" + $(if ($parts -gt 1) { " (part $($i + 1)/$parts)" } else { '' })
            $block = "$label`n``````$language`n$slice`n```````n`n"
            if ($payload.Length + $block.Length -gt $maxLength) {
                if ($payload -eq $prefix) { throw 'Single result fragment exceeds GitHub comment limit' }
                Post-Comment $payload
                $payload = $prefix
            }
            $payload += $block
        }
    }
    if ($payload -ne $prefix) { Post-Comment $payload }
}
function Get-PrebuiltFixture([string] $destination) {
    $digestLines = @(& python -m validation.real_game.fixture_runtime source-digest)
    if ($LASTEXITCODE -ne 0 -or $digestLines.Count -ne 1) {
        throw 'Cannot calculate validation fixture source digest'
    }
    $digest = ([string]$digestLines[0]).Trim()
    if ($digest -notmatch '^[a-f0-9]{64}$') { throw 'Invalid validation fixture source digest' }
    $asset = "autoadv-validation-game-$digest.zip"
    $zip = Join-Path $cache $asset
    if (-not (Test-Path -LiteralPath $zip -PathType Leaf)) {
        $url = "https://github.com/$repository/releases/download/autoadv-validation-fixtures/$asset"
        Write-Host "Downloading prebuilt validation game $asset"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    }
    if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
    Expand-Archive -LiteralPath $zip -DestinationPath $destination -Force
    $gameLines = @(& python -m validation.real_game.fixture_runtime verify --runtime $destination)
    if ($LASTEXITCODE -ne 0 -or $gameLines.Count -ne 1) {
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
        throw 'Prebuilt validation game verification failed'
    }
    return ([string]$gameLines[0]).Trim()
}

try {
    $stage = 'prerequisites'
    Assert-Command 'git'
    Assert-Command 'py'
    Assert-Command 'dotnet'
    $versionLines = @(& py -3.11 -c 'import sys;print(sys.version_info.major*100+sys.version_info.minor)')
    if ($LASTEXITCODE -ne 0 -or $versionLines.Count -ne 1 -or ([string]$versionLines[0]).Trim() -ne '311') {
        throw 'Python 3.11 is required (py -3.11)'
    }
    $dotnetSdks = & dotnet --list-sdks
    if ($LASTEXITCODE -ne 0 -or -not @($dotnetSdks | Where-Object { $_ -match '^9\.' }).Count) {
        throw '.NET 9 SDK is required for AutoAdv.ProcessLoopback'
    }
    $gitShaLines = @(& git -C $repo rev-parse HEAD)
    if ($LASTEXITCODE -ne 0 -or $gitShaLines.Count -ne 1) { throw 'Cannot read validation checkout commit' }
    $gitSha = ([string]$gitShaLines[0]).Trim()
    if ($gitSha -notmatch '^[a-f0-9]{40}$') { throw 'Cannot pin the validation checkout commit' }

    $stage = 'dependencies'
    Invoke-Checked 'py' @('-3.11','-m','venv',$venv)
    $env:VIRTUAL_ENV = $venv
    $env:PATH = (Join-Path $venv 'Scripts') + ';' + $oldPath
    Push-Location $repo
    try {
        Invoke-Checked 'python' @('-m','pip','install','--disable-pip-version-check','.[gui,native]')
    } finally { Pop-Location }
    $env:RUNNER_TEMP = $session

    $stage = 'fixture'
    Push-Location $repo
    try { $gameExecutable = Get-PrebuiltFixture $runtime }
    finally { Pop-Location }

    $stage = 'native-runtime'
    Push-Location $repo
    try {
        Invoke-Checked 'dotnet' @('build','integrations/windows-loopback/AutoAdv.ProcessLoopback.csproj','--configuration','Release','--nologo')
        $helper = (Resolve-Path 'integrations/windows-loopback/bin/Release/net9.0-windows/AutoAdv.ProcessLoopback.dll').Path
        $modelRoot = Join-Path $cache 'asr'
        Invoke-Checked 'python' @('-m','validation.real_game.prepare_asr_model','--directory',$modelRoot)
        $asrModel = Join-Path $modelRoot 'model.int8.onnx'
        $asrTokens = Join-Path $modelRoot 'tokens.txt'
    } finally { Pop-Location }

    foreach ($currentPhase in $phaseNames) {
        if ($currentPhase -eq 'native') {
            $stage = 'validate-native-matrix'
            Push-Location $repo
            try {
                $caseLines = @(& python -m validation.real_game.case_matrix)
                if ($LASTEXITCODE -ne 0 -or $caseLines.Count -ne 1) {
                    throw 'Cannot enumerate the RealAudio case matrix'
                }
                $nativeCases = @(([string]$caseLines[0] | ConvertFrom-Json))
            } finally { Pop-Location }
            if ($nativeCases.Count -eq 0) { throw 'RealAudio case matrix is empty' }

            $caseResults = New-Object 'System.Collections.Generic.List[object]'
            foreach ($nativeCase in $nativeCases) {
                $caseId = [string]$nativeCase.id
                $caseStartLine = [int]$nativeCase.start_line
                if ($caseId -notmatch '^[a-z0-9-]+$' -or $caseStartLine -lt 1) {
                    throw "Invalid RealAudio case descriptor: $caseId / $caseStartLine"
                }
                $stage = "validate-native-$caseId"
                $trace = Join-Path $session "autoadv-native-$caseId.jsonl"
                $caseReport = Join-Path $session "autoadv-native-$caseId.result.json"
                $oracle = Join-Path $session "autoadv-game-$caseId.jsonl"
                $caseError = $null
                Push-Location $repo
                try {
                    $arguments = @(
                        '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
                        (Join-Path $repo 'validation\real_game\run_live_check.ps1'),
                        '-Phase','native', '-NativeCase',$caseId, '-StartLine',[string]$caseStartLine,
                        '-GameExecutable',$gameExecutable, '-Automated',
                        '-Trace',$trace, '-Oracle',$oracle, '-Report',$caseReport,
                        '-TimeoutSeconds','90',
                        '-ProcessLoopbackHelper',$helper,'-AsrModel',$asrModel,'-AsrTokens',$asrTokens
                    )
                    & powershell.exe @arguments
                    if ($LASTEXITCODE -ne 0) { throw "run_live_check.ps1 exited with $LASTEXITCODE" }
                } catch {
                    # A failed ASR attribution/completion is a case result, not
                    # permission to skip the remaining independent cases.
                    $caseError = [string]$_.Exception.Message
                    Write-Warning "Native case ${caseId}: $caseError"
                } finally { Pop-Location }

                $measurement = $null
                if (Test-Path -LiteralPath $caseReport -PathType Leaf) {
                    try { $measurement = Get-Content -LiteralPath $caseReport -Raw -Encoding UTF8 | ConvertFrom-Json }
                    catch { $caseError = "Verification report cannot be parsed: $($_.Exception.Message)" }
                }
                $caseResults.Add([pscustomobject]@{
                    id = $caseId
                    start_line = $caseStartLine
                    report_present = [bool]$measurement
                    runner_error = $caseError
                    ok = if ($measurement) { $measurement.ok } else { $false }
                    collection_valid = if ($measurement) { $measurement.collection_valid } else { $null }
                    advancement_observed = if ($measurement) { $measurement.advancement_observed } else { $null }
                    voice_safety_verified = if ($measurement) { $measurement.voice_safety_verified } else { $null }
                    measurement = $measurement
                })
            }

            $caseArray = @($caseResults.ToArray())
            $missingReports = @($caseArray | Where-Object { -not $_.report_present })
            $failedCases = @($caseArray | Where-Object { $_.ok -ne $true })
            $nativeAggregate = [ordered]@{
                schema = 'autoadv-realaudio-matrix-v1'
                phase = 'native'
                ok = ($failedCases.Count -eq 0 -and $caseArray.Count -eq $nativeCases.Count)
                case_count = $nativeCases.Count
                completed_case_count = @($caseArray | Where-Object { $_.report_present }).Count
                passing_cases = @($caseArray | Where-Object { $_.ok -eq $true } | ForEach-Object { $_.id })
                failing_cases = @($failedCases | ForEach-Object { $_.id })
                collection_valid = ($missingReports.Count -eq 0 -and
                    @($caseArray | Where-Object { $_.collection_valid -ne $true }).Count -eq 0)
                advancement_observed = ($missingReports.Count -eq 0 -and
                    @($caseArray | Where-Object { $_.advancement_observed -ne $true }).Count -eq 0)
                voice_safety_verified = ($missingReports.Count -eq 0 -and
                    @($caseArray | Where-Object { $_.voice_safety_verified -ne $true }).Count -eq 0)
                cases = $caseArray
            }
            $aggregateReport = Join-Path $session 'autoadv-native.result.json'
            [IO.File]::WriteAllText(
                $aggregateReport,
                (ConvertTo-Json -InputObject $nativeAggregate -Depth 20),
                [Text.UTF8Encoding]::new($false))
            $results.Add([pscustomobject]@{
                phase = 'native'
                collection_valid = $nativeAggregate.collection_valid
                advancement_observed = $nativeAggregate.advancement_observed
                voice_safety_verified = $nativeAggregate.voice_safety_verified
                report_present = $true
                runner_error = if ($missingReports.Count) {
                    "$($missingReports.Count) RealAudio case(s) produced no verification report"
                } else { $null }
            })
            continue
        }

        $stage = "validate-$currentPhase"
        $trace = Join-Path $session "autoadv-$currentPhase.jsonl"
        $report = Join-Path $session "autoadv-$currentPhase.result.json"
        $phaseError = $null
        Push-Location $repo
        try {
            $arguments = @(
                '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
                (Join-Path $repo 'validation\real_game\run_live_check.ps1'),
                '-Phase',$currentPhase,
                '-GameExecutable',$gameExecutable,
                '-Automated','-Trace',$trace,'-Report',$report,
                '-TimeoutSeconds','120'
            )
            & powershell.exe @arguments
            if ($LASTEXITCODE -ne 0) { throw "run_live_check.ps1 exited with $LASTEXITCODE" }
        } catch {
            $phaseError = [string]$_.Exception.Message
            Write-Warning "Phase ${currentPhase}: $phaseError"
        } finally { Pop-Location }
        $measurement = $null
        if (Test-Path -LiteralPath $report -PathType Leaf) {
            try { $measurement = Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json }
            catch { $phaseError = "Verification report cannot be parsed: $($_.Exception.Message)" }
        }
        $results.Add([pscustomobject]@{
            phase = $currentPhase
            collection_valid = if ($measurement) { $measurement.collection_valid } else { $null }
            advancement_observed = if ($measurement) { $measurement.advancement_observed } else { $null }
            voice_safety_verified = if ($measurement) { $measurement.voice_safety_verified } else { $null }
            report_present = [bool]$measurement
            runner_error = $phaseError
        })
        if ($currentPhase -eq 'reading' -and $Phase -eq 'all' -and
                (-not $measurement -or $measurement.collection_valid -ne $true)) {
            Write-Warning 'Native phase skipped: reading phase did not obtain a real dialogue Hook.'
            $results.Add([pscustomobject]@{
                phase = 'native'; collection_valid = $false
                advancement_observed = $false; voice_safety_verified = $null
                report_present = $false
                runner_error = 'skipped_after_reading_hook_failure'
            })
            break
        }
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
    schema = 'autoadv-local-validation-v2'
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
    try { Upload-Result $session $gitSha }
    catch {
        $uploadError = [string]$_.Exception.Message
        Write-Warning "GitHub upload failed: $uploadError"
        Write-Warning "Local results are preserved at $session"
    }
} elseif (-not $NoUpload -and -not $hasProductRun) {
    Write-Warning 'Upload skipped: product validation never started. The local summary remains available.'
}
if ($uploadError) { throw "AutoAdv result upload failed: $uploadError" }
if ($errorText) { throw "AutoAdv validation setup failed: $errorText" }
$unsuccessful = @($results.ToArray() | Where-Object {
    $_.runner_error -or $_.collection_valid -ne $true -or
    $_.advancement_observed -ne $true -or
    ($_.phase -eq 'native' -and $_.voice_safety_verified -ne $true)
})
if ($results.Count -ne $phaseNames.Count -or $unsuccessful.Count -gt 0) {
    throw "AutoAdv validation did NOT pass ($($unsuccessful.Count) unsuccessful phase(s)); see local/uploaded reports"
}
Write-Host 'AutoAdv validation passed for all requested phases.'

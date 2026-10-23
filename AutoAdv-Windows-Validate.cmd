@echo off
setlocal EnableExtensions DisableDelayedExpansion
title AutoAdv Windows Validation
set "AUTOADV_BOOTSTRAP=%~f0"
set "AUTOADV_BASE=%~dp0.autoadv-validation"
if not exist "%AUTOADV_BASE%" mkdir "%AUTOADV_BASE%" >nul 2>&1
set "AUTOADV_BOOTSTRAP_LOG=%AUTOADV_BASE%\bootstrap-latest.log"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $repository='naro0216n-collab/AutoAdv'; $base=$env:AUTOADV_BASE; $log=$env:AUTOADV_BOOTSTRAP_LOG; $transcript=$false; $result=1; try { try { Start-Transcript -Path $log -Force | Out-Null; $transcript=$true } catch { Write-Warning ('Bootstrap transcript unavailable: '+$_.Exception.Message) }; $stamp=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'); $checkout=Join-Path $base ('bootstrap\'+$stamp+'-'+[Guid]::NewGuid().ToString('N').Substring(0,8)); New-Item -ItemType Directory -Force -Path (Split-Path -Parent $checkout) | Out-Null; if(-not (Get-Command git.exe -ErrorAction SilentlyContinue)){throw 'Git is required'}; & git.exe -c credential.helper= clone --depth 1 --single-branch --branch develop ('https://github.com/'+$repository+'.git') $checkout; if($LASTEXITCODE -ne 0){throw 'Cannot retrieve latest AutoAdv develop'}; $entry=Join-Path $checkout 'validation\real_game\run_windows_validation.ps1'; if(-not (Test-Path -LiteralPath $entry -PathType Leaf)){throw 'Git validation entrypoint is missing'}; & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $entry -WorkBase $base; $result=$LASTEXITCODE } catch { Write-Error $_; $result=1 } finally { if($transcript){ try { Stop-Transcript | Out-Null } catch {} } }; exit $result"
set "AUTOADV_RESULT=%ERRORLEVEL%"
echo.
echo Persistent bootstrap log: "%AUTOADV_BOOTSTRAP_LOG%"
if exist "%AUTOADV_BASE%\last-run.txt" (
    echo Latest validation session:
    type "%AUTOADV_BASE%\last-run.txt"
    echo.
)
if "%AUTOADV_RESULT%"=="0" (
    echo Validation complete.
    timeout /t 5 /nobreak >nul
) else (
    echo AutoAdv validation did not complete successfully.
    echo The persistent logs above remain available even if this window closes.
    echo Press any key to close this window.
    pause >nul
)
exit /b %AUTOADV_RESULT%

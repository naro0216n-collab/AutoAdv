@echo off
setlocal EnableExtensions DisableDelayedExpansion
title AutoAdv Windows Validation
set "AUTOADV_BOOTSTRAP=%~f0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $repository='naro0216n-collab/AutoAdv'; $base=Join-Path (Split-Path -Parent $env:AUTOADV_BOOTSTRAP) '.autoadv-validation'; $stamp=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'); $checkout=Join-Path $base ('bootstrap\'+$stamp+'-'+[Guid]::NewGuid().ToString('N').Substring(0,8)); New-Item -ItemType Directory -Force -Path (Split-Path -Parent $checkout) | Out-Null; if(-not (Get-Command git.exe -ErrorAction SilentlyContinue)){throw 'Git is required'}; & git.exe -c credential.helper= clone --depth 1 --single-branch --branch develop ('https://github.com/'+$repository+'.git') $checkout; if($LASTEXITCODE -ne 0){throw 'Cannot retrieve latest AutoAdv develop'}; $entry=Join-Path $checkout 'validation\real_game\run_windows_validation.ps1'; if(-not (Test-Path -LiteralPath $entry -PathType Leaf)){throw 'Git validation entrypoint is missing'}; & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $entry; exit $LASTEXITCODE"
set "AUTOADV_RESULT=%ERRORLEVEL%"
echo.
if "%AUTOADV_RESULT%"=="0" (
    echo Validation complete.
    timeout /t 5 /nobreak >nul
) else (
    echo AutoAdv validation did not complete successfully.
    echo Press any key to close this window.
    pause >nul
)
exit /b %AUTOADV_RESULT%

@echo off
chcp 65001 > nul
setlocal

rem Edit this value for the normal double-click build.
set "SERVERLESS_SUFFIX=1"

rem An optional first argument overrides the value above:
rem     build_serverless.bat 2
if not "%~1"=="" set "SERVERLESS_SUFFIX=%~1"

set "BAKE_FILE=serverless/docker-bake.hcl"
set "ON_ERROR_PAUSE=1"

pushd "%~dp0"

echo =====================================
echo ComfyUI RunPod Serverless build + push
echo Suffix: %SERVERLESS_SUFFIX%
echo Targets: serverless-cu128 serverless-cu130
echo Mode: --push (no local --load)
echo =====================================
echo.

docker buildx bake -f "%BAKE_FILE%" serverless-cu128 serverless-cu130 --push
if errorlevel 1 goto :fail

echo.
echo Build completed and both images were pushed.
powershell -NoProfile -Command "[console]::beep(784,180); [console]::beep(988,180); [console]::beep(1319,400)"
popd
pause
exit /b 0

:fail
set "BUILD_EXIT_CODE=%ERRORLEVEL%"
echo.
echo ERROR: Serverless build failed with exit code %BUILD_EXIT_CODE%
popd
if "%ON_ERROR_PAUSE%"=="1" pause
exit /b %BUILD_EXIT_CODE%

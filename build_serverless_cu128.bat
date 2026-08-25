@echo off
chcp 65001 > nul
setlocal

rem Build and push only the CUDA 12.8 / Python 3.12 serverless image.
rem The image is pushed directly and is not loaded into the local Docker store.
set "SERVERLESS_SUFFIX=2"

rem An optional first argument overrides the value above:
rem     build_serverless_cu128.bat 2
if not "%~1"=="" set "SERVERLESS_SUFFIX=%~1"

set "BAKE_FILE=serverless/docker-bake.hcl"
set "TARGET=serverless-cu128"
set "ON_ERROR_PAUSE=1"

pushd "%~dp0"

echo =====================================
echo ComfyUI RunPod Serverless CU128 build + push
echo Suffix: %SERVERLESS_SUFFIX%
echo Target: %TARGET%
echo Mode: --push (no local --load)
echo =====================================
echo.

docker buildx bake -f "%BAKE_FILE%" "%TARGET%" --push
if errorlevel 1 goto :fail

echo.
echo Build completed and pushed: %TARGET%
powershell -NoProfile -Command "[console]::beep(784,180); [console]::beep(988,180); [console]::beep(1319,400)"
popd
pause
exit /b 0

:fail
set "BUILD_EXIT_CODE=%ERRORLEVEL%"
echo.
echo ERROR: Serverless CU128 build failed with exit code %BUILD_EXIT_CODE%
popd
if "%ON_ERROR_PAUSE%"=="1" pause
exit /b %BUILD_EXIT_CODE%

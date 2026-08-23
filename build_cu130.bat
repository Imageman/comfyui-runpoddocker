@echo off
chcp 65001 > nul
setlocal

rem Isolated CUDA 13 / Python 3.13 / SageAttention 2+3 build and push.
rem The image is pushed directly and is not loaded into the local Docker store.

set "ON_ERROR_PAUSE=1"
set "BAKE_FILE=cu130-sage23/docker-bake.hcl"
set "TARGET=cu130-py313-sage23"
set "RELEASE_SUFFIX="

pushd "%~dp0"

echo =====================================
echo CUDA 13 build
echo Target: %TARGET%
echo Release suffix: %RELEASE_SUFFIX%
echo Mode:   --push (no local --load)
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
echo ERROR: CUDA 13 build failed with exit code %BUILD_EXIT_CODE%
popd
if "%ON_ERROR_PAUSE%"=="1" pause
exit /b %BUILD_EXIT_CODE%

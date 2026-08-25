@echo off
chcp 65001 > nul
setlocal

pushd "%~dp0"

echo =====================================
echo MiniMax H3 RunPod Serverless test
echo Config: test_config.json
echo =====================================
echo.

uv run --with-requirements "..\requirements.txt" python "test_runpod.py"
set "TEST_EXIT_CODE=%ERRORLEVEL%"

echo.
if "%TEST_EXIT_CODE%"=="0" (
    echo Test completed successfully.
) else (
    echo ERROR: Test failed with exit code %TEST_EXIT_CODE%
)

popd
pause
exit /b %TEST_EXIT_CODE%

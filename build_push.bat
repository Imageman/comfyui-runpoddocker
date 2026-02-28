@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

rem Если запуск двойным кликом — окно закроется. Поэтому на ошибке делаем pause.
set "ON_ERROR_PAUSE=1"

rem === настройки образа ===
set "IMAGE_NAME=realizedfantasy/comfyui-runpoddocker"
set "TAG=2026-02-12"

rem build-context 
rem set "DROOT_CTX=D:\sftp-root\home\realizedfantasy\mnt_storage"

echo =====================================
echo Build + Push:
echo   %IMAGE_NAME%:%TAG%
echo   %IMAGE_NAME%:latest
echo Build context (droot): %DROOT_CTX%
echo =====================================

echo.
echo [*] Building and pushing (no local --load)...
docker buildx bake -f docker-bake.hcl cu124-py311 ^
  --push 
if errorlevel 1 goto :fail

echo.
echo ✅ Done: pushed %IMAGE_NAME%:%TAG% and %IMAGE_NAME%:latest

echo OK
pause

rem опционально: чистка кэша билдера
echo [*] Pruning buildx cache (optional)...
docker buildx prune --builder fsbuilder --reserved-space 80GB -f
if errorlevel 1 goto :fail


goto :eof

:fail
echo.
echo [!] ERROR: step failed with exit code %errorlevel%
if "%ON_ERROR_PAUSE%"=="1" pause
exit /b %errorlevel%

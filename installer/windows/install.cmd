@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
if "%NO_PAUSE%"=="1" goto :end
echo.
if "%EXIT_CODE%"=="0" (
	echo Installation finished successfully.
) else (
	echo Installation failed with exit code %EXIT_CODE%.
)
pause
:end
exit /b %EXIT_CODE%

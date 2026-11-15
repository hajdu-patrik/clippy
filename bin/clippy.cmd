@echo off
setlocal
set "CLIPPY_ROOT=%~dp0.."
set "PYTHONPATH=%CLIPPY_ROOT%;%PYTHONPATH%"
"%CLIPPY_ROOT%\.venv\Scripts\python.exe" -m clippy %*
endlocal & exit /b %ERRORLEVEL%

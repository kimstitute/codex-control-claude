@echo off
setlocal
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "%~dp0claude_control_cli.py" %*
) else (
  python "%~dp0claude_control_cli.py" %*
)
exit /b %ERRORLEVEL%

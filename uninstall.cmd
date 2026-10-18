@echo off
rem Removes the native host of the "Add site to Throne" extension. Config backups are kept;
rem to delete them too:  uninstall.cmd -RemoveBackups
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
set RC=%ERRORLEVEL%
if not defined NOPAUSE pause
exit /b %RC%

@echo off
rem Installs the native host for the "Add site to Throne" extension (current user, no admin rights).
rem Extra arguments go to install.ps1, e.g.:  install.cmd -ThroneDir "W:\stop car\Throne"
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%
if not defined NOPAUSE pause
exit /b %RC%

@echo off
chcp 65001 >nul
call "%~dp0start.cmd" %*
exit /b %errorlevel%

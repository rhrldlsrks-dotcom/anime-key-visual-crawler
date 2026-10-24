@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0"
if errorlevel 1 goto folder_error

set "TMDB_STARTUP_LOG=%~dp0startup.log"
set "TMDB_PYTHON="
>"%TMDB_STARTUP_LOG%" echo TMDB Poster Downloader - startup

if exist "%~dp0.venv\Scripts\python.exe" set "TMDB_PYTHON=%~dp0.venv\Scripts\python.exe"
if defined TMDB_PYTHON goto python_found

for /d %%D in ("%LocalAppData%\Programs\Python\Python*") do if exist "%%~fD\python.exe" set "TMDB_PYTHON=%%~fD\python.exe"
if defined TMDB_PYTHON goto python_found

for /d %%D in ("%LocalAppData%\Python\pythoncore-*") do if exist "%%~fD\python.exe" set "TMDB_PYTHON=%%~fD\python.exe"
if defined TMDB_PYTHON goto python_found

for /f "delims=" %%P in ('where.exe python.exe 2^>nul') do if not defined TMDB_PYTHON set "TMDB_PYTHON=%%P"
if not defined TMDB_PYTHON goto python_missing

:python_found
echo Python: "%TMDB_PYTHON%"
>>"%TMDB_STARTUP_LOG%" echo Python: "%TMDB_PYTHON%"
"%TMDB_PYTHON%" -c "import sys, tkinter; assert sys.version_info >= (3, 10), 'Python 3.10 or newer is required'" >>"%TMDB_STARTUP_LOG%" 2>&1
if errorlevel 1 goto failed

"%TMDB_PYTHON%" -c "import PIL" >>"%TMDB_STARTUP_LOG%" 2>&1
if not errorlevel 1 goto dependencies_ready
echo Installing image preview library...
"%TMDB_PYTHON%" -m pip install -r "%~dp0requirements.txt" >>"%TMDB_STARTUP_LOG%" 2>&1
if errorlevel 1 goto failed

:dependencies_ready
echo Starting TMDB Poster Downloader...
"%TMDB_PYTHON%" -X utf8 "%~dp0main.py" %* >>"%TMDB_STARTUP_LOG%" 2>&1
if errorlevel 1 goto failed
popd
exit /b 0

:python_missing
>>"%TMDB_STARTUP_LOG%" echo Python was not found. Install Python 3.10 or newer, then try again.
goto failed

:failed
echo.
echo Startup failed. Details are saved in startup.log:
echo.
type "%TMDB_STARTUP_LOG%"
echo.
pause
popd
exit /b 1

:folder_error
echo Could not open the program folder.
pause
exit /b 1

@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ================================================================
rem  Cross-Harness Agent Team Orchestrator - one-click launcher
rem  Double-click to start the web console and open the browser.
rem
rem  Usage:
rem    start.cmd              start web console + open browser (default)
rem    start.cmd console      same as above
rem    start.cmd serve <run-id> <team-file>   start scheduler window
rem                                         (e.g. run-1, config\team.yaml)
rem    start.cmd check        print environment status, start nothing
rem    start.cmd help         show this help
rem ================================================================

set "MODE=%~1"
if "%MODE%"=="" set "MODE=console"
if /i "%MODE%"=="-h" set "MODE=help"
if /i "%MODE%"=="/?" set "MODE=help"
if /i "%MODE%"=="--help" set "MODE=help"

set "PORT=8080"
set "URL=http://127.0.0.1:%PORT%"

echo.
echo  ============================================================
echo   Cross-Harness Agent Team Orchestrator - launcher
echo   mode: %MODE%     root: %CD%
echo  ============================================================
echo.

rem ------- help ---------------------------------------------------
if /i "%MODE%"=="help" goto :help

rem ------- locate a python interpreter ----------------------------
set "PY=py -3"
where py >nul 2>nul
if errorlevel 1 set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
    echo [error] python not found. Install Python 3.10+ and re-run.
    goto :fail
)

rem ------- ensure venv + install (first run only) ------------------
if not exist ".venv\Scripts\python.exe" (
    echo [orchestrator] first run: bootstrapping venv and dependencies...
    echo [orchestrator] this may take several minutes.
    %PY% scripts\bootstrap.py --root .
    if errorlevel 1 (
        echo [error] bootstrap failed. Check network and retry.
        goto :fail
    )
    echo [orchestrator] environment ready.
)

set "VPY=.venv\Scripts\python.exe"
if not exist "%VPY%" (
    echo [error] venv python missing: %VPY%
    goto :fail
)

rem ------- check mode ----------------------------------------------
if /i "%MODE%"=="check" goto :check

rem ------- codebuddy china-station env (inherited by children) -----
set "CODEBUDDY_SKIP_GIT_BASH_CHECK=1"
set "CODEBUDDY_INTERNET_ENVIRONMENT=internal"

rem ------- serve mode ----------------------------------------------
if /i "%MODE%"=="serve" (
    set "RUN_ID=%~2"
    set "TEAM_FILE=%~3"
    if "%RUN_ID%"=="" goto :help
    if "%TEAM_FILE%"=="" goto :help
    if not exist "%TEAM_FILE%" (
        echo [error] team file not found: %TEAM_FILE%
        goto :fail
    )
    echo [orchestrator] starting scheduler for run "%RUN_ID%" with team "%TEAM_FILE%"
    echo [orchestrator] close the scheduler window to stop. Console UI can also
    echo                 start/stop schedulers from the Runs tab.
    start "orchestrator-serve-%RUN_ID%" cmd /k ""%VPY%" -m orchestrator serve-team --run "%RUN_ID%" --team "%TEAM_FILE%" --db .agent-hub\state\agent-hub.db"
    goto :done
)

rem ------- default: console mode -----------------------------------
if /i not "%MODE%"=="console" (
    echo [error] unknown mode: %MODE%
    goto :help
)

echo [orchestrator] picking a free port (starting at %PORT%) ...
:pick_port
powershell -NoProfile -Command "$l=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,%PORT%); try{$l.Start();$l.Stop();exit 0}catch{exit 1}" >nul 2>&1
if errorlevel 1 (
    set /a PORT+=1
    if %PORT% gtr 8099 (
        echo [error] no free port found between 8080 and 8099. Close something and retry.
        goto :fail
    )
    goto :pick_port
)
set "URL=http://127.0.0.1:%PORT%"

echo [orchestrator] starting web console on %URL% ...
start "orchestrator-console (port %PORT%, close to stop)" cmd /k ""%VPY%" -m orchestrator console --port %PORT%"
timeout /t 3 /nobreak >nul

:open_browser
start "" %URL%
echo [orchestrator] browser opened: %URL%
echo [orchestrator] if nothing opened, visit %URL% manually.
echo [orchestrator] note: if a page shows Steam/Inspectable Web Contents, that
echo                 means 8080 is taken by Steam - the console window above
echo                 prints the real address (console auto-shifts ports).
goto :done

rem ------- check ---------------------------------------------------
:check
echo  python        : %PY%
%PY% --version 2>&1
echo  venv          : %VPY%
if exist "%VPY%" (echo    present) else (echo    MISSING - run "start.cmd" to bootstrap)
echo  team config   : config\team.yaml
if exist "config\team.yaml" (echo    present) else (echo    MISSING)
echo  codebuddy env : CODEBUDDY_SKIP_GIT_BASH_CHECK=1 / CODEBUDDY_INTERNET_ENVIRONMENT=internal
echo                 (set automatically by start.cmd console/serve modes)
echo  how to start  : double-click start.cmd  or  run: start.cmd console
goto :done

:help
echo  Usage:
echo    start.cmd                     start web console + open browser
echo    start.cmd serve ^<run-id^> ^<team-file^>   start scheduler window
echo                                  example: start.cmd serve run-1 config\team.yaml
echo    start.cmd check               print environment status
echo.
echo  The web console (http://127.0.0.1:%PORT%) lets you:
echo    Connections - probe codex/codebuddy login, follow sign-in guides
echo    Teams       - assemble a temp team (backend/role/count) or use default
echo    Runs        - create a run, one-click start/stop its scheduler
echo  First use: run "orchestrator auth codex" and "orchestrator auth codebuddy"
echo  to sign in (browser login state is NOT shared with the CLI).
goto :done

:fail
echo.
echo  [error] launcher aborted. See messages above.
pause
exit /b 1

:done
exit /b 0

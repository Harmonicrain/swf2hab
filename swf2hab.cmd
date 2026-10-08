@echo off
rem Double-click to convert step by step (swf2hab wizard), or use it from a command prompt
rem with arguments, e.g.  swf2hab convert C:\dcr\hof_furni -o C:\hab\hof_furni
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
set "PY=python"
where python >nul 2>nul || set "PY=py -3"
if "%~1"=="" (
    %PY% -m swf2hab wizard
    echo.
    pause
) else (
    %PY% -m swf2hab %*
)

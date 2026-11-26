@echo off
setlocal EnableExtensions
set "NoDefaultCurrentDirectoryInExePath=1"
chcp 65001 >nul 2>nul

for %%I in ("%~dp0..") do set "PLUGIN_ROOT=%%~fI"
set "SERVER_PY=%PLUGIN_ROOT%\scripts\computer_use_server.py"

set "RUNTIME_ROOT=%CUA_RUNTIME_DIR%"
if defined RUNTIME_ROOT set "RUNTIME_ROOT=%RUNTIME_ROOT:"=%"
if defined RUNTIME_ROOT if "%RUNTIME_ROOT:~0,2%"=="${" set "RUNTIME_ROOT="
if not defined RUNTIME_ROOT set "RUNTIME_ROOT=%PLUGIN_DATA%"
if defined RUNTIME_ROOT set "RUNTIME_ROOT=%RUNTIME_ROOT:"=%"
if defined RUNTIME_ROOT if "%RUNTIME_ROOT:~0,2%"=="${" set "RUNTIME_ROOT="
if not defined RUNTIME_ROOT set "RUNTIME_ROOT=%PLUGIN_ROOT%\.runtime"

:check_runtime
if not exist "%RUNTIME_ROOT%" mkdir "%RUNTIME_ROOT%" >nul 2>nul
copy /y nul "%RUNTIME_ROOT%\.cua_write_probe" >nul 2>nul
if errorlevel 1 goto :fallback_runtime
del /q "%RUNTIME_ROOT%\.cua_write_probe" >nul 2>nul
goto :runtime_ready

:fallback_runtime
if not defined LOCALAPPDATA goto :fallback_temp
set "RUNTIME_ROOT=%LOCALAPPDATA%\computer-use-win32"
if not exist "%RUNTIME_ROOT%" mkdir "%RUNTIME_ROOT%" >nul 2>nul
copy /y nul "%RUNTIME_ROOT%\.cua_write_probe" >nul 2>nul
if errorlevel 1 goto :fallback_temp
del /q "%RUNTIME_ROOT%\.cua_write_probe" >nul 2>nul
goto :runtime_ready

:fallback_temp
if not defined TEMP goto :fallback_userprofile
set "RUNTIME_ROOT=%TEMP%\computer-use-win32"
if not exist "%RUNTIME_ROOT%" mkdir "%RUNTIME_ROOT%" >nul 2>nul
copy /y nul "%RUNTIME_ROOT%\.cua_write_probe" >nul 2>nul
if errorlevel 1 goto :fallback_userprofile
del /q "%RUNTIME_ROOT%\.cua_write_probe" >nul 2>nul
goto :runtime_ready

:fallback_userprofile
if not defined USERPROFILE goto :runtime_ready
set "RUNTIME_ROOT=%USERPROFILE%\.computer-use-win32"
if not exist "%RUNTIME_ROOT%" mkdir "%RUNTIME_ROOT%" >nul 2>nul
copy /y nul "%RUNTIME_ROOT%\.cua_write_probe" >nul 2>nul
if not errorlevel 1 del /q "%RUNTIME_ROOT%\.cua_write_probe" >nul 2>nul

:runtime_ready
set "CUA_RUNTIME_DIR=%RUNTIME_ROOT%"

set "PYTHON_EXE="
set "FALLBACK_PY="
set "PYLIBS_DIR="

if defined CUA_PYTHON set "CUA_PYTHON=%CUA_PYTHON:"=%"
if defined CUA_PYTHON call :probe_python "%CUA_PYTHON%"
if defined CUA_PYTHON if defined PYTHON_EXE goto :run_server
if defined CUA_PYTHON if defined FALLBACK_PY goto :bootstrap_deps

for %%N in (python.exe python3.exe) do (
    for /f "usebackq delims=" %%P in (`where %%N 2^>nul`) do (
        call :probe_python "%%~fP"
        if defined PYTHON_EXE goto :run_server
    )
)

for /f "usebackq delims=" %%P in (`py -3 -c "import sys;print(sys.executable)" 2^>nul`) do (
    call :probe_python "%%~fP"
    if defined PYTHON_EXE goto :run_server
)

for /f "usebackq delims=" %%L in (`py -0p 2^>nul`) do (
    call :parse_py0p "%%L"
    if defined PYTHON_EXE goto :run_server
)

for %%D in ("%LOCALAPPDATA%\Programs\Python" "%LOCALAPPDATA%\Python" "%ProgramFiles%" "%ProgramFiles(x86)%" "%SystemDrive%" "%LOCALAPPDATA%\miniconda3" "%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3" "%ProgramData%\miniconda3" "%ProgramData%\anaconda3") do (
    call :probe_dir "%%~D"
    if defined PYTHON_EXE goto :run_server
)

if not defined FALLBACK_PY goto :no_python

:bootstrap_deps
set "PYTHON_EXE=%FALLBACK_PY%"
set "PYLIBS_TAG=pylibs"
for /f "usebackq delims=" %%T in (`call "%PYTHON_EXE%" -c "import sys;print('pylibs_cp'+str(sys.version_info[0])+str(sys.version_info[1]))" 2^>nul`) do set "PYLIBS_TAG=%%T"
set "PYLIBS_DIR=%RUNTIME_ROOT%\%PYLIBS_TAG%"
if not defined PYTHONPATH goto :bootstrap_set_pypath
set "PYTHONPATH=%PYLIBS_DIR%;%PYTHONPATH%"
goto :bootstrap_check

:bootstrap_set_pypath
set "PYTHONPATH=%PYLIBS_DIR%"

:bootstrap_check
"%PYTHON_EXE%" -c "from PIL import Image; import numpy; assert hasattr(Image, 'Resampling')" >nul 2>nul
if not errorlevel 1 goto :run_server
if not exist "%PYLIBS_DIR%" mkdir "%PYLIBS_DIR%" >nul 2>nul
"%PYTHON_EXE%" -m pip --version >nul 2>nul
if errorlevel 1 "%PYTHON_EXE%" -m ensurepip --upgrade 1>&2
"%PYTHON_EXE%" -m pip install --no-input --disable-pip-version-check --target "%PYLIBS_DIR%" -r "%PLUGIN_ROOT%\requirements.txt" 1>&2
if errorlevel 1 goto :pip_failed
goto :run_server

:pip_failed
echo [computer-use] Failed to install required dependencies from "%PLUGIN_ROOT%\requirements.txt" for "%PYTHON_EXE%". 1>&2
exit /b 1

:no_python
echo [computer-use] No Python 3.10+ interpreter found. Install Python 3.10+ from python.org or set CUA_PYTHON. 1>&2
exit /b 1

:probe_dir
set "DIR_CAND=%~1"
if not defined DIR_CAND goto :eof
if not exist "%DIR_CAND%\." goto :eof
if exist "%DIR_CAND%\python.exe" call :probe_python "%DIR_CAND%\python.exe"
if defined PYTHON_EXE goto :eof
for /d %%S in ("%DIR_CAND%\Python3*" "%DIR_CAND%\pythoncore-*") do (
    if exist "%%~S\python.exe" call :probe_python "%%~S\python.exe"
    if defined PYTHON_EXE goto :eof
)
goto :eof

:parse_py0p
set "RAW_LINE=%~1"
if not defined RAW_LINE goto :eof
for /f "tokens=1,2*" %%A in ("%RAW_LINE%") do (
    if "%%B"=="*" (
        if exist "%%~C" call :probe_python "%%~fC"
    ) else if not "%%~C"=="" (
        if exist "%%~B %%~C" call :probe_python "%%~fB %%~C"
    ) else if exist "%%~B" (
        call :probe_python "%%~fB"
    )
)
goto :eof

:probe_python
if defined PYTHON_EXE goto :eof
set "CAND_EXE=%~1"
if not defined CAND_EXE goto :eof
if not exist "%CAND_EXE%" goto :eof
for %%F in ("%CAND_EXE%") do if "%%~zF"=="" goto :eof
for %%F in ("%CAND_EXE%") do if %%~zF EQU 0 goto :eof
if defined LOCALAPPDATA if /i "%CAND_EXE%"=="%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe" goto :eof
if defined LOCALAPPDATA if /i "%CAND_EXE%"=="%LOCALAPPDATA%\Microsoft\WindowsApps\python3.exe" goto :eof
"%CAND_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
"%CAND_EXE%" -c "from PIL import Image; import numpy; assert hasattr(Image, 'Resampling')" >nul 2>nul
if not errorlevel 1 goto :accept_global
set "CAND_TAG="
for /f "usebackq delims=" %%T in (`call "%CAND_EXE%" -c "import sys;print('pylibs_cp'+str(sys.version_info[0])+str(sys.version_info[1]))" 2^>nul`) do set "CAND_TAG=%%T"
if not defined CAND_TAG goto :probe_fallback
set "CAND_PYLIBS=%RUNTIME_ROOT%\%CAND_TAG%"
if not exist "%CAND_PYLIBS%\." goto :probe_fallback
set "CUA_PROBE_PYLIBS=%CAND_PYLIBS%"
"%CAND_EXE%" -c "import os,sys; p=os.environ.get('CUA_PROBE_PYLIBS',''); p and sys.path.insert(0,p); from PIL import Image; import numpy; assert hasattr(Image,'Resampling')" >nul 2>nul
if errorlevel 1 goto :probe_fallback_clear
set "CUA_PROBE_PYLIBS="
set "PYTHON_EXE=%CAND_EXE%"
set "PYLIBS_DIR=%CAND_PYLIBS%"
if not defined PYTHONPATH goto :set_pylibs_only
set "PYTHONPATH=%PYLIBS_DIR%;%PYTHONPATH%"
goto :eof

:accept_global
set "PYTHON_EXE=%CAND_EXE%"
goto :eof

:set_pylibs_only
set "PYTHONPATH=%PYLIBS_DIR%"
goto :eof

:probe_fallback_clear
set "CUA_PROBE_PYLIBS="
:probe_fallback
if not defined FALLBACK_PY set "FALLBACK_PY=%CAND_EXE%"
goto :eof

:run_server
if defined PYLIBS_DIR set "CUA_PYLIBS_DIR=%PYLIBS_DIR%"
if not defined PYTHONUTF8 set "PYTHONUTF8=1"
if not defined PYTHONIOENCODING set "PYTHONIOENCODING=utf-8"
"%PYTHON_EXE%" -u "%SERVER_PY%" %*
exit /b %errorlevel%

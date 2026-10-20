@echo off
setlocal enabledelayedexpansion
title Η Ατζέντα των Ειδήσεων
cd /d "%~dp0"
chcp 65001 >nul

:: -----------------------------------------------
:: Βασισμένο στο run.bat του MyRAE Analytics.
:: 1. Python 3.10+   2. venv   3. πακέτα (μόνο όταν αλλάζει το requirements.txt)
:: 4. δεδομένα (μόνο αν λείπουν)   5. εκκίνηση στο http://127.0.0.1:8000
:: -----------------------------------------------

:: Ανανέωση PATH από το registry, ώστε να φαίνεται Python που μόλις εγκαταστάθηκε
:refresh_path
for /f "tokens=2*" %%a in (
  'reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul'
) do set "_SYS_PATH=%%b"
for /f "tokens=2*" %%a in (
  'reg query "HKCU\Environment" /v Path 2^>nul'
) do set "_USR_PATH=%%b"
if defined _SYS_PATH set "PATH=%_SYS_PATH%"
if defined _USR_PATH set "PATH=%PATH%;%_USR_PATH%"

:: -----------------------------------------------
:: 1. Python 3.10+
:: -----------------------------------------------
:find_python
set PYTHON_EXE=
for %%v in (3.13 3.12 3.11 3.10) do (
  if not defined PYTHON_EXE (
    py -%%v --version >nul 2>&1
    if !errorlevel!==0 set PYTHON_EXE=py -%%v
  )
)
if not defined PYTHON_EXE (
  python --version >nul 2>&1
  if !errorlevel!==0 (
    for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do (
      for /f "tokens=1,2 delims=." %%a in ("%%v") do (
        if %%a GEQ 3 if %%b GEQ 10 set PYTHON_EXE=python
      )
    )
  )
)
if defined PYTHON_EXE goto :have_python

echo.
echo  [SETUP] Δεν βρέθηκε Python 3.10+. Εγκατάσταση μέσω winget...
where winget >nul 2>&1
if %errorlevel%==0 (
    winget install --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements
    if !errorlevel!==0 (
        call :refresh_path
        goto :find_python
    )
)
echo.
echo  [ERROR] Η αυτόματη εγκατάσταση απέτυχε. Εγκαταστήστε Python από https://www.python.org/downloads/
pause
exit /b 1

:: -----------------------------------------------
:: 2. Virtual environment (.venv μέσα στον φάκελο του project)
:: -----------------------------------------------
:have_python
echo  [OK] Python: %PYTHON_EXE%
set VENV_DIR=%~dp0.venv
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo  [SETUP] Δημιουργία virtual environment...
    %PYTHON_EXE% -m venv "%VENV_DIR%"
    if !errorlevel! neq 0 (
        echo  [ERROR] Αποτυχία δημιουργίας virtual environment.
        pause
        exit /b 1
    )
)
call "%VENV_DIR%\Scripts\activate.bat"

:: -----------------------------------------------
:: 3. Πακέτα — μόνο όταν αλλάζει το requirements.txt (έλεγχος hash)
:: -----------------------------------------------
set REQ_FILE=%~dp0requirements.txt
set STAMP_FILE=%VENV_DIR%\.req_stamp
set NEEDS_INSTALL=0
for /f %%h in ('certutil -hashfile "%REQ_FILE%" MD5 2^>nul ^| findstr /v ":"') do set CURRENT_HASH=%%h
if not exist "%STAMP_FILE%" (
    set NEEDS_INSTALL=1
) else (
    set /p SAVED_HASH=<"%STAMP_FILE%"
    if /i "!CURRENT_HASH!" neq "!SAVED_HASH!" set NEEDS_INSTALL=1
)
if %NEEDS_INSTALL%==1 (
    echo  [SETUP] Εγκατάσταση πακέτων...
    python -m pip install -q -r "%REQ_FILE%"
    if !errorlevel! neq 0 (
        echo  [ERROR] Το pip install απέτυχε. Ελέγξτε τη σύνδεση στο διαδίκτυο.
        pause
        exit /b 1
    )
    echo !CURRENT_HASH!>"%STAMP_FILE%"
    echo  [OK] Πακέτα εγκαταστάθηκαν.
) else (
    echo  [OK] Πακέτα ενημερωμένα.
)

:: -----------------------------------------------
:: 4. Δεδομένα — δημιουργία ευρετηρίου αν λείπει (~3-4 λεπτά, μία φορά)
:: -----------------------------------------------
if not exist "%~dp0data\news.duckdb" (
    echo.
    echo  [SETUP] Δεν βρέθηκε το data\news.duckdb. Προετοιμασία δεδομένων...
    echo          Χρειάζεται το dataset glossAPI/amna-press, βλ. README.
    python scripts\prepare_data.py
    if !errorlevel! neq 0 (
        echo  [ERROR] Η προετοιμασία δεδομένων απέτυχε.
        pause
        exit /b 1
    )
)

:: -----------------------------------------------
:: 5. Εκκίνηση
:: -----------------------------------------------
echo.
echo  ==========================================
echo   Η Ατζέντα των Ειδήσεων
echo   http://127.0.0.1:8000
echo   (Ctrl+C για τερματισμό)
echo  ==========================================
echo.
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:8000"
python server.py

echo.
echo  [INFO] Η εφαρμογή σταμάτησε.
pause

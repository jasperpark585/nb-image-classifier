@echo off
setlocal

REM Build NB Image Classifier as Windows EXE (onedir, no UPX)
REM Run this on a Windows machine with Python 3.11 installed.

where py >nul 2>&1
if %errorlevel% neq 0 (
  echo [ERROR] Python launcher (py) not found. Install Python 3.11 first.
  exit /b 1
)

if not exist .venv (
  py -3.11 -m venv .venv
)

call .venv\Scripts\activate
if %errorlevel% neq 0 (
  echo [ERROR] Failed to activate virtual environment.
  exit /b 1
)

py -3.11 -m pip install --upgrade pip
if %errorlevel% neq 0 exit /b 1

pip install -r requirements.txt
if %errorlevel% neq 0 exit /b 1

pip install pyinstaller
if %errorlevel% neq 0 exit /b 1

py -3.11 -m PyInstaller --noconfirm --clean --windowed --onedir --noupx --name NBImageClassifier nb_image_classifier.py
if %errorlevel% neq 0 exit /b 1

echo.
echo [DONE] Build complete.
echo Output: dist\NBImageClassifier\NBImageClassifier.exe
endlocal

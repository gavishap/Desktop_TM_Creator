@echo off
REM Build TM Desktop into a distributable folder + zip.
REM Prereqs (one time):  py -m venv venv  &&  venv\Scripts\pip install -r requirements.txt
setlocal

echo === Cleaning previous build ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo === Running PyInstaller ===
call venv\Scripts\pyinstaller.exe build.spec --noconfirm
if errorlevel 1 (
  echo Build failed.
  exit /b 1
)

echo === Adding .NET config ===
copy /y "TM Desktop.exe.config" "dist\TM Desktop\TM Desktop.exe.config" >nul

echo === Zipping for distribution ===
powershell -NoProfile -Command "Compress-Archive -Path 'dist/TM Desktop/*' -DestinationPath 'dist/TM-Desktop.zip' -Force"

echo.
echo Done. Hand off:  dist\TM-Desktop.zip
echo The recipient unzips and double-clicks "TM Desktop.exe" (Google Chrome must be installed).
endlocal

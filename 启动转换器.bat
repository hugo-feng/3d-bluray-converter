@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "bluray3d_converter.py"
) else (
  start "" "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" "bluray3d_converter.py"
)

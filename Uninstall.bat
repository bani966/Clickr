@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0clickr.ps1" uninstall
echo.
echo Python and its packages are left installed. To remove the packages:
echo     python -m pip uninstall -y pystray pillow six
echo.
pause

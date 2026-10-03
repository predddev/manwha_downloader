@echo off
title ManhwaGod Downloader ^& Web Reader
echo.
echo  =======================================================
echo   ManhwaGod Ultra High-Speed Downloader ^& Web Reader
echo  =======================================================
echo.
echo  Starting server at http://localhost:5000 ...
echo.
cd /d "%~dp0"
start "" http://localhost:5000
python app.py
pause


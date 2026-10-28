@echo off
title AIS Station - audio devices
cd /d "%~dp0"
AIS-Station.exe --list-audio
echo.
echo Use the name or number of your radio's input as "audio_device" in config.json.
echo.
pause

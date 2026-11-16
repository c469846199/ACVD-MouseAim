@echo off
cd /d "%~dp0"
set "PY=python"
title ACVD Aiming - Stop
"%PY%" tools\launch.py stop

@echo off
cd /d "%~dp0"
set "PY=python"
title ACVD Aiming - Log
"%PY%" tools\launch.py log

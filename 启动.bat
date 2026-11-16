@echo off
cd /d "%~dp0"
set "PY=python"
title ACVD Aiming - Core
"%PY%" tools\launch.py core

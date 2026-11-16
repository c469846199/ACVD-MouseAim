@echo off
cd /d "%~dp0"
set "PY=python"
title ACVD Aiming - Status
"%PY%" tools\ctl.py status
"%PY%" -c "input() if __import__('sys').stdin and __import__('sys').stdin.isatty() else None"

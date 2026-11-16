@echo off
cd /d "%~dp0"
set "PY=python"
title ACVD Aiming - SelfTest
"%PY%" tools\launch.py selftest

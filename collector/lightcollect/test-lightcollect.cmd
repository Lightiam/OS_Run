@echo off
setlocal
set PYTHONPATH=%~dp0..
python -m unittest discover -s "%~dp0tests"

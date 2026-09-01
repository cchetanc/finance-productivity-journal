@echo off
echo Setting up local environment to run database population...
if not exist venv (
    python -m venv venv
)
call venv\Scripts\activate.bat
pip install -r scratch\requirements_screener.txt -q
echo Starting population script...
python scratch\populate_locally.py
pause

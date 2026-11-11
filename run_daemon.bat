@echo off
rem Runs the OCR Scanner background daemon commands inside the same virtualenv as run.bat.
rem   run_daemon.bat start ^| stop ^| status ^| run
rem   run_daemon.bat autostart enable ^| disable ^| status
cd /d "%~dp0"
if not exist .venv ( py -3 -m venv .venv )
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
python ocr_daemon.py %*

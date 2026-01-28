@echo off
title Mistral Discord Bot
echo Checking dependencies...
pip install -r requirements.txt
echo Installing Browsers...
playwright install chromium
cls
echo Starting Bot...
echo Make sure Ollama is running in the background!
python bot.py
pause

@echo off
cd /d "%~dp0self-bot"
start cmd /k ".venv\Scripts\activate && python main.py"

cd /d "%~dp0destination_bot"
start cmd /k ".venv\Scripts\activate && python bot.py"

@echo off

REM Start destination_bot first (instantly)
cd /d "%~dp0destination_bot"
start cmd /k ".venv\Scripts\activate && python bot.py"

REM Wait 15 seconds, then start self-bot
timeout /t 15 /nobreak
cd /d "%~dp0self-bot"
start cmd /k ".venv\Scripts\activate && python main.py"

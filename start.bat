@echo off
cd /d "C:\Users\Administrator\Documents\1Tap Notify\self-bot"
start cmd /k ".venv\Scripts\activate && python main.py"

cd /d "C:\Users\Administrator\Documents\1Tap Notify\destination_bot"
start cmd /k ".venv\Scripts\activate && python bot.py"
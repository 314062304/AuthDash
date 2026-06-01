@echo off
REM AuthDash Launcher - Start server in background
cd /d "D:\Claudecode\Project\AuthDash"
start /b "" ".venv\Scripts\pythonw.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000
echo AuthDash started on http://127.0.0.1:8000

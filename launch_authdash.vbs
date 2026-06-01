'' AuthDash Launcher - Truly detached process (Windows-only)
Dim shell, fs
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "D:\Claudecode\Project\AuthDash"
shell.Run "D:\Claudecode\Project\AuthDash\.venv\Scripts\pythonw.exe -m uvicorn main:app --host 127.0.0.1 --port 8000", 0, False

"""Launch AuthDash as a truly detached background process on Windows."""
import subprocess
import sys
import os

BASE = os.path.dirname(os.path.abspath(__file__))
PYTHONW = os.path.join(BASE, ".venv", "Scripts", "pythonw.exe")
CMD = [PYTHONW, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000"]

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

proc = subprocess.Popen(
    CMD,
    cwd=BASE,
    close_fds=True,
    creationflags=flags,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    stdin=subprocess.DEVNULL,
)

print(f"AuthDash launched (PID {proc.pid})")
sys.exit(0)

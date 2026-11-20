@echo off
REM Builds agent\dist\SmartPad Helper.exe: the helper with Python built in, so other
REM Windows computers don't need Python. The server includes it in /agent/download.
cd /d "%~dp0\.."
".venv\Scripts\python.exe" -m pip install -q pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --console ^
  --name "SmartPad Helper" --icon NONE ^
  --paths server --hidden-import touchpad ^
  --collect-submodules pynput --collect-submodules websockets ^
  --distpath agent\dist --workpath agent\build --specpath agent\build ^
  agent\smartpad_agent.py

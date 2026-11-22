@echo off
rem Abre JARVIS con voz usando el entorno .venv del proyecto, sin ventana de consola.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -B -X utf8 -m jarvis --ventana %*
) else (
    start "" pythonw -B -X utf8 -m jarvis --ventana %*
)

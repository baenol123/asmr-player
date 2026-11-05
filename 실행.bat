@echo off
chcp 65001 > nul
cd /d "%~dp0"

where python > nul 2>&1
if errorlevel 1 (
    echo Python이 설치되어 있지 않습니다. https://www.python.org/downloads/ 에서 설치하세요.
    echo 설치할 때 "Add python.exe to PATH" 체크 필수!
    pause
    exit /b 1
)

python -c "import flask, requests, numpy" 2> nul
if errorlevel 1 (
    echo 필요한 패키지를 설치합니다...
    pip install -r requirements.txt
)

python server.py
pause

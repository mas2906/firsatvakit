@echo off
chcp 65001 > nul
echo.
echo ===================================
echo   FirsatVakti -- FastAPI Sunucu
echo ===================================
echo.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [HATA] .venv bulunamadi.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [HATA] .env bulunamadi.
    pause
    exit /b 1
)

echo [*] Sunucu baslatiliyor: http://localhost:8000
echo [*] Admin panel:        http://localhost:8000/admin
echo [*] Durdurmak icin:     CTRL+C
echo.

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

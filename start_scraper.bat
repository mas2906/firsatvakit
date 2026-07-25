@echo off
chcp 65001 > nul
echo.
echo ===================================
echo   FirsatVakti — Kategori Scraper
echo ===================================
echo.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [HATA] .venv bulunamadi. Once: python -m venv .venv ^& pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo [HATA] .env bulunamadi.
    pause
    exit /b 1
)

echo [*] Kategori scraper baslatiliyor...
echo [*] Her 10 dakikada bir pending kategorileri tarar
echo [*] Durdurmak icin: CTRL+C
echo.

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe local_scraper.py

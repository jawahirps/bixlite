@echo off
echo ============================================
echo   Ghaf App - RCM ^& Clinical Platform
echo ============================================
echo.
echo Starting Backend (FastAPI)...
start "Ghaf Backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"

timeout /t 4 /nobreak > nul

echo Starting Frontend (React/Vite)...
start "Ghaf Frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

timeout /t 3 /nobreak > nul

echo.
echo ============================================
echo   App is starting up...
echo   Open: http://localhost:5173
echo ============================================
echo.
pause

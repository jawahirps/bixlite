# Ghaf App — Windows Setup Guide

## Prerequisites (install in this order)

1. **Python 3.12** — https://www.python.org/downloads/
   - ✅ Check "Add Python to PATH" during install

2. **Node.js 18+** — https://nodejs.org/en/download
   - Use the LTS version

3. **PostgreSQL 15+** — https://www.postgresql.org/download/windows/
   - Remember the password you set for `postgres` user
   - After install, open pgAdmin or psql and create:
     ```sql
     CREATE USER ghaf WITH PASSWORD 'yourpassword';
     CREATE DATABASE ghaf_app OWNER ghaf;
     ```

---

## Setup Steps

### 1. Extract this zip
Extract `ghaf-app.zip` to a folder, e.g. `C:\ghaf-app\`

### 2. Backend setup
Open **Command Prompt** or **PowerShell** in `C:\ghaf-app\backend\`

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Copy and configure `.env`:
```bat
copy .env.example .env
notepad .env
```

Edit `.env` — set your DATABASE_URL:
```
DATABASE_URL=postgresql+psycopg2://ghaf:yourpassword@localhost:5432/ghaf_app
JWT_SECRET_KEY=replace-with-a-long-random-secret
CORS_ORIGINS=http://localhost:5173
METABASE_SITE_URL=http://localhost:3000
METABASE_SECRET_KEY=
```

Run database migrations:
```bat
.venv\Scripts\python -c "from app.database import Base, engine; from app.models import *; Base.metadata.create_all(engine)"
```

Start backend:
```bat
.venv\Scripts\uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 3. Frontend setup
Open a **new** Command Prompt in `C:\ghaf-app\frontend\`

```bat
npm install
npm run dev
```

### 4. Access the app
Open browser: **http://localhost:5173**

---

## Reference Data Import
After first login, import reference data from `backend\data\`:
- `clinicians.xlsx` — DHA clinician registry
- `payers.xlsx` — Insurance payer list
- `self_paid.xlsx` — Self-pay configuration

---

## Windows Startup Script
Save as `start_ghaf.bat` in `C:\ghaf-app\`:

```bat
@echo off
echo Starting Ghaf App Backend...
start "Ghaf Backend" cmd /k "cd /d C:\ghaf-app\backend && .venv\Scripts\uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"

timeout /t 3

echo Starting Ghaf App Frontend...
start "Ghaf Frontend" cmd /k "cd /d C:\ghaf-app\frontend && npm run dev"

echo.
echo App starting... Open http://localhost:5173 in your browser
pause
```

---

## Troubleshooting
- **psycopg2 error**: Run `.venv\Scripts\pip install psycopg2-binary`
- **Port 8000 in use**: Change `--port 8000` to `--port 8001` and update `frontend\src\api\client.js`
- **CORS error**: Make sure `CORS_ORIGINS=http://localhost:5173` in `.env`

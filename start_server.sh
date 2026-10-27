#!/bin/bash
# start_server.sh — Build frontend, start backend + Cloudflare Tunnel
# Run from: ~/my_project/ghaf-app/
# Usage: bash start_server.sh

BASE="$(cd "$(dirname "$0")" && pwd)"
FRONTEND="$BASE/frontend"
BACKEND="$BASE/backend"
TUNNEL_NAME="ghaf-reports"
BACKEND_PORT=8000

echo "=== [1/3] Building frontend ==="
cd "$FRONTEND"
npm run build
if [ $? -ne 0 ]; then echo "Frontend build FAILED"; exit 1; fi
echo "Frontend build complete → $FRONTEND/dist"

# When the backend and tunnel run as launchd services (com.ghaf.backend / com.ghaf.tunnel, started at login and
# restarted if they stop), restart those instead of starting a second copy.
if launchctl print "gui/$(id -u)/com.ghaf.backend" >/dev/null 2>&1; then
  echo ""
  echo "=== [2/2] Restarting launchd services (backend + tunnel) ==="
  launchctl kickstart -k "gui/$(id -u)/com.ghaf.backend"
  launchctl kickstart -k "gui/$(id -u)/com.ghaf.tunnel" 2>/dev/null || true
  for i in $(seq 1 20); do
    sleep 1
    [ "$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$BACKEND_PORT/api/health)" = "200" ] && break
  done
  echo "Backend: $(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$BACKEND_PORT/api/health)  (log: ~/Library/Logs/ghaf_backend.log)"
  echo "Site:    https://reports.ghafservices.com"
  exit 0
fi

echo ""
echo "=== [2/3] Starting backend (port $BACKEND_PORT) ==="
cd "$BACKEND"
# Kill any old uvicorn on this port
lsof -ti tcp:$BACKEND_PORT | xargs kill -9 2>/dev/null || true
sleep 1

# Use the project venv only if it actually runs (a moved venv keeps stale shebangs),
# else the system Python that has the backend packages
if .venv/bin/python -c "import uvicorn" >/dev/null 2>&1; then
  UVICORN=(.venv/bin/python -m uvicorn)
else
  UVICORN=(python3.12 -m uvicorn)
fi
nohup "${UVICORN[@]}" app.main:app --host 127.0.0.1 --port $BACKEND_PORT > /tmp/ghaf_backend.log 2>&1 &
UVICORN_PID=$!
echo "Backend PID: $UVICORN_PID  (log: /tmp/ghaf_backend.log)"

echo "Waiting for backend to start..."
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$BACKEND_PORT/api/health 2>/dev/null)
  if [ "$HTTP_STATUS" = "200" ]; then
    echo "Backend healthy ✓ (took ${i}s)"
    break
  fi
  echo "  ... waiting ($i/10)"
done

if [ "$HTTP_STATUS" != "200" ]; then
  echo "Backend did not start. Last 20 lines of log:"
  tail -20 /tmp/ghaf_backend.log
  exit 1
fi

echo ""
echo "=== [3/3] Starting Cloudflare Tunnel ($TUNNEL_NAME) ==="
pkill -f "cloudflared tunnel run" 2>/dev/null || true
sleep 1
nohup cloudflared tunnel run "$TUNNEL_NAME" > /tmp/ghaf_tunnel.log 2>&1 &
TUNNEL_PID=$!
echo "Tunnel PID: $TUNNEL_PID  (log: /tmp/ghaf_tunnel.log)"
sleep 3

echo ""
echo "=== Done ==="
echo "Site:    https://reports.ghafservices.com"
echo "Health:  https://reports.ghafservices.com/api/health"
echo ""
echo "To watch logs:"
echo "  tail -f /tmp/ghaf_backend.log"
echo "  tail -f /tmp/ghaf_tunnel.log"

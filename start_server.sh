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

echo ""
echo "=== [2/3] Starting backend (port $BACKEND_PORT) ==="
cd "$BACKEND"
# Kill any old uvicorn on this port
lsof -ti tcp:$BACKEND_PORT | xargs kill -9 2>/dev/null || true
sleep 1

nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $BACKEND_PORT > /tmp/ghaf_backend.log 2>&1 &
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

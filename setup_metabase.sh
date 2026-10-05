#!/bin/bash
# setup_metabase.sh — Download Metabase OSS JAR and create run script
# Run once from: ~/my_project/ghaf-app/
# Usage: bash setup_metabase.sh

set -e

MB_DIR="$HOME/metabase"
MB_JAR="$MB_DIR/metabase.jar"
MB_VERSION="v0.52.5"
MB_URL="https://downloads.metabase.com/${MB_VERSION}/metabase.jar"

echo "=== Metabase OSS Setup ==="
echo ""

# Check Java
if ! java -version 2>/dev/null; then
    echo "ERROR: Java not found. Install with: brew install --cask temurin"
    exit 1
fi
echo ""

# Create directory
mkdir -p "$MB_DIR"

# Download JAR if not present
if [ -f "$MB_JAR" ]; then
    echo "Metabase JAR already downloaded at $MB_JAR"
else
    echo "Downloading Metabase ${MB_VERSION} (~340MB)..."
    curl -L "$MB_URL" -o "$MB_JAR" --progress-bar
    echo "Download complete."
fi

# Write start script
cat > "$MB_DIR/start_metabase.sh" << 'RUNSCRIPT'
#!/bin/bash
# start_metabase.sh — Start Metabase on port 3000
MB_DIR="$HOME/metabase"
MB_JAR="$MB_DIR/metabase.jar"

echo "Starting Metabase on http://localhost:3000 ..."
echo "Log: /tmp/metabase.log"
echo ""
echo "Press Ctrl+C to stop."

export MB_DB_TYPE=h2
export MB_DB_FILE="$MB_DIR/metabase.db"
export MB_JETTY_PORT=3000

nohup java -jar "$MB_JAR" > /tmp/metabase.log 2>&1 &
MB_PID=$!
echo "Metabase PID: $MB_PID"

echo "Waiting for Metabase to start (may take 30-60 seconds)..."
for i in $(seq 1 60); do
    sleep 2
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/api/health 2>/dev/null)
    if [ "$STATUS" = "200" ]; then
        echo ""
        echo "✅ Metabase is ready → http://localhost:3000"
        echo ""
        echo "Next steps:"
        echo "  1. Open http://localhost:3000 in your browser"
        echo "  2. Complete the setup wizard"
        echo "  3. Add database: PostgreSQL"
        echo "     Host: localhost   Port: 5432"
        echo "     Database: ghaf_app   Username: ghaf   Password: (leave blank)"
        echo ""
        exit 0
    fi
    echo "  ... waiting (${i}/60)"
done

echo "Timed out. Check /tmp/metabase.log for errors."
RUNSCRIPT

chmod +x "$MB_DIR/start_metabase.sh"

echo ""
echo "=== Done ==="
echo "To start Metabase, run:"
echo "  bash ~/metabase/start_metabase.sh"
echo ""
echo "Or just run it now:"
echo "  bash ~/metabase/start_metabase.sh"

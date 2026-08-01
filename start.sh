#!/bin/bash
set -e

echo "[start.sh] Starting Python Flask backend..."

# Use gunicorn in production for proper multi-threaded request handling.
# 1 worker preserves in-memory scraper state (progress dicts, stop events).
# 8 threads handles concurrent dashboard polling without blocking scrapers.
if command -v gunicorn &>/dev/null; then
  gunicorn \
    --bind 0.0.0.0:8000 \
    --workers 1 \
    --threads 8 \
    --worker-class gthread \
    --timeout 300 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile - \
    "app:app" &
  PYTHON_PID=$!
  echo "[start.sh] Gunicorn PID: $PYTHON_PID"
else
  python3 app.py &
  PYTHON_PID=$!
  echo "[start.sh] Flask PID: $PYTHON_PID (gunicorn not found, using dev server)"
fi

echo "[start.sh] Waiting for Flask to bind on port 8000..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/api/scrapers > /dev/null 2>&1; then
    echo "[start.sh] Flask is up after ${i}s"
    break
  fi
  sleep 1
done

echo "[start.sh] Starting Node.js server..."
NODE_ENV=production npx tsx server.ts &
NODE_PID=$!
echo "[start.sh] Node PID: $NODE_PID"

# Keep script alive — exit only if both processes die
wait $NODE_PID
echo "[start.sh] Node exited. Killing Flask/Gunicorn..."
kill $PYTHON_PID 2>/dev/null

#!/usr/bin/env bash
# Start the Streamlit control panel UI
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "Starting GPU Fan Control UI at http://localhost:8505"
exec "$PROJECT_DIR/venv/bin/streamlit" run "$PROJECT_DIR/ui/app.py" \
    --server.port 8505 \
    --server.headless true \
    --server.address 127.0.0.1

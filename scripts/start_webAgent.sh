#!/bin/bash
# Repo root (script lives in scripts/). .venv and .env are resolved from here.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.." || exit 1

# Run after database migration; starts uvicorn in the background (default port 8080, override with PORT=).

echo "========================================="
echo "WebAgent - Quick Start"
echo "========================================="

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "❌ Virtual environment not found"
    echo "Run: uv sync --python 3.14 --extra encryption"
    exit 1
fi

# Check environment file
if [ ! -f ".env" ]; then
    echo "❌ .env file not found"
    echo "Copy .env.example to .env and add your credentials"
    exit 1
fi

# Start server in background
PORT="${PORT:-8080}"
echo "Starting FastAPI server on port ${PORT}..."
echo "Server will run in background. Logs: server.log"
echo "To stop: pkill -f 'uvicorn app.main:app'"

# Create log file
LOG_FILE="server_$(date +%Y%m%d_%H%M%S).log"
echo "Server starting at $(date)" > "$LOG_FILE"

# Start server
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PORT" >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!

echo "Server started with PID: $SERVER_PID"
echo "Logs: $LOG_FILE"
echo ""

# Wait for server to start
echo "Waiting for server to start..."
sleep 3

# Check if server is running
if curl -s "http://localhost:${PORT}/health" > /dev/null; then
    echo "✅ Server is running!"
    echo ""
    echo "Context defaults are applied on first chat if the user has no context rows (no separate seed endpoint)."
    echo ""
    echo "========================================="
    echo "✅ WebAgent is ready!"
    echo ""
    echo "Test endpoints:"
    echo "  Health:    curl http://localhost:${PORT}/health"
    echo "  Chat test: curl -X POST http://localhost:${PORT}/api/v1/chat \\"
    echo "    -H \"Content-Type: application/json\" \\"
    echo "    -d '{\"user_id\":\"test\",\"session_id\":\"test\",\"message\":\"Hello!\"}'"
    echo ""
    echo "Web interface:"
    echo "  Minimal tester:  http://localhost:${PORT}/test"
    echo "  Full UI:         http://localhost:${PORT}/index.html"
    echo ""
    echo "Connect your web app to:"
    echo "  http://localhost:${PORT}/api/v1/chat"
    echo ""
    echo "API documentation:"
    echo "  http://localhost:${PORT}/docs"
    echo ""
    echo "To stop server:"
    echo "  pkill -f 'uvicorn app.main:app'"
    echo "  or kill $SERVER_PID"
    echo ""
    echo "========================================="
    
    # Write PID to file for later cleanup
    echo $SERVER_PID > .server.pid
    echo "Server PID saved to .server.pid"
    
else
    echo "❌ Server failed to start"
    echo "Check the log file: $LOG_FILE"
    kill $SERVER_PID 2>/dev/null
    exit 1
fi

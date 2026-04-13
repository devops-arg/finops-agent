#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== FinOps Agent ==="

# Check Python
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo "Error: Python 3 is required"
    exit 1
fi
PYTHON=$(command -v python3 || command -v python)

# Create venv if needed
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv venv
fi

# Activate venv
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f "venv/Scripts/activate" ]; then
    source venv/Scripts/activate
fi

# Install deps
echo "Installing dependencies..."
pip install -q -r requirements.txt

# Check .env
if [ ! -f ".env" ]; then
    echo "Warning: .env file not found. Copy .env.example to .env and configure it."
    echo "  cp .env.example .env"
    exit 1
fi

# Start backend
echo "Starting backend on port ${PORT:-8000}..."
python run_server.py &
BACKEND_PID=$!

# Start frontend server
echo "Starting frontend on port 3000..."
python -m http.server 3000 --directory frontend &
FRONTEND_PID=$!

echo ""
echo "Backend:   http://localhost:${PORT:-8000}"
echo "Frontend:  http://localhost:3000"
echo "API Docs:  http://localhost:${PORT:-8000}/docs"
echo ""
echo "Press Ctrl+C to stop"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" INT TERM
wait

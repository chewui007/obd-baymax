#!/usr/bin/env bash
# ====================================================================
# BAYMAX OBD // Telemetry & Diagnostic Workstation Launcher
# ====================================================================

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "[SYS] Starting BAYMAX OBD Workstation..."

if [ ! -d ".venv" ]; then
    echo "[SYS] Initializing Python 3.12 virtualenv..."
    /opt/homebrew/bin/python3.12 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt
fi

# Auto-start Ollama daemon if installed and not running
if command -v ollama >/dev/null 2>&1; then
    if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "[LLM] Starting local Ollama daemon on localhost:11434..."
        nohup ollama serve >/tmp/baymax_ollama.log 2>&1 &
        sleep 1
    else
        echo "[LLM] Local Ollama daemon online on localhost:11434."
    fi
fi

echo "[NET] Workstation active at: http://127.0.0.1:8000"
exec .venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload

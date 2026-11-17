"""Configuración central de BAYMAX OBD (rutas y variables de entorno)."""
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
MANUALS_DIR = DATA_DIR / "manuals"
CHROMA_DIR = DATA_DIR / "chroma_data"
DB_PATH = DATA_DIR / "garage.db"
FRONTEND_DIR = ROOT_DIR / "frontend"

HOST = os.getenv("BAYMAX_HOST", "127.0.0.1")
PORT = int(os.getenv("BAYMAX_PORT", "8000"))

# Ollama (LLM local). qwen2.5:7b en Ollama ya viene cuantizado Q4_K_M por defecto.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
PREFERRED_MODELS = ["qwen2.5:7b", "qwen2.5:7b-instruct", "llama3.1:8b", "qwen2.5", "llama3.1"]

# Telemetría
TELEMETRY_HZ = float(os.getenv("TELEMETRY_HZ", "10"))
SNAPSHOT_EVERY_S = float(os.getenv("SNAPSHOT_EVERY_S", "1.0"))  # muestreo a SQLite

# Búsqueda web: usar Jina Reader (r.jina.ai) para extraer el texto de las páginas
USE_JINA = os.getenv("USE_JINA", "0") == "1"

for _d in (DATA_DIR, MANUALS_DIR, CHROMA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

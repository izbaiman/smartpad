"""Central configuration for the SmartPad server.

Values come from (highest priority first):
  1. Real environment variables
  2. A `.env` file in the project root (never served to the phone)
  3. Defaults below
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
DATA_DIR = ROOT_DIR / "data"
DOCS_DIR = DATA_DIR / "documents"
EXPORT_DIR = ROOT_DIR / "exports"
CERT_DIR = DATA_DIR / "certs"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines, # comments). Does not override real env vars."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    host: str = field(default_factory=lambda: os.environ.get("SMARTPAD_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("SMARTPAD_PORT", "8765")))
    https: bool = field(default_factory=lambda: _bool("SMARTPAD_HTTPS", False))
    # "auto" -> anthropic if a credential is available, else offline
    ai_provider: str = field(default_factory=lambda: os.environ.get("SMARTPAD_AI_PROVIDER", "auto").lower())
    model: str = field(default_factory=lambda: os.environ.get("SMARTPAD_MODEL", "claude-opus-5-5"))
    # Allows touchpad to actually move the mouse. Set to 0 for headless test machines.
    enable_input_control: bool = field(default_factory=lambda: _bool("SMARTPAD_INPUT_CONTROL", True))
    # Load the local handwriting model at startup (in the background).
    preload_ocr: bool = field(default_factory=lambda: _bool("SMARTPAD_OCR_PRELOAD", True))
    # Fixed pairing token (optional). If empty a random one is generated per run.
    pairing_token: str = field(default_factory=lambda: os.environ.get("SMARTPAD_TOKEN", ""))


settings = Settings()

for d in (DATA_DIR, DOCS_DIR, EXPORT_DIR):
    d.mkdir(parents=True, exist_ok=True)

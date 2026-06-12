"""Load config.yaml + .env into one settings object."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_settings(config_path: Path | None = None) -> dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env")
    path = config_path or PROJECT_ROOT / "config.yaml"
    with open(path) as f:
        cfg = yaml.safe_load(f)

    cfg["data_dir"] = str((PROJECT_ROOT / cfg.get("data_dir", "data")).resolve())

    # .env overrides for the signal engine
    if os.getenv("BETFAIR_COMMISSION"):
        cfg["signals"]["betfair_commission"] = float(os.environ["BETFAIR_COMMISSION"])
    if os.getenv("MIN_EDGE"):
        cfg["signals"]["min_edge"] = float(os.environ["MIN_EDGE"])

    cfg["betfair"] = {
        "app_key": os.getenv("BETFAIR_APP_KEY", ""),
        "session_token": os.getenv("BETFAIR_SESSION_TOKEN", ""),
        "username": os.getenv("BETFAIR_USERNAME", ""),
        "password": os.getenv("BETFAIR_PASSWORD", ""),
        "cert_file": os.getenv("BETFAIR_CERT_FILE", ""),
        "key_file": os.getenv("BETFAIR_KEY_FILE", ""),
    }
    cfg["anthropic_api_key"] = os.getenv("ANTHROPIC_API_KEY", "")

    # execution: armed only when BOTH config and env say so
    cfg.setdefault("execution", {})
    if os.getenv("EXECUTOR_ARMED", "").lower() not in ("1", "true", "yes"):
        cfg["execution"]["armed"] = False
    cfg["poly_private_key"] = os.getenv("POLY_PRIVATE_KEY", "")
    cfg["poly_funder"] = os.getenv("POLY_FUNDER", "")
    return cfg

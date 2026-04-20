"""Canonical filesystem paths. Override NSE_UNIVERSE_DATA_DIR to relocate."""
from __future__ import annotations

import os
from pathlib import Path


def _env_dir(var: str, default: Path) -> Path:
    val = os.environ.get(var)
    return Path(val).expanduser().resolve() if val else default


REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DATA_DIR: Path = _env_dir("NSE_UNIVERSE_DATA_DIR", REPO_ROOT / "data")

RAW_DIR: Path = DATA_DIR / "raw"
PARQUET_DIR: Path = DATA_DIR / "parquet"
ACTIONS_DIR: Path = DATA_DIR / "actions"
DB_DIR: Path = DATA_DIR / "db"
QUARANTINE_DIR: Path = RAW_DIR / "_quarantine"

DB_PATH: Path = DB_DIR / "universe.duckdb"
STATE_PATH: Path = DATA_DIR / "state.json"
INDICES_CONFIG: Path = REPO_ROOT / "config" / "indices.yml"


def ensure_dirs() -> None:
    for p in (RAW_DIR, PARQUET_DIR, ACTIONS_DIR, DB_DIR, QUARANTINE_DIR):
        p.mkdir(parents=True, exist_ok=True)

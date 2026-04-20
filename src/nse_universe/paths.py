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
# Default to shipped config/indices.yml next to the repo root. Consumers
# that install this package as a dep (outside the repo layout) can point
# NSE_UNIVERSE_INDICES_CONFIG at any YAML to use their own rank windows.
_DEFAULT_INDICES = REPO_ROOT / "config" / "indices.yml"
INDICES_CONFIG: Path = Path(
    os.environ.get("NSE_UNIVERSE_INDICES_CONFIG", str(_DEFAULT_INDICES))
).expanduser()


def ensure_dirs() -> None:
    for p in (RAW_DIR, PARQUET_DIR, ACTIONS_DIR, DB_DIR, QUARANTINE_DIR):
        p.mkdir(parents=True, exist_ok=True)

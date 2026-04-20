"""Lightweight JSON state file — committed to git, tracks high-level progress.

Most state lives in DuckDB (fetch_log, non_trading_days). This file holds only
what is useful *before* the DB is opened: config version, last sync attempt,
user-tweakable toggles.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from nse_universe.paths import STATE_PATH


@dataclass
class State:
    schema_version: int = 1
    last_sync_attempted_at: str | None = None
    last_sync_completed_at: str | None = None
    last_rank_computed_at: str | None = None
    last_actions_refreshed_at: str | None = None
    history_start: str = "2005-01-01"
    notes: list[str] = field(default_factory=list)


def load(path: Path | None = None) -> State:
    p = path or STATE_PATH
    if not p.exists():
        return State()
    with p.open() as f:
        data = json.load(f)
    return State(**{k: v for k, v in data.items() if k in State.__dataclass_fields__})


def save(state: State, path: Path | None = None) -> None:
    p = path or STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(asdict(state), f, indent=2, sort_keys=True)
    tmp.replace(p)


def mark_sync_attempt() -> None:
    s = load()
    s.last_sync_attempted_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    save(s)


def mark_sync_complete() -> None:
    s = load()
    s.last_sync_completed_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    save(s)


def mark_rank_computed() -> None:
    s = load()
    s.last_rank_computed_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    save(s)


def mark_actions_refreshed() -> None:
    s = load()
    s.last_actions_refreshed_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    save(s)

"""Index definitions — loaded from config/indices.yml."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from nse_universe.paths import INDICES_CONFIG


@dataclass(frozen=True)
class IndexSpec:
    name: str
    rank_lo: int  # inclusive, 1-indexed
    rank_hi: int  # inclusive
    description: str = ""

    def contains(self, rank: int) -> bool:
        return self.rank_lo <= rank <= self.rank_hi


def load_indices(path: Path | None = None) -> Mapping[str, IndexSpec]:
    p = path or INDICES_CONFIG
    with p.open() as f:
        raw = yaml.safe_load(f)
    out: dict[str, IndexSpec] = {}
    for name, spec in (raw.get("indices") or {}).items():
        lo, hi = spec["rank_range"]
        if not (isinstance(lo, int) and isinstance(hi, int) and 1 <= lo <= hi):
            raise ValueError(f"indices.yml: bad rank_range for {name}: {spec['rank_range']!r}")
        out[name] = IndexSpec(name=name, rank_lo=lo, rank_hi=hi, description=spec.get("description", ""))
    if not out:
        raise ValueError("indices.yml contains no indices")
    return out

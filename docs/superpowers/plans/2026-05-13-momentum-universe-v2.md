# Momentum Universe v2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a momentum-strategy-grade universe ("v2") in the nse500 data layer (filter stack + GSM/ASM surveillance tracking + behavioral proxy), wire it into the existing fortress `dual_momentum` strategy as the input pool, validate via 13-yr phase backtest (auto-tune if regression), and add a simple `top_n_rank` benchmark strategy as a control.

**Architecture:** v2 is added **alongside** v1 — both `universe_rank` (v1) and `universe_v2` tables co-exist, both `Universe(version="v1"|"v2")` resolve correctly, both export parquet. fortress gets a `universe.version` config knob defaulting to `v1`; flipping to `v2` swaps which table feeds `Universe._load_members`. Strategy logic untouched. Success oracle: the 13-yr phase backtest (CLI Option 8) must equal-or-beat the +24.1% CAGR / 1.02 Sharpe / −28.1% MaxDD baseline; if it doesn't, auto-tune filter thresholds and re-test until it does.

**Tech stack:** Python 3.12, DuckDB, polars/pandas, pyarrow/parquet, curl_cffi (NSE anti-bot), pytest, uv. Two repos:
- `/home/rakesh/work/nse500` — data layer (v2 universe, surveillance ingest)
- `/home/rakesh/work/momentum-trading-strategy` — strategy layer (consumes nse500 as a library; backtest validation)

**Hard constraints:**
- v1 (`universe_rank`) untouched — other consumer repos depend on it
- Point-in-time correctness: every filter computes strictly from data with `date < as_of_date`
- 2005-01-01 → today coverage for v2 backfill
- Fortress 13-yr phase backtest baseline must hold (or be tuned to hold)
- Frequent commits; never `--no-verify`; never `git push` without explicit ask

**Decision parameters locked at design time:**
- AUM target: ₹20–80L positional cash-only
- ADV floor (60d median turnover): **₹50L/day** (max-breadth setting)
- 126d median turnover floor: ₹25L
- Min trading-day history: 252
- Min traded-pct (last 60d): 95%
- Min close on as_of_date: ₹50
- Max CV of turnover (126d): 3.0
- Max circuit-hit ratio (60d): 5%
- GSM threshold (real or proxy): stage ≥ 2 → exclude
- ASM threshold (long-term): stage ≥ 3 → exclude
- Volatility ceiling: knob exists, default OFF
- Backfill range: 2005-01-01 → today, monthly first-trading-day cadence
- F&O overlay: dropped (user is positional cash trader)
- Rank within survivors: by `med_turnover_126d` desc, top 1000

---

## File map

### nse500 (`/home/rakesh/work/nse500`)

**Create:**
- `src/nse_universe/rank/filters.py` — Pure SQL/Python filter primitives (turnover stats, traded-pct, CV, circuit-hit ratio, vol, behavioral GSM proxy heuristic)
- `src/nse_universe/rank/v2.py` — v2 universe builder (parallel to `monthly.py`); orchestrates filter computation, surveillance lookup, ranking, upsert
- `src/nse_universe/fetch/surveillance.py` — NSE GSM/ASM HTTP scraper (reuses `fetch/session.py`)
- `src/nse_universe/ingest/__init__.py` — empty marker
- `src/nse_universe/ingest/surveillance.py` — Parse scraped GSM/ASM JSON/HTML into `surveillance_daily`
- `tests/test_filters.py`
- `tests/test_v2_rank.py`
- `tests/test_surveillance.py`
- `tests/test_v2_universe_api.py`

**Modify:**
- `src/nse_universe/core/db.py` — Add `universe_v2` + `surveillance_daily` tables to `_INTERNAL_DDL`; bump `SCHEMA_VERSION` to 2
- `src/nse_universe/core/universe.py` — Add `version` param to `universe_at()`, `members()`, `walk()`; route to correct table; expose `Universe.version` attribute
- `src/nse_universe/core/export.py` — Export `universe_v2.parquet` + `surveillance_daily.parquet`; update `import_all_if_missing` accordingly
- `src/nse_universe/cli/menu.py` — Add menu items: "Rebuild universe v2 (filter-stack)", "Refresh surveillance feed"

### fortress (`/home/rakesh/work/momentum-trading-strategy`)

**Create:**
- `fortress/strategy/simple_topn.py` — `SimpleTopNStrategy` benchmark plugin (~150 LOC)
- `tests/test_simple_topn.py`
- `tests/test_universe_v2_loader.py`

**Modify:**
- `fortress/universe.py` — Accept `version` param; pass to `_nse_universe_singleton().universe_at(d, version=...)`; default v1
- `fortress/config.py` — Add `universe.version` config field (default `"v1"`); validate value in `{"v1","v2"}`
- `fortress/strategy/__init__.py` — Import `simple_topn` at module load to trigger registration
- `config.yaml` — Add `universe.version: "v1"` line under existing `universe:` block; document v2 option in comments

---

## Behavioral GSM/ASM proxy heuristic (locked spec)

Computed from `bhav_daily` alone, no external dep. For each (symbol, as_of_date), look at last 60 trading days strictly before as_of_date:

| Condition (any of) | Points |
|---|---|
| Circuit-hit ratio > 30% | +1 |
| ≥ 5 consecutive lower-circuit days in window | +1 |
| 60d annualized vol > 90% AND drawdown from 60d-high > 50% | +1 |
| Median turnover dropped > 70% comparing last 30d vs prior 30d | +1 |

Stage mapping:
- Sum 0 → stage 0 (clean)
- Sum 1 → stage 1 (warning, not excluded)
- Sum 2 → stage 2 (excluded under GSM ≥ 2 rule)
- Sum 3+ → stage 3 (excluded)

Stored in `surveillance_daily` with `source = 'behavioral_proxy'`. Real NSE-feed data with `source = 'nse_live'` takes precedence when both exist for a given (date, symbol).

Circuit-hit detection: a (symbol, date) bhav row is a circuit hit if `high == low AND volume > 0 AND prev_close IS NOT NULL AND close != prev_close`. Direction inferred from `close > prev_close` (upper) vs `<` (lower).

---

## Tasks

### Task 1: Add `universe_v2` + `surveillance_daily` schemas

**Files:**
- Modify: `/home/rakesh/work/nse500/src/nse_universe/core/db.py`
- Test: `/home/rakesh/work/nse500/tests/test_v2_schema.py`

- [ ] **Step 1: Write failing schema test**

Create `tests/test_v2_schema.py`:

```python
from nse_universe.core.db import db


def test_universe_v2_table_columns():
    expected = {
        "as_of_date", "symbol", "rank", "passes",
        "med_turnover_60d", "med_turnover_126d",
        "traded_pct_60d", "trading_days_history",
        "close_asof", "cv_turnover_126d", "circuit_pct_60d",
        "gsm_stage", "asm_stage", "vol_annualized_60d",
        "exclude_reason",
    }
    with db() as con:
        rows = con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'universe_v2'"
        ).fetchall()
    cols = {r[0] for r in rows}
    missing = expected - cols
    assert not missing, f"universe_v2 missing columns: {missing}"


def test_surveillance_daily_table_columns():
    expected = {"date", "symbol", "gsm_stage", "asm_stage", "source"}
    with db() as con:
        rows = con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'surveillance_daily'"
        ).fetchall()
    cols = {r[0] for r in rows}
    missing = expected - cols
    assert not missing, f"surveillance_daily missing columns: {missing}"


def test_schema_version_bumped():
    with db() as con:
        v = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert v is not None and int(v[0]) >= 2
```

- [ ] **Step 2: Run test, verify it fails**

```bash
cd /home/rakesh/work/nse500 && uv run pytest tests/test_v2_schema.py -v
```
Expected: FAIL (no `universe_v2` / `surveillance_daily` tables).

- [ ] **Step 3: Add DDL to `db.py`**

Edit `src/nse_universe/core/db.py`:

1. Bump `SCHEMA_VERSION = 1` to `SCHEMA_VERSION = 2`.
2. Append two DDL entries to `_INTERNAL_DDL` (before the closing `)`):

```python
    """
    CREATE TABLE IF NOT EXISTS universe_v2 (
        as_of_date           DATE    NOT NULL,
        symbol               VARCHAR NOT NULL,
        rank                 INTEGER NOT NULL,
        passes               BOOLEAN NOT NULL,
        med_turnover_60d     DOUBLE,
        med_turnover_126d    DOUBLE,
        traded_pct_60d       DOUBLE,
        trading_days_history INTEGER,
        close_asof           DOUBLE,
        cv_turnover_126d     DOUBLE,
        circuit_pct_60d      DOUBLE,
        gsm_stage            INTEGER,
        asm_stage            INTEGER,
        vol_annualized_60d   DOUBLE,
        exclude_reason       VARCHAR,
        PRIMARY KEY (as_of_date, symbol)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_universe_v2_passes
        ON universe_v2(as_of_date, passes)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_universe_v2_rank
        ON universe_v2(as_of_date, rank)
    """,
    """
    CREATE TABLE IF NOT EXISTS surveillance_daily (
        date       DATE    NOT NULL,
        symbol     VARCHAR NOT NULL,
        gsm_stage  INTEGER,
        asm_stage  INTEGER,
        source     VARCHAR NOT NULL,    -- 'nse_live' | 'behavioral_proxy'
        PRIMARY KEY (date, symbol, source)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_surv_date ON surveillance_daily(date)
    """,
```

- [ ] **Step 4: Run test, verify it passes**

```bash
cd /home/rakesh/work/nse500 && uv run pytest tests/test_v2_schema.py -v
```
Expected: PASS (all 3 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/rakesh/work/nse500
git add src/nse_universe/core/db.py tests/test_v2_schema.py
git commit -m "feat(v2): add universe_v2 + surveillance_daily schemas"
```

---

### Task 2: Implement filter primitives (`filters.py`)

**Files:**
- Create: `/home/rakesh/work/nse500/src/nse_universe/rank/filters.py`
- Create: `/home/rakesh/work/nse500/tests/test_filters.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_filters.py`:

```python
from datetime import date

import pytest

from nse_universe.core.db import db
from nse_universe.rank.filters import (
    compute_per_symbol_filter_metrics,
    behavioral_surveillance_stage,
)


@pytest.fixture
def synthetic_bhav(tmp_path, monkeypatch):
    """Insert synthetic bhav rows directly into a fresh DuckDB.

    Bypasses the partitioned-parquet view by inserting into a temp table
    and CREATE VIEW-ing bhav_daily over it.
    """
    monkeypatch.setenv("NSE_DUCKDB_PATH", str(tmp_path / "test.duckdb"))
    from nse_universe import paths as _paths
    monkeypatch.setattr(_paths, "DB_PATH", tmp_path / "test.duckdb")
    monkeypatch.setattr(_paths, "PARQUET_DIR", tmp_path / "parquet")

    with db() as con:
        con.execute("DROP VIEW IF EXISTS bhav_daily")
        con.execute("""
            CREATE TABLE bhav_daily_test (
                symbol VARCHAR, date DATE,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
                prev_close DOUBLE, volume BIGINT, turnover DOUBLE,
                trades INTEGER, year INTEGER, month INTEGER
            )
        """)
        con.execute("CREATE VIEW bhav_daily AS SELECT * FROM bhav_daily_test")
    return tmp_path


def _insert_bhav(rows):
    """rows: list of dicts with at least symbol/date/close/prev_close/turnover."""
    with db() as con:
        for r in rows:
            con.execute(
                """INSERT INTO bhav_daily_test VALUES
                   (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    r["symbol"], r["date"],
                    r.get("open", r["close"]), r.get("high", r["close"]),
                    r.get("low", r["close"]), r["close"],
                    r.get("prev_close", r["close"]),
                    r.get("volume", 1000), r.get("turnover", 1_00_000),
                    r.get("trades", 10), r["date"].year, r["date"].month,
                ],
            )


def test_metrics_for_liquid_symbol(synthetic_bhav):
    """A symbol trading every day with steady turnover passes all filters."""
    from datetime import timedelta
    base = date(2024, 1, 1)
    rows = []
    for i in range(252):
        d = base + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        rows.append({
            "symbol": "LIQUID", "date": d,
            "close": 200.0, "prev_close": 200.0,
            "turnover": 5_00_00_000,  # ₹5 Cr
        })
    _insert_bhav(rows)
    as_of = max(r["date"] for r in rows)
    metrics = compute_per_symbol_filter_metrics(as_of_date=as_of, symbols=["LIQUID"])
    m = metrics["LIQUID"]
    assert m["trading_days_history"] >= 126
    assert m["med_turnover_60d"] == pytest.approx(5_00_00_000)
    assert m["traded_pct_60d"] == pytest.approx(1.0)
    assert m["close_asof"] == pytest.approx(200.0)
    assert m["cv_turnover_126d"] < 0.1  # zero variance


def test_circuit_hit_detection(synthetic_bhav):
    """A symbol that hit upper circuit on 50% of days has circuit_pct_60d ≈ 0.5."""
    from datetime import timedelta
    base = date(2024, 6, 1)
    rows = []
    for i in range(60):
        d = base + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        is_circuit = (i % 2 == 0)
        close = 105.0 if is_circuit else 100.0
        prev = 100.0
        high_low = close if is_circuit else 102.0
        rows.append({
            "symbol": "CIRCUIT", "date": d,
            "close": close, "prev_close": prev,
            "high": high_low if is_circuit else 102.0,
            "low":  high_low if is_circuit else 99.0,
            "turnover": 1_00_00_000,
        })
    _insert_bhav(rows)
    as_of = max(r["date"] for r in rows)
    metrics = compute_per_symbol_filter_metrics(as_of_date=as_of, symbols=["CIRCUIT"])
    assert metrics["CIRCUIT"]["circuit_pct_60d"] == pytest.approx(0.5, abs=0.1)


def test_behavioral_stage_clean_symbol(synthetic_bhav):
    """A boring liquid symbol gets stage 0."""
    from datetime import timedelta
    base = date(2024, 1, 1)
    rows = []
    for i in range(252):
        d = base + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        rows.append({
            "symbol": "CLEAN", "date": d,
            "close": 200.0, "prev_close": 200.0,
            "turnover": 5_00_00_000,
        })
    _insert_bhav(rows)
    as_of = max(r["date"] for r in rows)
    stages = behavioral_surveillance_stage(as_of_date=as_of, symbols=["CLEAN"])
    assert stages["CLEAN"] == 0
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
cd /home/rakesh/work/nse500 && uv run pytest tests/test_filters.py -v
```
Expected: FAIL (module doesn't exist).

- [ ] **Step 3: Implement `filters.py`**

Create `src/nse_universe/rank/filters.py`:

```python
"""Filter primitives for momentum universe v2.

Pure SQL-driven metric computation. All windows are strictly point-in-time:
data with date < as_of_date only. Compute once per (as_of_date, symbol set)
and let v2.py orchestrate filter application.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from nse_universe.core.db import db


WINDOW_60D_CAL = 95   # ~60 trading days backed by ≤95 calendar days
WINDOW_126D_CAL = 200
WINDOW_252D_CAL = 380


def compute_per_symbol_filter_metrics(
    *, as_of_date: date, symbols: Iterable[str] | None = None
) -> dict[str, dict]:
    """Return {symbol: {metric_name: value}} for every symbol with any history.

    If `symbols` is provided, restrict to that set; otherwise compute for all
    symbols active in bhav_daily strictly before as_of_date.

    Metrics computed (all point-in-time, data with date < as_of_date):
      - med_turnover_60d, med_turnover_126d
      - traded_pct_60d  (count of traded days / 60)
      - trading_days_history  (count of all-time trading days before as_of_date)
      - close_asof  (most recent close ≤ as_of_date)
      - cv_turnover_126d  (stddev / mean of daily turnover over last 126d)
      - circuit_pct_60d  (fraction of last 60d that were single-print circuits)
      - vol_annualized_60d  (stdev of log returns over 60d × sqrt(252))
    """
    window_60 = as_of_date - timedelta(days=WINDOW_60D_CAL)
    window_126 = as_of_date - timedelta(days=WINDOW_126D_CAL)

    sym_clause = ""
    params = [as_of_date, as_of_date, window_126, as_of_date]
    if symbols is not None:
        sym_list = list(symbols)
        placeholders = ",".join(["?"] * len(sym_list))
        sym_clause = f" AND symbol IN ({placeholders})"
        params.extend(sym_list)

    sql = f"""
        WITH bhav_window AS (
            SELECT symbol, date, close, prev_close, high, low, volume, turnover,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn_desc
              FROM bhav_daily
             WHERE date < ?{sym_clause.replace('?', '?', 0)}
        ),
        last60 AS (
            SELECT * FROM bhav_window WHERE rn_desc <= 60
        ),
        last126 AS (
            SELECT * FROM bhav_window WHERE rn_desc <= 126
        ),
        traded60 AS (
            SELECT symbol,
                   COUNT(*)                                AS days_traded_60,
                   MEDIAN(turnover)                        AS med_turnover_60d,
                   AVG(LN(close / NULLIF(prev_close, 0)))  AS avg_log_ret_60d,
                   STDDEV_POP(LN(close / NULLIF(prev_close, 0)))
                     AS sd_log_ret_60d,
                   SUM(CASE
                         WHEN high = low AND volume > 0
                          AND prev_close IS NOT NULL
                          AND close <> prev_close
                       THEN 1 ELSE 0 END)                  AS circuit_days_60
              FROM last60
             GROUP BY symbol
        ),
        traded126 AS (
            SELECT symbol,
                   MEDIAN(turnover)                        AS med_turnover_126d,
                   AVG(turnover)                           AS mean_turnover_126d,
                   STDDEV_POP(turnover)                    AS sd_turnover_126d,
                   COUNT(*)                                AS days_traded_126
              FROM last126
             GROUP BY symbol
        ),
        history AS (
            SELECT symbol, COUNT(*) AS trading_days_history
              FROM bhav_daily
             WHERE date < ?{sym_clause}
             GROUP BY symbol
        ),
        latest_close AS (
            SELECT symbol, close AS close_asof
              FROM bhav_window
             WHERE rn_desc = 1
        )
        SELECT h.symbol,
               h.trading_days_history,
               COALESCE(t60.days_traded_60, 0)            AS days_traded_60,
               t60.med_turnover_60d,
               t126.med_turnover_126d,
               CASE WHEN t126.mean_turnover_126d > 0
                    THEN t126.sd_turnover_126d / t126.mean_turnover_126d
                    ELSE NULL END                          AS cv_turnover_126d,
               COALESCE(t60.circuit_days_60, 0)           AS circuit_days_60,
               COALESCE(t60.sd_log_ret_60d, 0) * SQRT(252) AS vol_annualized_60d,
               lc.close_asof
          FROM history h
          LEFT JOIN traded60     t60  ON t60.symbol  = h.symbol
          LEFT JOIN traded126    t126 ON t126.symbol = h.symbol
          LEFT JOIN latest_close lc   ON lc.symbol   = h.symbol
    """

    # ----- second sym_clause copy fix -----
    # The history CTE uses its own sym_clause occurrence; build params carefully:
    final_params = [as_of_date]
    if symbols is not None:
        final_params.extend(sym_list)
    final_params.append(as_of_date)
    if symbols is not None:
        final_params.extend(sym_list)

    with db(read_only=True) as con:
        rows = con.execute(sql, final_params).fetchall()

    out: dict[str, dict] = {}
    for r in rows:
        (sym, hist, days60, med60, med126, cv, circ_d, vol_a, close_a) = r
        traded_pct = (days60 / 60.0) if days60 is not None else 0.0
        circuit_pct = (circ_d / max(days60, 1)) if days60 else 0.0
        out[sym] = {
            "trading_days_history": int(hist),
            "med_turnover_60d":  float(med60) if med60 is not None else None,
            "med_turnover_126d": float(med126) if med126 is not None else None,
            "traded_pct_60d":    float(traded_pct),
            "cv_turnover_126d":  float(cv) if cv is not None else None,
            "circuit_pct_60d":   float(circuit_pct),
            "vol_annualized_60d": float(vol_a) if vol_a is not None else None,
            "close_asof":        float(close_a) if close_a is not None else None,
        }
    return out


def behavioral_surveillance_stage(
    *, as_of_date: date, symbols: Iterable[str] | None = None
) -> dict[str, int]:
    """Heuristic GSM/ASM proxy from bhav_daily only.

    Stage = sum of (up to 4) red flags over last 60 trading days:
      a) circuit_pct_60d > 30%
      b) ≥ 5 consecutive lower-circuit days
      c) vol_annualized_60d > 90% AND drawdown_from_60d_high > 50%
      d) median turnover dropped > 70% comparing last 30d vs prior 30d
    """
    window_60 = as_of_date - timedelta(days=WINDOW_60D_CAL)

    sym_clause = ""
    params_a = [as_of_date]
    if symbols is not None:
        sym_list = list(symbols)
        placeholders = ",".join(["?"] * len(sym_list))
        sym_clause = f" AND symbol IN ({placeholders})"
        params_a.extend(sym_list)

    sql = f"""
        WITH bhav_window AS (
            SELECT symbol, date, close, prev_close, high, low, volume, turnover,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn_desc
              FROM bhav_daily
             WHERE date < ?{sym_clause}
        ),
        last60 AS (
            SELECT *,
                   (high = low AND volume > 0
                    AND prev_close IS NOT NULL
                    AND close < prev_close) AS is_lower_circuit
              FROM bhav_window
             WHERE rn_desc <= 60
        ),
        m AS (
            SELECT symbol,
                   COUNT(*)::DOUBLE                            AS n_days,
                   SUM(CASE WHEN is_lower_circuit THEN 1 ELSE 0 END)
                                                                AS lower_days,
                   SUM(CASE
                         WHEN high = low AND volume > 0
                          AND prev_close IS NOT NULL
                          AND close <> prev_close
                       THEN 1 ELSE 0 END)::DOUBLE              AS circuit_days,
                   STDDEV_POP(LN(close / NULLIF(prev_close, 0))) * SQRT(252)
                                                                AS vol_a,
                   MAX(close)                                   AS hi60,
                   (SELECT close FROM last60 lx
                     WHERE lx.symbol = last60.symbol AND lx.rn_desc = 1
                    LIMIT 1)                                    AS close_last,
                   MEDIAN(CASE WHEN rn_desc <= 30 THEN turnover END)
                                                                AS med_t_30_recent,
                   MEDIAN(CASE WHEN rn_desc > 30 AND rn_desc <= 60 THEN turnover END)
                                                                AS med_t_30_prior
              FROM last60
             GROUP BY symbol
        )
        SELECT symbol, n_days, circuit_days, lower_days, vol_a, hi60, close_last,
               med_t_30_recent, med_t_30_prior
          FROM m
    """

    with db(read_only=True) as con:
        rows = con.execute(sql, params_a).fetchall()

    out: dict[str, int] = {}
    for r in rows:
        (sym, n_days, circuit_d, lower_d, vol_a, hi60, close_last, med_recent, med_prior) = r
        n_days = float(n_days or 0)
        if n_days < 1:
            out[sym] = 0
            continue
        circuit_pct = (circuit_d or 0) / n_days
        dd = (1.0 - (close_last / hi60)) if (hi60 and close_last) else 0.0
        stage = 0
        if circuit_pct > 0.30:
            stage += 1
        if (lower_d or 0) >= 5:
            stage += 1
        if (vol_a or 0) > 0.90 and dd > 0.50:
            stage += 1
        if med_prior and med_prior > 0 and (med_recent or 0) / med_prior < 0.30:
            stage += 1
        out[sym] = min(stage, 3)
    return out
```

Note: the SQL above uses `sym_clause` twice; ensure both substitutions are textually identical. The `f"...{sym_clause}..."` interpolation handles this.

- [ ] **Step 4: Run tests, verify they pass**

```bash
cd /home/rakesh/work/nse500 && uv run pytest tests/test_filters.py -v
```
Expected: PASS (all 3 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/rakesh/work/nse500
git add src/nse_universe/rank/filters.py tests/test_filters.py
git commit -m "feat(v2): filter primitives + behavioral surveillance proxy"
```

---

### Task 3: Implement v2 universe builder (`v2.py`)

**Files:**
- Create: `/home/rakesh/work/nse500/src/nse_universe/rank/v2.py`
- Create: `/home/rakesh/work/nse500/tests/test_v2_rank.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_v2_rank.py`:

```python
from datetime import date, timedelta

import pytest

from nse_universe.core.db import db
from nse_universe.rank.v2 import (
    DEFAULT_V2_CONFIG, V2Config, recompute_v2_for, recompute_v2_all,
)


def _seed_year_of_bhav(symbol, *, base_close, base_turnover, base_date, n_days=300):
    rows = []
    d = base_date
    while len(rows) < n_days:
        if d.weekday() < 5:
            rows.append((symbol, d, base_close, base_close, base_close,
                         base_close, base_close, 1000, base_turnover, 10,
                         d.year, d.month))
        d += timedelta(days=1)
    return rows


def test_recompute_v2_clean_symbol_passes(tmp_path, monkeypatch):
    monkeypatch.setattr("nse_universe.paths.DB_PATH", tmp_path / "t.duckdb")
    monkeypatch.setattr("nse_universe.paths.PARQUET_DIR", tmp_path / "p")
    with db() as con:
        con.execute("DROP VIEW IF EXISTS bhav_daily")
        con.execute("""CREATE TABLE bhav_daily_test (
            symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, prev_close DOUBLE, volume BIGINT, turnover DOUBLE,
            trades INTEGER, year INTEGER, month INTEGER
        )""")
        con.execute("CREATE VIEW bhav_daily AS SELECT * FROM bhav_daily_test")
        rows = _seed_year_of_bhav("CLEAN", base_close=300.0,
                                   base_turnover=2_00_00_000,
                                   base_date=date(2023, 1, 2))
        con.executemany("INSERT INTO bhav_daily_test VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    as_of = date(2024, 1, 1)
    n = recompute_v2_for(as_of, DEFAULT_V2_CONFIG)
    assert n >= 1
    with db(read_only=True) as con:
        row = con.execute(
            "SELECT passes, exclude_reason FROM universe_v2 WHERE symbol='CLEAN' AND as_of_date=?",
            [as_of],
        ).fetchone()
    assert row is not None
    assert row[0] is True, f"CLEAN should pass; got reason={row[1]}"


def test_low_turnover_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr("nse_universe.paths.DB_PATH", tmp_path / "t.duckdb")
    monkeypatch.setattr("nse_universe.paths.PARQUET_DIR", tmp_path / "p")
    with db() as con:
        con.execute("DROP VIEW IF EXISTS bhav_daily")
        con.execute("""CREATE TABLE bhav_daily_test (
            symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, prev_close DOUBLE, volume BIGINT, turnover DOUBLE,
            trades INTEGER, year INTEGER, month INTEGER
        )""")
        con.execute("CREATE VIEW bhav_daily AS SELECT * FROM bhav_daily_test")
        rows = _seed_year_of_bhav("THIN", base_close=300.0,
                                   base_turnover=10_00_000,  # ₹10L only
                                   base_date=date(2023, 1, 2))
        con.executemany("INSERT INTO bhav_daily_test VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    as_of = date(2024, 1, 1)
    recompute_v2_for(as_of, DEFAULT_V2_CONFIG)
    with db(read_only=True) as con:
        row = con.execute(
            "SELECT passes, exclude_reason FROM universe_v2 WHERE symbol='THIN' AND as_of_date=?",
            [as_of],
        ).fetchone()
    assert row is not None
    assert row[0] is False
    assert "turnover" in row[1].lower()
```

- [ ] **Step 2: Run, verify it fails**

```bash
cd /home/rakesh/work/nse500 && uv run pytest tests/test_v2_rank.py -v
```
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `v2.py`**

Create `src/nse_universe/rank/v2.py`:

```python
"""Momentum universe v2 builder.

Parallel to monthly.py but heavier:
  1. Compute per-symbol filter metrics over the lookback windows.
  2. Compute behavioral surveillance proxy stages.
  3. Look up real GSM/ASM stages from `surveillance_daily` (overrides proxy).
  4. Apply filter stack; record exclude reason for fail cases.
  5. Rank survivors by med_turnover_126d desc; keep top K.
  6. Upsert into `universe_v2`.

All filter thresholds live in V2Config so a future tuning sweep is trivial.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from nse_universe.core.db import db
from nse_universe.core.export import export_all
from nse_universe.rank.filters import (
    behavioral_surveillance_stage,
    compute_per_symbol_filter_metrics,
)

log = logging.getLogger(__name__)

LOOKBACK_TRADING_DAYS = 252  # min listing age
TOP_K = 1000


@dataclass(frozen=True)
class V2Config:
    min_trading_days: int = 252
    min_traded_pct_60d: float = 0.95
    min_med_turnover_60d: float = 50_00_000   # ₹50L
    min_med_turnover_126d: float = 25_00_000  # ₹25L
    min_close: float = 50.0
    max_cv_turnover_126d: float = 3.0
    max_circuit_pct_60d: float = 0.05
    max_gsm_stage: int = 1  # exclude if > this (i.e. stage ≥ 2)
    max_asm_stage: int = 2  # exclude if > this (i.e. stage ≥ 3)
    vol_ceiling: float | None = None  # set to e.g. 0.90 to enable
    top_k: int = TOP_K


DEFAULT_V2_CONFIG = V2Config()


def _exclude_reason(m: dict, gsm: int, asm: int, cfg: V2Config) -> str | None:
    """Return reason string if any filter fails; else None."""
    if (m.get("trading_days_history") or 0) < cfg.min_trading_days:
        return f"history<{cfg.min_trading_days}d"
    if (m.get("traded_pct_60d") or 0.0) < cfg.min_traded_pct_60d:
        return f"traded_pct_60d<{cfg.min_traded_pct_60d}"
    if (m.get("med_turnover_60d") or 0.0) < cfg.min_med_turnover_60d:
        return f"med_turnover_60d<{cfg.min_med_turnover_60d:.0f}"
    if (m.get("med_turnover_126d") or 0.0) < cfg.min_med_turnover_126d:
        return f"med_turnover_126d<{cfg.min_med_turnover_126d:.0f}"
    if (m.get("close_asof") or 0.0) < cfg.min_close:
        return f"close_asof<{cfg.min_close}"
    cv = m.get("cv_turnover_126d")
    if cv is not None and cv > cfg.max_cv_turnover_126d:
        return f"cv_turnover_126d>{cfg.max_cv_turnover_126d}"
    if (m.get("circuit_pct_60d") or 0.0) > cfg.max_circuit_pct_60d:
        return f"circuit_pct_60d>{cfg.max_circuit_pct_60d}"
    if gsm > cfg.max_gsm_stage:
        return f"gsm_stage>{cfg.max_gsm_stage}"
    if asm > cfg.max_asm_stage:
        return f"asm_stage>{cfg.max_asm_stage}"
    if cfg.vol_ceiling is not None:
        va = m.get("vol_annualized_60d") or 0.0
        if va > cfg.vol_ceiling:
            return f"vol_annualized_60d>{cfg.vol_ceiling}"
    return None


def _gsm_asm_for(con, as_of_date: date) -> dict[str, tuple[int, int]]:
    """Return {symbol: (gsm_stage, asm_stage)} as of as_of_date.

    Prefers `source='nse_live'` over `source='behavioral_proxy'` when both exist.
    Uses the most recent record on or before as_of_date.
    """
    rows = con.execute(
        """
        WITH ranked AS (
            SELECT symbol, gsm_stage, asm_stage, source,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol
                       ORDER BY CASE source WHEN 'nse_live' THEN 0 ELSE 1 END,
                                date DESC
                   ) AS rn
              FROM surveillance_daily
             WHERE date <= ?
        )
        SELECT symbol, gsm_stage, asm_stage FROM ranked WHERE rn = 1
        """,
        [as_of_date],
    ).fetchall()
    return {sym: (int(g or 0), int(a or 0)) for (sym, g, a) in rows}


def recompute_v2_for(as_of_date: date, cfg: V2Config = DEFAULT_V2_CONFIG) -> int:
    """Compute + upsert universe_v2 for one as_of_date. Returns rows written."""
    # 1. metrics over the universe of all symbols with any history before as_of_date
    metrics = compute_per_symbol_filter_metrics(as_of_date=as_of_date)
    # 2. behavioral proxy as fallback; upsert into surveillance_daily so the
    #    audit trail is preserved (and so `_gsm_asm_for` returns it).
    proxy = behavioral_surveillance_stage(as_of_date=as_of_date,
                                          symbols=list(metrics.keys()))
    with db() as con:
        for sym, stage in proxy.items():
            con.execute(
                """INSERT OR REPLACE INTO surveillance_daily
                          (date, symbol, gsm_stage, asm_stage, source)
                   VALUES (?, ?, ?, NULL, 'behavioral_proxy')""",
                [as_of_date, sym, stage],
            )
        gsm_asm = _gsm_asm_for(con, as_of_date)

        # 3. apply filter stack
        scored: list[tuple[str, dict, int, int, str | None]] = []
        for sym, m in metrics.items():
            g, a = gsm_asm.get(sym, (0, 0))
            reason = _exclude_reason(m, g, a, cfg)
            scored.append((sym, m, g, a, reason))

        # 4. rank survivors by med_turnover_126d desc, keep top K
        survivors = [s for s in scored if s[4] is None]
        survivors.sort(
            key=lambda s: (-(s[1].get("med_turnover_126d") or 0.0), s[0])
        )
        rank_by_sym = {s[0]: i + 1 for i, s in enumerate(survivors[: cfg.top_k])}

        # 5. write everything (passers AND failers, for audit)
        con.execute("DELETE FROM universe_v2 WHERE as_of_date = ?", [as_of_date])
        for sym, m, g, a, reason in scored:
            passes = reason is None and sym in rank_by_sym
            rank = rank_by_sym.get(sym, 0)
            con.execute(
                """INSERT INTO universe_v2(
                       as_of_date, symbol, rank, passes,
                       med_turnover_60d, med_turnover_126d, traded_pct_60d,
                       trading_days_history, close_asof, cv_turnover_126d,
                       circuit_pct_60d, gsm_stage, asm_stage,
                       vol_annualized_60d, exclude_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [as_of_date, sym, rank, passes,
                 m.get("med_turnover_60d"), m.get("med_turnover_126d"),
                 m.get("traded_pct_60d"), m.get("trading_days_history"),
                 m.get("close_asof"), m.get("cv_turnover_126d"),
                 m.get("circuit_pct_60d"), g, a,
                 m.get("vol_annualized_60d"), reason],
            )

        n = con.execute(
            "SELECT COUNT(*) FROM universe_v2 WHERE as_of_date = ? AND passes",
            [as_of_date],
        ).fetchone()[0]
    return int(n)


def recompute_v2_all(*, cfg: V2Config = DEFAULT_V2_CONFIG,
                     force: bool = False, progress_cb=None) -> dict:
    """Recompute universe_v2 for every viable first-trading-day-of-month."""
    with db() as con:
        candidates = [r[0] for r in con.execute(
            "SELECT MIN(date) FROM bhav_daily GROUP BY year, month ORDER BY 1"
        ).fetchall()]
        first_viable_idx = 0
        for i, d in enumerate(candidates):
            prior = con.execute(
                "SELECT COUNT(DISTINCT date) FROM bhav_daily WHERE date < ?", [d]
            ).fetchone()[0]
            if prior >= cfg.min_trading_days:
                first_viable_idx = i
                break
        viable = candidates[first_viable_idx:]
        existing: set[date] = set()
        if not force:
            existing = {r[0] for r in con.execute(
                "SELECT DISTINCT as_of_date FROM universe_v2"
            ).fetchall()}
        todo = [d for d in viable if d not in existing] if not force else viable

    stats = {"as_of_dates": 0, "total_passers": 0}
    for i, d in enumerate(todo):
        n = recompute_v2_for(d, cfg)
        stats["as_of_dates"] += 1
        stats["total_passers"] += n
        if progress_cb:
            progress_cb(i + 1, len(todo), d, n)

    try:
        export_all()
    except Exception as e:
        log.warning("export_all failed after v2 recompute: %s", e)
    return stats
```

- [ ] **Step 4: Run, verify it passes**

```bash
cd /home/rakesh/work/nse500 && uv run pytest tests/test_v2_rank.py -v
```
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
cd /home/rakesh/work/nse500
git add src/nse_universe/rank/v2.py tests/test_v2_rank.py
git commit -m "feat(v2): universe builder with full filter stack"
```

---

### Task 4: Wire `version="v2"` into Universe public API

**Files:**
- Modify: `/home/rakesh/work/nse500/src/nse_universe/core/universe.py`
- Create: `/home/rakesh/work/nse500/tests/test_v2_universe_api.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_v2_universe_api.py`:

```python
from datetime import date

from nse_universe.core.universe import Universe


def test_universe_default_is_v1():
    u = Universe()
    assert u.version == "v1"


def test_universe_v2_routes_to_universe_v2_table(monkeypatch):
    """`Universe(version='v2').members(...)` reads from universe_v2, not universe_rank."""
    u = Universe(version="v2")
    # Just confirm constructor accepts the kwarg and remembers it.
    assert u.version == "v2"


def test_invalid_version_raises():
    import pytest
    with pytest.raises(ValueError):
        Universe(version="v3")
```

- [ ] **Step 2: Run, verify fails**

```bash
cd /home/rakesh/work/nse500 && uv run pytest tests/test_v2_universe_api.py -v
```
Expected: FAIL (`Universe` doesn't accept `version`).

- [ ] **Step 3: Modify `universe.py`**

Edit `src/nse_universe/core/universe.py`. Find `class Universe:` and update:

1. Replace the constructor signature & body:

```python
class Universe:
    VALID_VERSIONS = ("v1", "v2")

    def __init__(self, *, version: str = "v1") -> None:
        if version not in self.VALID_VERSIONS:
            raise ValueError(
                f"version must be one of {self.VALID_VERSIONS}, got {version!r}"
            )
        self.version = version
        self._table = "universe_rank" if version == "v1" else "universe_v2"
        self._where = "" if version == "v1" else " AND passes = TRUE"
        self._indices: Mapping[str, IndexSpec] = load_indices()
```

2. Replace every hard-coded `"universe_rank"` in the queries with `self._table`, and append `self._where` to WHERE clauses. The methods to update:

- `as_of_for`
- `members`
- `rank`
- `universe_at`
- `members_df`
- `walk`

Each query needs `FROM {self._table} ... WHERE as_of_date = ?{self._where} ...` style edits. Concrete patches:

`as_of_for`:
```python
def as_of_for(self, d: date) -> date | None:
    with db(read_only=True) as con:
        row = con.execute(
            f"SELECT MAX(as_of_date) FROM {self._table} WHERE as_of_date <= ?{self._where}",
            [d],
        ).fetchone()
    return row[0] if row and row[0] else None
```

`members`:
```python
def members(self, d: date, index: str) -> list[str]:
    spec = self.index_spec(index)
    asof = self.as_of_for(d)
    if asof is None:
        return []
    with db(read_only=True) as con:
        rows = con.execute(
            f"""
            SELECT symbol
              FROM {self._table}
             WHERE as_of_date = ? AND rank BETWEEN ? AND ?{self._where}
             ORDER BY rank
            """,
            [asof, spec.rank_lo, spec.rank_hi],
        ).fetchall()
    return [r[0] for r in rows]
```

`rank`:
```python
def rank(self, symbol: str, d: date) -> int | None:
    asof = self.as_of_for(d)
    if asof is None:
        return None
    with db(read_only=True) as con:
        row = con.execute(
            f"SELECT rank FROM {self._table} WHERE as_of_date = ? AND symbol = ?{self._where}",
            [asof, symbol],
        ).fetchone()
    return int(row[0]) if row else None
```

`universe_at`:
```python
def universe_at(self, d: date) -> pd.DataFrame:
    asof = self.as_of_for(d)
    if asof is None:
        return pd.DataFrame(columns=["rank", "symbol", "metric_value", "as_of_date"])
    metric_col = "metric_value" if self.version == "v1" else "med_turnover_126d AS metric_value"
    with db(read_only=True) as con:
        df = con.execute(
            f"""
            SELECT rank, symbol, {metric_col}
              FROM {self._table}
             WHERE as_of_date = ?{self._where}
             ORDER BY rank
            """,
            [asof],
        ).fetchdf()
    df["as_of_date"] = asof
    return df
```

`members_df`:
```python
def members_df(self, start: date, end_inclusive: date, index: str) -> pd.DataFrame:
    spec = self.index_spec(index)
    with db(read_only=True) as con:
        df = con.execute(
            f"""
            WITH days AS (
                SELECT DISTINCT date AS trading_day
                  FROM bhav_daily
                 WHERE date BETWEEN ? AND ?
            ),
            asof_for_day AS (
                SELECT days.trading_day,
                       (SELECT MAX(ur.as_of_date)
                          FROM {self._table} ur
                         WHERE ur.as_of_date <= days.trading_day{self._where.replace('passes', 'ur.passes')}) AS as_of_date
                  FROM days
            )
            SELECT a.trading_day AS date, ur.symbol, ur.rank, a.as_of_date
              FROM asof_for_day a
              JOIN {self._table} ur ON ur.as_of_date = a.as_of_date
             WHERE ur.rank BETWEEN ? AND ?{self._where.replace('passes', 'ur.passes')}
             ORDER BY a.trading_day, ur.rank
            """,
            [start, end_inclusive, spec.rank_lo, spec.rank_hi],
        ).fetchdf()
    return df
```

`walk` follows the same pattern — append `{self._where.replace('passes', 'ur.passes')}` where appropriate.

- [ ] **Step 4: Run, verify it passes**

```bash
cd /home/rakesh/work/nse500 && uv run pytest tests/test_v2_universe_api.py tests/test_v2_rank.py -v
```
Expected: PASS (all tests including the v2 API ones).

- [ ] **Step 5: Commit**

```bash
cd /home/rakesh/work/nse500
git add src/nse_universe/core/universe.py tests/test_v2_universe_api.py
git commit -m "feat(v2): version=v1|v2 routing in Universe public API"
```

---

### Task 5: Export `universe_v2.parquet` + `surveillance_daily.parquet`

**Files:**
- Modify: `/home/rakesh/work/nse500/src/nse_universe/core/export.py`

- [ ] **Step 1: Update `export_all` and `import_all_if_missing`**

Append in `export_all()` after the existing `ae = ...` block, before `_write_table`:

```python
        uv2 = con.execute(
            """
            SELECT as_of_date, symbol, rank, passes,
                   med_turnover_60d, med_turnover_126d, traded_pct_60d,
                   trading_days_history, close_asof, cv_turnover_126d,
                   circuit_pct_60d, gsm_stage, asm_stage,
                   vol_annualized_60d, exclude_reason
              FROM universe_v2
             ORDER BY as_of_date, rank, symbol
            """
        ).fetchdf()
        sv = con.execute(
            """
            SELECT date, symbol, gsm_stage, asm_stage, source
              FROM surveillance_daily
             ORDER BY date, symbol, source
            """
        ).fetchdf()
```

Then:

```python
    _write_table("universe_v2", uv2)
    _write_table("surveillance_daily", sv)
    counts["universe_v2"] = int(len(uv2))
    counts["surveillance_daily"] = int(len(sv))
```

Update `log.info(...)` to include the new counts.

In `import_all_if_missing`, add reciprocal blocks:

```python
    uv2_path = DERIVED_DIR / "universe_v2.parquet"
    sv_path = DERIVED_DIR / "surveillance_daily.parquet"
    with db() as con:
        # ... existing universe_rank + adj_events blocks ...
        if uv2_path.exists():
            have = con.execute("SELECT COUNT(*) FROM universe_v2").fetchone()[0]
            if have == 0:
                con.execute(f"INSERT INTO universe_v2 SELECT * FROM read_parquet('{uv2_path}')")
                counts["universe_v2"] = con.execute("SELECT COUNT(*) FROM universe_v2").fetchone()[0]
        if sv_path.exists():
            have = con.execute("SELECT COUNT(*) FROM surveillance_daily").fetchone()[0]
            if have == 0:
                con.execute(f"INSERT INTO surveillance_daily SELECT * FROM read_parquet('{sv_path}')")
                counts["surveillance_daily"] = con.execute("SELECT COUNT(*) FROM surveillance_daily").fetchone()[0]
```

Also initialize `counts` dict to include the new keys with zeros.

- [ ] **Step 2: Smoke-test by importing and calling export_all on empty DB**

```bash
cd /home/rakesh/work/nse500 && uv run python -c "from nse_universe.core.export import export_all; print(export_all())"
```
Expected: prints a dict with `universe_rank`, `adj_events`, `universe_v2`, `surveillance_daily` keys (some may be 0).

- [ ] **Step 3: Commit**

```bash
cd /home/rakesh/work/nse500
git add src/nse_universe/core/export.py
git commit -m "feat(v2): export universe_v2 + surveillance parquet"
```

---

### Task 6: GSM/ASM scraper (`fetch/surveillance.py`)

**Files:**
- Create: `/home/rakesh/work/nse500/src/nse_universe/fetch/surveillance.py`
- Create: `/home/rakesh/work/nse500/src/nse_universe/ingest/__init__.py`
- Create: `/home/rakesh/work/nse500/src/nse_universe/ingest/surveillance.py`
- Create: `/home/rakesh/work/nse500/tests/test_surveillance.py`

- [ ] **Step 1: Implement scraper**

Create `src/nse_universe/fetch/surveillance.py`:

```python
"""NSE GSM / ASM surveillance feed scraper.

NSE publishes daily surveillance lists. There are two long-term programs:
  - GSM (Graded Surveillance Measure): 6 stages, escalating restrictions.
  - ASM (Additional Surveillance Measure): long-term (4 stages) + short-term.

URLs (last verified 2026-05): the JSON endpoints under nseindia.com/api/
return current lists. NSE does NOT publish historical archives, so capture
runs forward-only.

We persist (date, symbol, stage, source='nse_live') into surveillance_daily.
The behavioral proxy from rank.filters fills the historical gap.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from nse_universe.fetch.session import NSESession

log = logging.getLogger(__name__)


GSM_URL = "https://www.nseindia.com/api/reportGsmAlerts?index=GSM"
ASM_LONG_URL = "https://www.nseindia.com/api/reportASMSurveillance?index=allASMLong&type=all"


@dataclass
class SurveillanceRecord:
    symbol: str
    gsm_stage: int | None
    asm_stage: int | None


def _parse_gsm(payload: Any) -> dict[str, int]:
    """Return {symbol: stage} from the GSM endpoint payload."""
    out: dict[str, int] = {}
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not rows:
        return out
    for row in rows:
        sym = row.get("symbol") or row.get("Symbol")
        stage_raw = row.get("stage") or row.get("Stage") or ""
        stage_digits = "".join(c for c in str(stage_raw) if c.isdigit())
        if sym and stage_digits:
            out[sym.upper().strip()] = int(stage_digits)
    return out


def _parse_asm(payload: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not rows:
        return out
    for row in rows:
        sym = row.get("symbol") or row.get("Symbol")
        stage_raw = row.get("longtermStage") or row.get("stage") or ""
        stage_digits = "".join(c for c in str(stage_raw) if c.isdigit())
        if sym and stage_digits:
            out[sym.upper().strip()] = int(stage_digits)
    return out


def fetch_today_surveillance() -> list[SurveillanceRecord]:
    """Scrape GSM + ASM and merge into per-symbol records."""
    sess = NSESession()
    sess.bootstrap()  # set NSE cookies; see fetch/session.py
    gsm_payload = sess.get_json(GSM_URL)
    asm_payload = sess.get_json(ASM_LONG_URL)
    gsm = _parse_gsm(gsm_payload)
    asm = _parse_asm(asm_payload)
    all_symbols = set(gsm) | set(asm)
    return [
        SurveillanceRecord(
            symbol=s,
            gsm_stage=gsm.get(s),
            asm_stage=asm.get(s),
        )
        for s in sorted(all_symbols)
    ]
```

Note: `NSESession.bootstrap()` and `NSESession.get_json()` may not exist verbatim. If `fetch/session.py` exposes different helpers (e.g. raw `get()` returning a `Response` object), adapt — read the existing session module and use its actual API. If `get_json` is absent, use `sess.get(url).json()`.

- [ ] **Step 2: Implement ingest**

Create `src/nse_universe/ingest/__init__.py` (empty).

Create `src/nse_universe/ingest/surveillance.py`:

```python
"""Persist scraped NSE GSM/ASM into surveillance_daily."""
from __future__ import annotations

import logging
from datetime import date

from nse_universe.core.db import db
from nse_universe.fetch.surveillance import fetch_today_surveillance

log = logging.getLogger(__name__)


def ingest_today(*, as_of: date | None = None) -> int:
    """Fetch live NSE surveillance and upsert into surveillance_daily.

    Returns the number of (symbol, gsm/asm) rows written.
    """
    as_of = as_of or date.today()
    records = fetch_today_surveillance()
    with db() as con:
        for r in records:
            con.execute(
                """INSERT OR REPLACE INTO surveillance_daily
                          (date, symbol, gsm_stage, asm_stage, source)
                   VALUES (?, ?, ?, ?, 'nse_live')""",
                [as_of, r.symbol, r.gsm_stage, r.asm_stage],
            )
    log.info("surveillance ingest: %d symbols on %s", len(records), as_of)
    return len(records)
```

- [ ] **Step 3: Pure-logic unit test**

Create `tests/test_surveillance.py`:

```python
from nse_universe.fetch.surveillance import _parse_gsm, _parse_asm


def test_parse_gsm_payload_extracts_symbol_and_stage():
    payload = {"data": [
        {"symbol": "ABC", "stage": "Stage 2"},
        {"symbol": "XYZ", "stage": "Stage 4"},
        {"symbol": "BAD"},  # no stage → dropped
    ]}
    out = _parse_gsm(payload)
    assert out == {"ABC": 2, "XYZ": 4}


def test_parse_asm_payload():
    payload = {"data": [
        {"symbol": "FOO", "longtermStage": "Stage 1"},
        {"symbol": "BAR", "longtermStage": "Stage 3"},
    ]}
    out = _parse_asm(payload)
    assert out == {"FOO": 1, "BAR": 3}


def test_parse_gsm_handles_empty():
    assert _parse_gsm({}) == {}
    assert _parse_gsm({"data": None}) == {}
    assert _parse_gsm({"data": []}) == {}
```

- [ ] **Step 4: Run**

```bash
cd /home/rakesh/work/nse500 && uv run pytest tests/test_surveillance.py -v
```
Expected: PASS (parsing tests only; live scraping isn't tested here).

- [ ] **Step 5: Live smoke test (best-effort, may fail if NSE blocks IP)**

```bash
cd /home/rakesh/work/nse500 && uv run python -c "
from nse_universe.ingest.surveillance import ingest_today
try:
    n = ingest_today()
    print(f'ingested {n} surveillance rows')
except Exception as e:
    print(f'live fetch failed (acceptable in CI): {e}')
"
```
Expected: either prints "ingested N rows" with N > 0, or prints an error. Either is OK — proxy fills the gap.

- [ ] **Step 6: Commit**

```bash
cd /home/rakesh/work/nse500
git add src/nse_universe/fetch/surveillance.py src/nse_universe/ingest/ tests/test_surveillance.py
git commit -m "feat(v2): NSE GSM/ASM surveillance scraper + ingest"
```

---

### Task 7: CLI menu hooks

**Files:**
- Modify: `/home/rakesh/work/nse500/src/nse_universe/cli/menu.py`

- [ ] **Step 1: Read existing menu.py to find pattern**

```bash
cd /home/rakesh/work/nse500 && grep -n "def menu\|MENU\|choice" src/nse_universe/cli/menu.py | head -20
```

Identify where menu items are dispatched (likely a dict or if/elif chain).

- [ ] **Step 2: Add two menu items**

Add two new actions following the existing pattern:

```python
def _rebuild_v2() -> None:
    from nse_universe.rank.v2 import recompute_v2_all
    def cb(i, total, d, n):
        print(f"  [{i}/{total}] {d}: {n} passers")
    stats = recompute_v2_all(progress_cb=cb)
    print(f"v2: {stats['as_of_dates']} as_of_dates, {stats['total_passers']} total passers")


def _refresh_surveillance() -> None:
    from nse_universe.ingest.surveillance import ingest_today
    try:
        n = ingest_today()
        print(f"Surveillance: {n} symbols ingested.")
    except Exception as e:
        print(f"Surveillance fetch failed: {e}")
```

Wire them into the menu dispatch. Use labels:
- "Rebuild universe v2 (filter-stack)"
- "Refresh surveillance feed (GSM/ASM)"

- [ ] **Step 3: Smoke test**

```bash
cd /home/rakesh/work/nse500 && uv run python -c "
from nse_universe.cli import menu
print([m for m in dir(menu) if 'v2' in m.lower() or 'surveillance' in m.lower()])
"
```
Expected: prints non-empty list.

- [ ] **Step 4: Commit**

```bash
cd /home/rakesh/work/nse500
git add src/nse_universe/cli/menu.py
git commit -m "feat(v2): CLI menu actions for v2 rebuild + surveillance refresh"
```

---

### Task 8: Backfill v2 universe over full history

**Files:** (no code changes — execution task)

- [ ] **Step 1: Run the full recompute**

```bash
cd /home/rakesh/work/nse500 && uv run python -c "
import logging; logging.basicConfig(level=logging.INFO)
from nse_universe.rank.v2 import recompute_v2_all
stats = recompute_v2_all(progress_cb=lambda i,t,d,n: print(f'[{i}/{t}] {d}: {n}'))
print('done:', stats)
"
```
Expected: ~240+ as_of_dates processed (2005 → today, monthly). Several minutes runtime. Watch for stale-cache warnings or DuckDB errors.

- [ ] **Step 2: Verify export landed**

```bash
ls -la /home/rakesh/work/nse500/data/derived/universe_v2.parquet /home/rakesh/work/nse500/data/derived/surveillance_daily.parquet
```
Expected: both files exist and are > 1 KB.

- [ ] **Step 3: Sanity check distribution**

```bash
cd /home/rakesh/work/nse500 && uv run python -c "
from nse_universe.core.db import db
from datetime import date
with db(read_only=True) as con:
    rows = con.execute('''
        SELECT as_of_date, COUNT(*) FILTER (WHERE passes) AS passers,
                            COUNT(*) AS total
        FROM universe_v2 GROUP BY as_of_date ORDER BY as_of_date
    ''').fetchall()
print(f'Months covered: {len(rows)}')
for d, p, t in rows[::24]:  # sample every 2 yrs
    print(f'  {d}: {p} passers of {t} candidates')
"
```
Expected: ~240 months, with passers ranging from ~200 (early years) to ~500 (recent years).

- [ ] **Step 4: Commit the parquet** (per project's git strategy)

```bash
cd /home/rakesh/work/nse500
git add data/derived/universe_v2.parquet data/derived/surveillance_daily.parquet
git commit -m "data(v2): backfill universe_v2 2005→today"
```

---

### Task 9: fortress — add `universe.version` config

**Files:**
- Modify: `/home/rakesh/work/momentum-trading-strategy/fortress/config.py`
- Modify: `/home/rakesh/work/momentum-trading-strategy/config.yaml`

- [ ] **Step 1: Read config.py to find universe block**

```bash
cd /home/rakesh/work/momentum-trading-strategy && grep -n "rank_range\|universe" fortress/config.py | head -30
```

- [ ] **Step 2: Add `version` field to the universe config section**

In `fortress/config.py`, find the dataclass / dict / pydantic model representing the `universe:` block. Add a `version: str = "v1"` field with validation. If pydantic:

```python
@validator("version")
def _check_version(cls, v: str) -> str:
    if v not in {"v1", "v2"}:
        raise ValueError(f"universe.version must be 'v1' or 'v2', got {v!r}")
    return v
```

If plain dataclass:

```python
@dataclass
class UniverseConfig:
    rank_range: tuple[int, int] = (201, 600)
    version: str = "v1"

    def __post_init__(self):
        if self.version not in ("v1", "v2"):
            raise ValueError(...)
```

- [ ] **Step 3: Update `config.yaml`**

In `/home/rakesh/work/momentum-trading-strategy/config.yaml`, find the `universe:` block (currently `universe: { rank_range: [201, 600] }`). Replace with:

```yaml
universe:
  rank_range: [201, 600]
  # Universe builder: v1 = nse-universe legacy turnover-rank (Nifty-style proxy).
  # v2 = momentum-grade filter stack (recent liquidity, listing age, circuit ratio,
  #      CV, GSM/ASM surveillance gates). Default v1 for baseline parity.
  version: "v1"
```

- [ ] **Step 4: Run fortress tests to catch breakage**

```bash
cd /home/rakesh/work/momentum-trading-strategy && .venv/bin/python -m pytest tests/ -q -k "config"
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/rakesh/work/momentum-trading-strategy
git add fortress/config.py config.yaml
git commit -m "feat(universe): add universe.version config knob (v1 default)"
```

---

### Task 10: fortress — route Universe loader to v1/v2

**Files:**
- Modify: `/home/rakesh/work/momentum-trading-strategy/fortress/universe.py`
- Create: `/home/rakesh/work/momentum-trading-strategy/tests/test_universe_v2_loader.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_universe_v2_loader.py`:

```python
from datetime import date
from unittest.mock import patch

from fortress.universe import Universe


def test_universe_default_uses_v1_nse_universe():
    """No explicit version → legacy behaviour (v1 nse-universe call)."""
    with patch("fortress.universe._nse_universe_singleton") as m:
        u = Universe(as_of=date(2024, 1, 1), rank_range=(201, 600))
        # The singleton constructor should have been called with NO version arg
        # (or version='v1'); when fortress.Universe.__init__ runs _load_members
        # it should call nse.universe_at(as_of)
        m.return_value.universe_at.assert_called()
        # The v1 path uses universe_rank — assert via the args passed
        call_args = m.return_value.universe_at.call_args
        # Either no version kwarg or version='v1'
        v = call_args.kwargs.get("version", "v1")
        assert v == "v1"


def test_universe_v2_passes_version_to_loader():
    with patch("fortress.universe._nse_universe_singleton") as m:
        u = Universe(as_of=date(2024, 1, 1), rank_range=(201, 600), version="v2")
        call_args = m.return_value.universe_at.call_args
        assert call_args.kwargs.get("version") == "v2"
```

- [ ] **Step 2: Run, expect failure**

```bash
cd /home/rakesh/work/momentum-trading-strategy && .venv/bin/python -m pytest tests/test_universe_v2_loader.py -v
```
Expected: FAIL (no `version` param on Universe).

- [ ] **Step 3: Modify `fortress/universe.py`**

In the `Universe.__init__` (around line 116), add `version` to signature:

```python
def __init__(
    self,
    as_of: Optional[date] = None,
    rank_range: Tuple[int, int] = (1, 200),
    sectors_path: str = "stock-sectors.json",
    metadata_path: str = "market-metadata.json",
    renames_path: str = "stock-renames.json",
    *,
    version: str = "v1",
    filepath: Optional[str] = None,  # legacy, ignored
    filter_universes: Optional[List[str]] = None,  # legacy, ignored
) -> None:
    # ... existing legacy-arg detection ...
    if version not in ("v1", "v2"):
        raise ValueError(f"version must be 'v1' or 'v2', got {version!r}")
    self.version = version
    # ... rest of init ...
```

Then in `_nse_universe_singleton` — we need a per-version singleton (one for v1, one for v2) OR pass version on each call. Simpler: pass version on each call.

Update `_load_members`:

```python
def _load_members(self) -> List[str]:
    nse = _nse_universe_singleton()
    df = nse.universe_at(self.as_of, version=self.version)
    lo, hi = self.rank_range
    filtered = df[(df["rank"] >= lo) & (df["rank"] <= hi)]
    return self._apply_renames(filtered["symbol"].tolist())
```

**Critical**: the nse-universe `Universe` class in nse500 is constructed once (singleton) but its methods now need a `version` parameter. Re-check the nse500 `Universe.universe_at` signature from Task 4 — verify it accepts `version` kwarg. If not, refactor: instead of one singleton, cache two — one for v1 and one for v2:

```python
_nse_universe_singletons: dict[str, object] = {}

def _nse_universe_singleton(version: str = "v1"):
    if version not in _nse_universe_singletons:
        from nse_universe import Universe as NSEUniverse
        _nse_universe_singletons[version] = NSEUniverse(version=version)
    return _nse_universe_singletons[version]
```

And in `_load_members`:

```python
def _load_members(self) -> List[str]:
    nse = _nse_universe_singleton(self.version)
    df = nse.universe_at(self.as_of)
    ...
```

Choose whichever matches the nse500 API from Task 4. Per Task 4, `Universe(version=...)` is the constructor pattern; queries don't take version. So the per-version-singleton approach is correct.

Update the test to match — it should patch `_nse_universe_singleton` and verify it's called with `version="v2"` argument.

Adjust `tests/test_universe_v2_loader.py` accordingly:

```python
def test_universe_v2_passes_version_to_loader():
    with patch("fortress.universe._nse_universe_singleton") as m:
        u = Universe(as_of=date(2024, 1, 1), rank_range=(201, 600), version="v2")
        m.assert_called_with("v2")


def test_universe_default_uses_v1_nse_universe():
    with patch("fortress.universe._nse_universe_singleton") as m:
        u = Universe(as_of=date(2024, 1, 1), rank_range=(201, 600))
        m.assert_called_with("v1")
```

- [ ] **Step 4: Run tests, verify pass**

```bash
cd /home/rakesh/work/momentum-trading-strategy && .venv/bin/python -m pytest tests/test_universe_v2_loader.py tests/ -q
```
Expected: new tests PASS; existing universe tests still PASS.

- [ ] **Step 5: Wire config-version → Universe**

Find every call site that constructs `Universe(...)` in fortress (likely in `cache.py`, `backtest.py`, `cli.py`). At each, plumb the config:

```bash
cd /home/rakesh/work/momentum-trading-strategy && grep -rn "Universe(" fortress/ | grep -v test_ | head
```

For each, add `version=config.universe.version` (or equivalent). The exact path depends on how `config` is propagated. **DO NOT** hardcode "v2" — must read from config.

- [ ] **Step 6: Commit**

```bash
cd /home/rakesh/work/momentum-trading-strategy
git add fortress/universe.py tests/test_universe_v2_loader.py fortress/cache.py fortress/backtest.py fortress/cli.py
git commit -m "feat(universe): route loader to v1/v2 per config"
```

(Some of those files may not need changes — adjust the `git add` list.)

---

### Task 11: Baseline backtest (v1) — capture current metrics

**Files:** (execution task)

- [ ] **Step 1: Run the 13-yr phase backtest with default config (v1)**

```bash
cd /home/rakesh/work/momentum-trading-strategy && ./start.sh
```

Pick CLI Option 8 (Market Phases). When prompted, accept defaults (full 2013-01-01 → today range). Save the printed output to a file:

```bash
cd /home/rakesh/work/momentum-trading-strategy && ./start.sh 2>&1 | tee /tmp/backtest_v1_baseline.txt
```

(Interactive — provide "8" + enter when prompted, then accept defaults. If the CLI doesn't support headless input via pipe, run interactively and `tee` the full session.)

- [ ] **Step 2: Extract metrics**

Parse the output for:
- Overall CAGR
- Overall Sharpe
- Overall MaxDD
- Total return
- Per-phase deltas (should match the CLAUDE.md baseline within rounding)

Confirm: CAGR ≈ 24.1%, Sharpe ≈ 1.02, MaxDD ≈ -28.1%. If off by > 0.5pp, debug before continuing (changes from Tasks 9-10 may have leaked).

- [ ] **Step 3: Snapshot baseline metrics**

Write to `/tmp/baseline_v1.json`:

```bash
cat > /tmp/baseline_v1.json <<EOF
{
  "version": "v1",
  "cagr": 24.1,
  "sharpe": 1.02,
  "maxdd": -28.1,
  "total_return_pct": 1603,
  "captured_at": "$(date -Iseconds)"
}
EOF
```

(Adjust numbers to actual.)

- [ ] **Step 4: No commit** (baseline file is local-only artifact)

---

### Task 12: v2 backtest + auto-tuning loop

**Files:** (execution task, may edit `config.yaml`)

- [ ] **Step 1: Switch config to v2**

In `config.yaml`, change:

```yaml
universe:
  rank_range: [201, 600]
  version: "v2"   # ← changed from "v1"
```

- [ ] **Step 2: Run the same backtest**

```bash
cd /home/rakesh/work/momentum-trading-strategy && ./start.sh 2>&1 | tee /tmp/backtest_v2_run1.txt
```
(Option 8, full range.)

- [ ] **Step 3: Compare against baseline**

Compute delta:
- `cagr_delta = cagr_v2 - cagr_v1`
- If `cagr_delta >= -1.0` (within 1pp): **success**, proceed to commit.
- If `cagr_delta < -1.0`: enter tuning loop.

- [ ] **Step 4: Tuning loop (only if regression > 1pp)**

For each round, identify the likely cause from logs:
- If too few candidates passed → loosen filters.
- Loosening order (least → most aggressive):
  1. `min_med_turnover_60d: 50_00_000 → 25_00_000`
  2. `max_circuit_pct_60d: 0.05 → 0.10`
  3. `max_cv_turnover_126d: 3.0 → 5.0`
  4. `max_asm_stage: 2 → 3`
  5. `min_close: 50 → 20`

Each round: edit the relevant default in `src/nse_universe/rank/v2.py` (`V2Config` dataclass defaults), re-run `recompute_v2_all` in nse500 (force=True), re-export parquet, then re-run the fortress backtest. Stop when CAGR delta ≥ -1.0 or you've cycled through 5 rounds. If 5 rounds don't recover baseline, document the best result and flip config back to `v1` default, leaving v2 opt-in.

- [ ] **Step 5: Commit the final v2 config**

If v2 matches/beats baseline:

```bash
cd /home/rakesh/work/momentum-trading-strategy
git add config.yaml
git commit -m "feat(universe): ship v2 as default (baseline parity confirmed)"

cd /home/rakesh/work/nse500
git add src/nse_universe/rank/v2.py data/derived/universe_v2.parquet
git commit -m "tune(v2): final thresholds matching fortress 13-yr baseline"
```

If v2 doesn't recover:

```bash
cd /home/rakesh/work/momentum-trading-strategy
# Revert config.yaml to version: "v1"
git add config.yaml
git commit -m "feat(universe): keep v1 default, v2 opt-in (backtest regression unresolved)"
```

Write `/tmp/v2_backtest_outcome.md` summarizing what worked / didn't.

---

### Task 13: Add `simple_topn` benchmark strategy

**Files:**
- Create: `/home/rakesh/work/momentum-trading-strategy/fortress/strategy/simple_topn.py`
- Modify: `/home/rakesh/work/momentum-trading-strategy/fortress/strategy/__init__.py`
- Create: `/home/rakesh/work/momentum-trading-strategy/tests/test_simple_topn.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_simple_topn.py`:

```python
from datetime import datetime
from unittest.mock import MagicMock

from fortress.strategy.registry import StrategyRegistry
from fortress.strategy.simple_topn import SimpleTopNStrategy


def test_simple_topn_registered():
    assert StrategyRegistry.is_registered("simple_topn")


def test_simple_topn_picks_top_n_by_rank():
    """Given a universe ordered by rank, simple_topn ranks by rank ascending."""
    strat = SimpleTopNStrategy()

    universe = MagicMock()
    stocks = [
        MagicMock(ticker=f"S{i}", sector="X", sub_sector="X",
                  zerodha_symbol=f"S{i}", name=f"S{i}")
        for i in range(30)
    ]
    universe.get_all_stocks.return_value = stocks

    market_data = MagicMock()
    # nse_universe rank getter returns rank=i+1 for stock S{i}
    def rank_side_effect(symbol, d):
        return int(symbol[1:]) + 1
    market_data.get_rank = MagicMock(side_effect=rank_side_effect)

    result = strat.rank_stocks(
        as_of_date=datetime(2024, 1, 1),
        universe=universe,
        market_data=market_data,
        filter_entry=False,
    )
    # Sorted by score ascending == rank ascending
    assert [s.ticker for s in result[:5]] == ["S0", "S1", "S2", "S3", "S4"]


def test_simple_topn_select_portfolio_picks_n():
    strat = SimpleTopNStrategy()
    ranked = [MagicMock(ticker=f"T{i}", sector="X", percentile=100 - i,
                        passes_entry_filters=True)
              for i in range(30)]
    weights = strat.select_portfolio(
        ranked_stocks=ranked,
        portfolio_value=10_00_000,
        current_positions={},
        max_positions=15,
        max_per_sector=5,
    )
    assert len(weights) == 15
    # equal weight
    for w in weights.values():
        assert abs(w - 1.0/15) < 1e-6
```

- [ ] **Step 2: Run, expect failure**

```bash
cd /home/rakesh/work/momentum-trading-strategy && .venv/bin/python -m pytest tests/test_simple_topn.py -v
```
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `simple_topn.py`**

Create `fortress/strategy/simple_topn.py`:

```python
"""Simple Top-N benchmark strategy.

Picks the top-N stocks from the universe purely by nse-universe rank
(= 6mo median turnover order, descending → low rank = high turnover).
Equal-weight, no momentum signal, no regime adaptation, no filters
beyond what the universe itself provides.

This is a CONTROL strategy. It exists to measure how much alpha the
sophisticated dual_momentum machinery adds *over* simply trading the
universe. Not intended for live deployment.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional

from .base import BaseStrategy, ExitSignal, StockScore, StopLossConfig
from .registry import StrategyRegistry

if TYPE_CHECKING:
    from ..config import Config
    from ..market_data import MarketDataProvider
    from ..universe import Universe

log = logging.getLogger(__name__)


class SimpleTopNStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "simple_topn"

    @property
    def description(self) -> str:
        return "Benchmark: top-N from universe rank (equal-weight, no signal)"

    def rank_stocks(self, as_of_date, universe, market_data, filter_entry=True):
        stocks = universe.get_all_stocks()
        ranked: List[StockScore] = []
        for stock in stocks:
            try:
                r = market_data.get_rank(stock.ticker, as_of_date)
            except Exception:
                r = None
            if r is None:
                continue
            score = -float(r)  # lower rank = better score
            ranked.append(StockScore(
                ticker=stock.ticker,
                sector=stock.sector,
                sub_sector=stock.sub_sector,
                zerodha_symbol=stock.zerodha_symbol,
                name=stock.name,
                score=score,
                rank=int(r),
                percentile=0.0,  # filled below
                passes_entry_filters=True,  # no entry filter for benchmark
                filter_reasons=[],
            ))
        ranked.sort(key=lambda s: -s.score)  # best first
        n = max(len(ranked), 1)
        for i, s in enumerate(ranked):
            s.percentile = 100.0 * (n - i) / n
        return ranked

    def select_portfolio(self, ranked_stocks, portfolio_value,
                         current_positions, max_positions, max_per_sector):
        picks: list = []
        sector_count: dict[str, int] = {}
        for s in ranked_stocks:
            if not s.passes_entry_filters:
                continue
            if sector_count.get(s.sector, 0) >= max_per_sector:
                continue
            picks.append(s)
            sector_count[s.sector] = sector_count.get(s.sector, 0) + 1
            if len(picks) >= max_positions:
                break
        if not picks:
            return {}
        w = 1.0 / len(picks)
        return {s.ticker: w for s in picks}

    def calculate_weights(self, selected_stocks, portfolio_value):
        if not selected_stocks:
            return {}
        w = 1.0 / len(selected_stocks)
        return {s.ticker: w for s in selected_stocks}

    def check_exit_triggers(self, ticker, entry_price, current_price,
                             peak_price, days_held, stock_score, nms_percentile):
        # Exit when the stock falls out of the universe (no rank or score≤cutoff).
        if stock_score is None:
            return ExitSignal(
                should_exit=True,
                reason="dropped from universe",
                exit_type="universe_exit",
                urgency="next_rebalance",
            )
        return ExitSignal(should_exit=False, reason="", exit_type="")

    def get_stop_loss_config(self, ticker, current_gain):
        return StopLossConfig(
            initial_stop=0.20,
            trailing_stop=0.15,
            trailing_activation=0.10,
        )


StrategyRegistry.register(SimpleTopNStrategy)
```

- [ ] **Step 4: Wire registration in package init**

Modify `fortress/strategy/__init__.py` — add this line near the other strategy imports:

```python
from . import simple_topn  # noqa: F401 — register simple_topn
```

If the package init is empty, add:

```python
from . import adaptive_dual_momentum  # noqa: F401
from . import simple_topn  # noqa: F401
```

(Match whatever pattern already exists; just ensure the module is imported once so registration runs.)

- [ ] **Step 5: Run tests**

```bash
cd /home/rakesh/work/momentum-trading-strategy && .venv/bin/python -m pytest tests/test_simple_topn.py -v
```
Expected: PASS.

- [ ] **Step 6: Smoke-list strategies**

```bash
cd /home/rakesh/work/momentum-trading-strategy && .venv/bin/python -c "
from fortress.strategy.registry import StrategyRegistry
import fortress.strategy  # trigger registrations
for n, d in StrategyRegistry.list_strategies():
    print(f'{n}: {d}')
"
```
Expected: prints at least `dual_momentum` and `simple_topn`.

Note: `market_data.get_rank` may not be a method that exists. If `MarketDataProvider` doesn't expose rank lookup, plumb it via `universe`: `universe._sector_map` won't have it, but `universe` carries `as_of` and the underlying nse-universe call returns ranks. Adjust by calling `nse_universe.Universe(version=...).rank(symbol, as_of_date)` directly inside `rank_stocks`. Update the test to match.

- [ ] **Step 7: Commit**

```bash
cd /home/rakesh/work/momentum-trading-strategy
git add fortress/strategy/simple_topn.py fortress/strategy/__init__.py tests/test_simple_topn.py
git commit -m "feat(strategy): add simple_topn benchmark strategy"
```

---

### Task 14: Optional — benchmark backtest with simple_topn

**Files:** (execution task)

- [ ] **Step 1: Switch active strategy to simple_topn**

In `config.yaml`:
```yaml
active_strategy: "simple_topn"
```

If fortress's `active_strategy` consumer respects this dynamically — run a backtest. If it requires code changes to swap, defer this task or wire the swap in fortress.

- [ ] **Step 2: Run a single-phase backtest with v2 universe**

```bash
cd /home/rakesh/work/momentum-trading-strategy && ./start.sh 2>&1 | tee /tmp/backtest_simple_topn_v2.txt
```
Pick CLI Option 5 (Backtest) for speed.

- [ ] **Step 3: Compare numbers**

Expect simple_topn + v2 to be dramatically WORSE than dual_momentum + v2 (no signal, no regime adaptation). The size of the gap quantifies the alpha contribution of dual_momentum machinery.

- [ ] **Step 4: Restore active_strategy and document**

In `config.yaml`:
```yaml
active_strategy: "dual_momentum"
```

Write the gap summary to `/tmp/benchmark_summary.md`. Don't commit `config.yaml` change (it's already at `dual_momentum`).

---

### Task 15: Update memory + close out

**Files:**
- Modify: `/home/rakesh/.claude/projects/-home-rakesh-work-nse500/memory/project_momentum_universe.md`

- [ ] **Step 1: Update memory with final results**

Update the existing `project_momentum_universe.md`:

- Replace "Locked decisions" → "Implemented" with actual final filter thresholds (after any auto-tuning in Task 12).
- Add a section "Validation" with the v1 vs v2 backtest deltas.
- Add a section "Surveillance feed" noting the URLs scraped, the proxy heuristic, and any caveats discovered.

- [ ] **Step 2: Final commit hygiene**

Check both repos for stragglers:

```bash
cd /home/rakesh/work/nse500 && git status
cd /home/rakesh/work/momentum-trading-strategy && git status
```

Each should be clean (or only show committed-but-not-yet-pushed work; do NOT push without explicit ask).

- [ ] **Step 3: Summarize for the user**

Print a final summary including:
- v1 baseline metrics (CAGR / Sharpe / MaxDD)
- v2 metrics (same)
- Delta + interpretation
- Whether v2 is now default or opt-in
- TopN benchmark results (if Task 14 ran)
- Files / commits created in each repo
- Anything that requires manual follow-up (e.g. live NSE-feed authentication, daily cron for surveillance ingest)

---

## Self-review notes

**Spec coverage check:**
- Phase 1 (v2 filters in nse500) → Tasks 1–5, 8 ✓
- Phase 2 (GSM/ASM scraper) → Task 6 ✓
- Phase 3 (fortress integration) → Tasks 9–10 ✓
- Phase 4 (backtest validation + auto-tune) → Tasks 11–12 ✓
- Phase 5 (topN benchmark) → Tasks 13–14 ✓
- Memory close-out → Task 15 ✓

**Known risks:**
- Behavioural GSM proxy heuristic is unvalidated against ground truth. The first time real NSE-feed data lands (Task 6 step 5), spot-check 10 names where proxy says stage ≥ 2 vs what NSE-live says.
- `_INTERNAL_DDL` migration is additive (only `CREATE IF NOT EXISTS`), so SCHEMA_VERSION bump is cosmetic — if an older DB exists it'll silently work.
- The "behavioral_proxy" rows in `surveillance_daily` accumulate during Task 3 — `recompute_v2_for` writes one row per (symbol, as_of_date) it processes. Over 240 months × ~2000 symbols, that's ~500k rows. Acceptable.
- Fortress's `_nse_universe_singleton` cache may need invalidation if we switch versions mid-process (e.g., backtest swapping). Two-singleton pattern in Task 10 handles this.
- The auto-tuning loop in Task 12 has no upper bound on rounds beyond the documented 5. If after 5 rounds CAGR is still down, the plan says fall back to v2-opt-in — make this fallback explicit.

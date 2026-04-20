# Using `nse-universe` as a library

This document is for algorithm/backtest code that wants to **depend on** `nse-universe` without modifying it. It covers: installing, picking a data-access mode, the library API, common backtest patterns, and caveats.

> If you are maintaining the `nse-universe` repo itself, read `README.md` and `learning.md` instead.

---

## TL;DR

Three integration modes, pick one:

| Mode | Install | Data lives | Best for |
|---|---|---|---|
| **A. Embedded** | clone repo, `uv sync`, import as Python lib | local `data/` directory | production backtests, fastest queries, offline work |
| **B. Remote (HTTP)** | your algo makes HTTP calls | upstream at `https://javajack.github.io/custom-nse-500-historical-data/` | lightweight algos, cross-language, CI jobs |
| **C. Hybrid** | install lib, point to shared `data/` | separate data clone | one data copy, many algo repos |

---

## Mode A — embedded library (recommended for Python backtests)

### Install

```bash
# your algo repo, separate from nse-universe
git clone https://github.com/javajack/custom-nse-500-historical-data.git ~/work/nse-universe
cd ~/work/nse-universe
uv sync                       # one-time: installs the lib + deps

# in your backtest repo
uv add --editable ~/work/nse-universe      # or pip install -e ~/work/nse-universe
```

Or add to your `pyproject.toml` directly:

```toml
[project]
dependencies = [
    "nse-universe @ git+https://github.com/javajack/custom-nse-500-historical-data.git",
]
```

### Quickstart

```python
from datetime import date
from nse_universe import Universe

u = Universe()

# Point-in-time members of a custom index on any trading day
u.members(date(2020, 3, 20), "nifty_50")
# → ['RELIANCE', 'HDFCBANK', 'TCS', ...]   50 symbols

u.rank("RELIANCE", date(2020, 3, 20))
# → 1

u.is_member("IRCTC", date(2020, 3, 20), "midcap_150")
# → False

# Full universe on a date
u.universe_at(date(2020, 3, 20))
# → DataFrame(rank, symbol, metric_value, as_of_date)

# Health / coverage
u.health()
# → {'trading_days': 5256, 'first_date': date(2005,1,3), 'last_date': date(...),
#    'distinct_symbols': 4166, 'rank_snapshots': 249, ...}
```

### The backtest-friendly `walk()` iterator

```python
from datetime import date
from nse_universe import Universe

u = Universe()

# Every trading day × its eligible nifty_100 members
for d, members in u.walk(date(2015, 1, 1), date(2025, 1, 1), "nifty_100", freq="D"):
    prices = load_your_prices(members, d)
    signals = your_strategy(prices, d)
    ...

# Or rebalance-date only (first trading day of each month)
for d, members in u.walk(date(2015, 1, 1), date(2025, 1, 1), "midcap_150", freq="M"):
    update_portfolio(members, d)
```

### Long-form DataFrame for vectorized joins

```python
df = u.members_df(date(2015, 1, 1), date(2025, 1, 1), "nifty_500")
# Columns: date, symbol, rank, as_of_date
# Shape: (~5M rows — every trading day × top-500 member)

# Join with your price data on (date, symbol)
joined = your_prices.merge(df, on=["date", "symbol"], how="inner")
```

---

## Mode B — remote HTTP (cross-language, no local data)

Point your code at the hosted Pages URL or a self-hosted FastAPI.

### Raw file access (no server needed)

```python
import pandas as pd

URL_BASE = "https://javajack.github.io/custom-nse-500-historical-data"

# One day's Parquet, normalized
df = pd.read_parquet(f"{URL_BASE}/data/parquet/year=2020/month=03/2020-03-20.parquet")
```

```python
# DuckDB streams over HTTP via the httpfs extension — zero files on disk
import duckdb
con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs;")
df = con.execute(f"""
    SELECT symbol, AVG(close) AS avg_close, SUM(turnover) AS tot_tn
      FROM read_parquet('{URL_BASE}/data/parquet/year=2020/month=*/*.parquet')
     WHERE date BETWEEN DATE '2020-03-01' AND DATE '2020-03-31'
  GROUP BY symbol
  ORDER BY tot_tn DESC
     LIMIT 20
""").df()
```

```bash
# Shell: mirror a whole year
wget -r -np -nH --cut-dirs=2 -R "index.html*" \
     https://javajack.github.io/custom-nse-500-historical-data/data/parquet/year=2020/
```

### Membership queries via FastAPI (run locally, or host anywhere)

```bash
# one-time: start the API in the nse-universe repo
cd ~/work/nse-universe && uv run uvicorn nse_universe.api.app:app --port 8765
```

Your algo (any language) calls HTTP:

```python
import httpx
r = httpx.get("http://127.0.0.1:8765/members", params={"date": "2020-03-20", "index": "nifty_50"})
members = r.json()["symbols"]     # ['RELIANCE', ...]
```

```bash
curl "http://127.0.0.1:8765/members?date=2020-03-20&index=nifty_50"
curl "http://127.0.0.1:8765/rank?symbol=RELIANCE&date=2020-03-20"
curl "http://127.0.0.1:8765/is_member?symbol=IRCTC&date=2020-03-20&index=midcap_150"
curl "http://127.0.0.1:8765/universe_at?date=2020-03-20"
```

Endpoints are documented at `http://127.0.0.1:8765/docs` (Swagger UI) and `http://127.0.0.1:8765/openapi.json` (schema for Go/Java/TS client codegen via [openapi-generator](https://openapi-generator.tech)).

---

## Mode C — hybrid (shared data directory)

Useful when you have many backtest repos that all need the same data, and you don't want multiple 680MB copies.

```bash
# one central checkout with the data
git clone https://github.com/javajack/custom-nse-500-historical-data.git /srv/nse-universe
# keep it updated: cd /srv/nse-universe && git pull

# each algo repo has its own venv, installs the lib, but points to the shared data
export NSE_UNIVERSE_DATA_DIR=/srv/nse-universe/data
uv run my_backtest.py
```

The library respects `NSE_UNIVERSE_DATA_DIR` so the same Universe() instance reads from there.

---

## Common backtest patterns

### Membership filter inside a loop

```python
from datetime import date, timedelta
from nse_universe import Universe
import pandas as pd

u = Universe()
UNIVERSE = "nifty_100"
signals = []

for d, members in u.walk(date(2018, 1, 1), date(2024, 1, 1), UNIVERSE, freq="D"):
    prices = load_ohlc_for(members, d - timedelta(days=30), d)
    mom = prices.pct_change(20).iloc[-1]
    for sym, r in mom.nlargest(10).items():
        signals.append({"date": d, "symbol": sym, "score": r})

pd.DataFrame(signals)
```

### Survivorship-safe universe scan (vectorized)

```python
# Build a (date, symbol) key of membership, join once, done
members = u.members_df(start, end, "nifty_500")   # ~2M rows for 10 years
prices = pd.read_parquet("my_prices.parquet")      # your closes
eligible = prices.merge(members[["date", "symbol"]], on=["date", "symbol"])
# now `eligible` has only rows where the symbol was in custom nifty_500 on that date
```

### Rebalance-date cohorts

```python
# A single monthly portfolio per as_of_date — common in factor studies
cohorts = {}
for d, syms in u.walk(start, end, "nifty_50", freq="M"):
    cohorts[d] = syms    # 50 symbols each

# Compute factor scores for each cohort, rebalance on next freq=M date
```

### Reading prices alongside membership

`nse-universe` Parquet has price columns too. You can get membership AND prices in one read:

```python
URL = "https://javajack.github.io/custom-nse-500-historical-data/data/parquet"

prices = pd.read_parquet(f"{URL}/year=2020/month=03/2020-03-20.parquet")
# Columns: symbol, date, open, high, low, close, prev_close, volume, turnover, trades, year, month
# Every symbol NSE published EQ for that day (~1800), with prices

# Intersect with your custom_nifty_50 membership:
nifty50 = u.members(date(2020, 3, 20), "nifty_50")
prices_nifty50 = prices[prices["symbol"].isin(nifty50)]
```

---

## Custom indices — add your own without modifying the lib

**Don't** edit `config/indices.yml` in the library repo. Instead, pass your own config path:

```python
# your_indices.yml
# indices:
#   my_top30:         { rank_range: [1, 30] }
#   my_momentum_tail: { rank_range: [301, 500] }
#   deep_smallcap:    { rank_range: [451, 1000] }

import os
os.environ["NSE_UNIVERSE_INDICES_CONFIG"] = "/path/to/your_indices.yml"
from nse_universe import Universe
u = Universe()
u.members(date(2020, 3, 20), "my_top30")
```

(If the library doesn't support that env override yet, file an issue — it's a one-line change.)

Alternatively, compute membership directly from rank in your code:

```python
# Bypass index definitions; use rank windows directly
full = u.universe_at(date(2020, 3, 20))
my_universe = full[(full["rank"] >= 1) & (full["rank"] <= 30)]["symbol"].tolist()
```

---

## Data semantics you must understand

### 1. Monthly rebalance

Membership for any date D resolves to the most recent `as_of_date ≤ D` (monthly snapshot, first trading day of each month). Between rebalances, members are stable. Intra-month additions/removals are invisible by design.

```python
u.as_of_for(date(2020, 3, 15))    # → date(2020, 3, 2)   (first trading day of March 2020)
u.as_of_for(date(2020, 3, 31))    # → date(2020, 3, 2)   (same — still March's snapshot)
u.as_of_for(date(2020, 4, 5))     # → date(2020, 4, 1)   (new snapshot)
```

### 2. Rank is 6-month median turnover, not free-float market cap

Good enough for factor studies (90%+ rank correlation with real mcap for top-500). **Not a drop-in for index replication** — at rank 450–500 you'll see ~5–10% of names differ from the real Nifty 500.

### 3. IPO eligibility is 126 trading days

A stock needs ≥126 trading days of history before it can be ranked. This prevents one-day IPO spikes from entering top-50.

### 4. Prices are UNADJUSTED

`close`, `open`, etc. in Parquet are as-printed by NSE. Splits and bonuses will look like price discontinuities.

- If you use these prices for momentum / mean-reversion factors: apply adjustment via `data/actions/{SYMBOL}.parquet` (yfinance splits + dividends, ~66% symbol coverage).
- If you use Zerodha / Upstox / your broker's adjusted feed for prices: ignore the nse-universe prices and just use it for membership.

```python
from nse_universe.actions.fetch import compute_adj_factor
fac = compute_adj_factor("RELIANCE")
# DataFrame(event_date, after_split_factor) — cumulative 1/ratio, applied to dates ≤ event_date
```

### 5. Survivorship bias is zero *in the data*

Every bhavcopy ever published is retained. If a symbol traded on day D, it's in day D's zip and Parquet. **But** your membership universe on any given day only includes symbols that were top-1000 by rank at that month's rebalance — delisted names drop out of rank naturally as they stop trading.

If your strategy needs delisted names that weren't top-1000 (e.g., deep-smallcap survivorship studies), read Parquet directly instead of using `Universe.members()`.

### 6. EQ series only

The Parquet contains NSE's EQ series. No BE (trade-to-trade), no SME, no derivatives, no T-bills. If you need those, read the raw zip CSVs which have all series.

---

## Performance tips

- **Use `walk()` with `freq="M"` if monthly cadence works** — DuckDB caches the member list per `as_of_date`, so walking 10 years of daily steps is near-free after the first month.
- **Prefer `members_df()` for vectorized ops** over looping `members()` day-by-day.
- **Prefer local Parquet reads** over HTTP when you have the data checked out. Even with DuckDB httpfs streaming, local is 10× faster for range scans.
- **For one-off queries on a single day**, HTTP-read a single-day Parquet (~80–200 KB) is plenty fast.
- **`pd.read_parquet(URL)` with HTTPS works** for any single file.
- **DuckDB can SQL-query thousands of remote Parquet files at once** via `read_parquet('{URL}/year=*/month=*/*.parquet')` — partition pruning reduces network traffic dramatically for date-filtered queries.

---

## Thread-safety + concurrency

- `Universe()` is cheap to construct (reads one YAML). Make one per thread or per-request.
- The underlying DuckDB connection is opened read-only per query — concurrent readers are safe.
- **Do not** run multiple ingest/rank jobs against the same `data/db/universe.duckdb` — DuckDB holds an exclusive lock during writes.

---

## Updating your pinned copy

The dataset grows ~1 trading day per day. You have three options:

1. **Pull the repo** (`git pull`) — fetches new raw + Parquet + rank snapshots + actions in one shot. Idempotent.
2. **Fetch only the new day** — `curl` the new-date Parquet URL directly if that's all you need.
3. **Re-run the ingester locally** — `cd ~/work/nse-universe && ./start.sh` → Sync bhavcopy → Full pipeline. Useful if you've diverged or need reproducibility.

If membership as of "today" matters for live trading: the monthly rebalance means today's `u.members(today, idx)` uses the most recent first-of-month snapshot. For this to include the last complete month, the upstream repo must have run its full pipeline including `Recompute rankings` after the month closed.

---

## Versioning + stability

- **URL paths are stable**. Neither the library API nor the Pages URLs rename things under you.
- **Parquet schema is append-only**. New columns may appear; existing columns don't rename or retype.
- **Function signatures on `Universe` are stable**. New kwargs default to backward-compatible behavior.
- **Breaking changes** (if ever) ship with a major version bump and a migration note.

Pin to a commit if you need airtight reproducibility:

```toml
dependencies = [
    "nse-universe @ git+https://github.com/javajack/custom-nse-500-historical-data.git@<commit-sha>",
]
```

---

## Minimal working example (copy-paste runnable)

```python
# backtest_skeleton.py
# A complete, standalone example. Run with: uv run python backtest_skeleton.py
from datetime import date
import pandas as pd
from nse_universe import Universe

u = Universe()
print("coverage:", u.health())

start, end = date(2022, 1, 1), date(2022, 12, 31)

# Equal-weight monthly nifty_100 portfolio, hold for 1 month, flat otherwise.
portfolio_history = []
for rebalance_d, syms in u.walk(start, end, "nifty_100", freq="M"):
    w = {s: 1 / len(syms) for s in syms}
    portfolio_history.append({"date": rebalance_d, "weights": w})
    print(rebalance_d, "→", len(syms), "members")

# In your real backtest you'd now join these weights with a price feed
# and simulate PnL. That part is yours — this library stops at the universe.
```

---

## When things go wrong

| Symptom | Fix |
|---|---|
| `ImportError: No module named nse_universe` | `uv sync` in the library repo or check your path |
| `u.members(...)` returns `[]` | Ranker hasn't run. In the lib repo, `./start.sh` → Recompute rankings |
| `FileNotFoundError: data/db/universe.duckdb` | Rebuild: `./start.sh` → Rebuild DuckDB from parquet |
| HTTPS fetch of a Parquet is slow | Use local clone or `raw.githubusercontent.com` URLs for batch work |
| Duplicate DuckDB lock error | Another process is writing. Wait or kill it |
| Results differ from last month | Rerun the ranker — a new monthly snapshot lands on the 1st |

For anything else: open an issue at https://github.com/javajack/custom-nse-500-historical-data

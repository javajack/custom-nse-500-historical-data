# nse-universe

A point-in-time **custom index membership oracle** for NSE equities, built from the public bhavcopy archive. Ask "on date D, what were the top 500 NSE stocks by median turnover, and which were in *custom nifty_50* / *midcap_150* / *smallcap_250*?" — get a deterministic answer, survivor-bias free.

Intended as a standalone service: multiple algos / backtests can depend on it without embedding universe logic of their own.

## What it is

- **Storage**: DuckDB (derived, rebuildable) over **partitioned parquet** (raw data, versioned in git).
- **Data source**: NSE's public bhavcopy zip archive (`archives.nseindia.com/content/historical/EQUITIES/...`).
- **Ranking**: monthly rebalance; rank = 6-month median daily turnover (stable proxy for mcap when no free-float data is available).
- **Custom indices**: derived from rank windows, defined in `config/indices.yml` — `nifty_50` = ranks 1–50, `midcap_150` = 101–250, `smallcap_250` = 251–500, etc. Add your own freely.
- **Corporate actions**: splits + dividends via yfinance (optional; membership is rank-ordinal and robust to splits without adjustment).
- **Interface**: Python library + menu-driven CLI + FastAPI service (for Go / Java / other consumers).

## Setup

Requires [`uv`](https://github.com/astral-sh/uv) and Python ≥ 3.11.

```bash
./start.sh                    # ensures venv + deps, launches interactive menu
```

Or manually:

```bash
uv sync                       # installs deps, creates .venv
uv run nse-universe           # launches the menu
```

## Menu-driven workflow (no cron required)

Pick `Sync bhavcopy` → tool auto-detects which trading days are missing between `history_start` (2005-01-01 by default) and today, fetches only those. Safe to interrupt; idempotent to rerun.

Pipeline:

1. **Sync bhavcopy** — fetches missing zips with anti-bot stack (see below).
2. **Ingest pending zips** — normalizes to parquet, EQ series only.
3. **Recompute rankings** — monthly snapshots; 6mo median turnover rank.
4. **Refresh corporate actions** — splits/dividends via yfinance (optional).
5. **Query universe** — interactive date + index → member list.

Or pick **Full pipeline** to chain 1→2→3.

## Anti-bot stack

NSE actively filters scrapers. This tool bypasses via:

- `curl_cffi` Chrome TLS fingerprint impersonation — defeats JA3-based filtering (biggest single win).
- Cookie bootstrap on `nseindia.com` before archive requests.
- Rotating real User-Agents (Chrome / Firefox / Edge pool).
- Jittered delays (2–5s default) honoring `Retry-After`.
- Exponential backoff + session rotation on 429/403/5xx.
- Filesystem-first cache: zip re-fetch only if missing/corrupt; 404 on past weekdays → recorded permanently as non-trading.

## Dual URL / dual schema

NSE retired the legacy `cmDDMMMYYYYbhav.csv.zip` archive around 2024-07-08 in favor of a new `BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip` with a different CSV schema (`TckrSymb`, `SctySrs`, `ClsPric`, …). Both URLs are probed on each fetch in date-appropriate order, and the ingester auto-detects schema from columns. Both legacy and new formats are supported transparently.

## Full backfill

```bash
uv run python scripts/backfill.py --pace normal
```

Runs reverse chronological (latest year first) so recent data is backtest-ready soonest. 20 years ≈ 3–5 hrs at normal pace. Safe to interrupt and rerun — all state is file-based.

## API server

Launch via menu → *Start API server* or directly:

```bash
uv run uvicorn nse_universe.api.app:app --port 8765
```

| Endpoint          | Description                                    |
|-------------------|------------------------------------------------|
| `GET /health`     | data-health stats                              |
| `GET /indices`    | list custom indices                            |
| `GET /members`    | `?date=YYYY-MM-DD&index=nifty_50` → symbols    |
| `GET /rank`       | `?symbol=RELIANCE&date=…` → rank or null       |
| `GET /is_member`  | `?symbol=…&date=…&index=…` → bool              |
| `GET /universe_at`| `?date=…` → full ranked snapshot               |
| `GET /openapi.json` | OpenAPI spec for Go / Java client codegen    |
| `GET /docs`       | Swagger UI                                     |

## Python library

```python
from datetime import date
from nse_universe import Universe

u = Universe()

u.members(date(2020, 3, 15), "nifty_50")
u.rank("RELIANCE", date(2020, 3, 15))
u.is_member("IRCON", date(2024, 2, 15), "midcap_150")

# Backtest-friendly walk:
for d, syms in u.walk(date(2015, 1, 1), date(2025, 1, 1), "nifty_100", freq="D"):
    # run strategy on `syms` for day `d`
    ...
```

## Data layout

```
data/
├── raw/YYYY/MMM/cmDDMMMYYYYbhav.csv.zip   (committed to git — immutable)
├── parquet/year=YYYY/month=MM/*.parquet   (committed to git — immutable)
├── actions/{SYMBOL}.parquet               (committed to git — yfinance snapshots)
├── state.json                             (committed to git — high-level markers)
└── db/universe.duckdb                     (NOT committed — rebuilt via menu)
```

If the repo balloons past ~1 GB, migrate `data/raw/` to Git LFS (one-line change).

## Configuration

- `config/indices.yml` — custom index definitions, `name → [rank_lo, rank_hi]`. Add entries freely; no rebuild required.
- `data/state.json` — `history_start` (default 2005-01-01), sync/rank/actions timestamps.
- Env var `NSE_UNIVERSE_DATA_DIR` — relocate data directory.

## Design tradeoffs (read before trusting blindly)

- **Turnover-rank vs. real Nifty**: ~5–10% of names at rank 450–500 may differ from actual Nifty 500 constituents. Fine for factor backtests, not for index-replication strategies.
- **Monthly rebalance**: reduces noise but intra-month membership churn is invisible. If your strategy is intra-month sensitive, shorten cadence in `rank/monthly.py`.
- **Corporate actions from yfinance**: covers splits + cash dividends (~90% of cases). Misses bonus issues not modeled as splits, rights issues, mergers, name changes. See `data/actions/` for coverage.
- **Prices are unadjusted** in `bhav_daily`. Membership is ordinal, so splits don't affect rank. If you use these prices in price-based factors, apply `compute_adj_factor` from `nse_universe.actions.fetch`.
- **DuckDB single-writer**: the ingester holds the write lock alone; concurrent readers are fine. Don't run two sync jobs in parallel.

## Public dataset site

A GitHub Pages site under `docs/` publishes every zip and Parquet at stable URLs with a clean SEO/LLM-optimized front page. Build locally:

```bash
uv run python scripts/build_docs.py
```

This generates `docs/manifest.json`, `docs/stats.json`, per-year HTML pages, `sitemap.xml`, and the 1200×630 OG image from `scripts/JetBrainsMono-Bold.ttf`. Configure your URLs once in `docs/_config.json`. The bundled GitHub Actions workflow (`.github/workflows/pages.yml`) rebuilds and deploys on every push to `main`.

## License

Private.

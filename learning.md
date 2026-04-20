# Learnings — building nse-universe

This file is a field report from building a survivorship-free NSE point-in-time index membership oracle on top of public bhavcopy archives. It is written for another engineer — or another LLM picking this up cold — to inherit everything that cost us calendar time to discover. No abstractions; just the grit.

---

## 1. What the system is, in one paragraph

Daily NSE "bhavcopy" CSV zips from ~1994 to today contain every traded symbol's OHLC + volume + turnover. If you archive every zip day by day, you get a survivorship-bias-free tape (delisted stocks are still on whichever day they traded). Rank each symbol's trailing 6-month median turnover once a month, and the top N by rank is your custom Nifty-500. Slice the rank window differently — 1–50, 101–250 — and you have a custom Nifty-50 or Midcap-150 without ever touching an index-provider feed. A Python library + FastAPI service returns "on date D, which symbols were in custom index I?" deterministically. The backing store is partitioned parquet on disk (git-committed) with a DuckDB file as a derived index (gitignored, rebuildable).

---

## 2. The data source you actually deal with, not the one in the docs

There are **two URL formats, not one**, and they publish **different CSV schemas**.

**Legacy** (stable 1994 → approximately 2024-07-05):
```
https://archives.nseindia.com/content/historical/EQUITIES/YYYY/MMM/cmDDMMMYYYYbhav.csv.zip
```
Example: `cm15JAN2024bhav.csv.zip`. `MMM` is uppercase three-letter month.

**New** (approximately 2024-07-08 onwards, possibly retroactive to some prior dates):
```
https://archives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
```
Example: `BhavCopy_NSE_CM_0_0_0_20240715_F_0000.csv.zip`.

The cutover is not clean. Don't hardcode a date cutoff — **try both URLs per date**, in the order most likely to succeed first (new for recent dates, legacy for historical). If both return 404 on a weekday in the past, treat as a non-trading day. We use ~2024-07-08 as "preference order pivot date" but the fallback logic makes both URLs available regardless.

### CSV schema — legacy

Columns (trailing comma in every line produces an `Unnamed: 13` column in pandas — drop it):
```
SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE, LAST, PREVCLOSE,
TOTTRDQTY, TOTTRDVAL, TIMESTAMP, TOTALTRADES, ISIN
```

- **Filter**: `SERIES == 'EQ'` (T-bills, SME, BZ, BE, etc. all land in the same CSV).
- **`TIMESTAMP` is not one format** — it's `%d-%b-%Y` (e.g. `15-JAN-2024`) in post-mid-2011 files and `%d-%b-%y` (e.g. `13-Jul-20`) in pre-mid-2011 files. Try 4-digit first, fall back to 2-digit, fall back to pandas `format="mixed", dayfirst=True`.
- **`TOTALTRADES` is optional** — it doesn't exist in CSVs from 2005 through ~mid-2011. Treat as nullable; emit `pd.NA` for those rows. If you enforce it in your required-columns set, every pre-2012 file quarantines and you will waste a day before you notice.

### CSV schema — new

Columns (entirely different vocabulary, derivatives + cash market mixed in the same file):
```
TradDt, BizDt, Sgmt, Src, FinInstrmTp, FinInstrmId, ISIN, TckrSymb, SctySrs,
XpryDt, FininstrmActlXpryDt, StrkPric, OptnTp, FinInstrmNm,
OpnPric, HghPric, LwPric, ClsPric, LastPric, PrvsClsgPric, UndrlygPric, SttlmPric,
OpnIntrst, ChngInOpnIntrst, TtlTradgVol, TtlTrfVal, TtlNbOfTxsExctd,
SsnId, NewBrdLotQty, Rmks, Rsvd1, Rsvd2, Rsvd3, Rsvd4
```

- **Filter to cash market equity**: `Sgmt == 'CM' AND SctySrs == 'EQ'`. Without the `Sgmt` filter you will ingest futures and options too.
- **`TradDt` is ISO `YYYY-MM-DD`** — don't try to parse it with `%d-%b-%Y`.
- `TtlNbOfTxsExctd` is the trades-count equivalent of legacy's `TOTALTRADES` — I also treat it as optional for defensive reasons.

### Detection strategy

Don't branch on URL or date — branch on **columns present in the CSV** after read. Two frozen sets `LEGACY_COLS` and `NEW_COLS` (required minus optional) are tested via `.issubset(df.columns)`. Whichever matches wins; neither match raises an informative error that names which columns are missing, which is how both "TOTALTRADES optional pre-2012" and "TtlNbOfTxsExctd optional in edge cases" were found.

---

## 3. Scraping NSE — what actually works

NSE is actively hostile to scrapers. Plain `requests.get(url)` returns 403 with correct headers because the TLS fingerprint (JA3 hash) doesn't match a real browser. Here's the stack that works:

### Layer 1 — `curl_cffi` with Chrome TLS impersonation (biggest win)

```python
from curl_cffi import requests as cffi_requests
session = cffi_requests.Session(impersonate="chrome124")
```

This makes your TLS handshake **byte-identical to Chrome 124's**. Without this, nothing else matters.

Rotate impersonation profiles on retry. We pool `chrome124, chrome120, chrome116, chrome110`. The profile switch also regenerates headers — no need to micromanage.

### Layer 2 — cookie bootstrap

NSE's archive endpoints check for cookies set by the homepage (`nsit`, `nseappid`, `bm_sv`, etc.). You must visit `https://www.nseindia.com/` with your session before the archive URL, or you get 403 even with perfect TLS.

```python
session.get("https://www.nseindia.com/", timeout=30)
time.sleep(random.uniform(0.4, 1.1))
# optionally visit another "warm" page
session.get("https://www.nseindia.com/all-reports", timeout=30)
```

Do this **once per session**, not per request. After warmup, the same session can fetch thousands of archive URLs.

### Layer 3 — realistic headers + User-Agent rotation

Pool of 7 recent real browsers (Chrome 122/123/124 on Win/Mac/Linux + Firefox 124/125 + Edge 124). Set on session creation:

```python
{
    "User-Agent": random.choice(USER_AGENTS),
    "Accept": "text/html,application/xhtml+xml,...;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}
```

Per-request override `Referer: https://www.nseindia.com/` + `Sec-Fetch-Site: same-origin`.

### Layer 4 — pacing + backoff

- Jittered delay between successful fetches: `random.uniform(2.0, 5.0)` seconds ("polite"), `1.0–3.0` ("normal"), `0.5–1.5` ("fast"). Normal is a good default for a 20-year backfill; we never got rate-limited at that pace across ~5000 requests.
- On 429 or 5xx: honor `Retry-After` if the server sent one, else exponential backoff `base * 2^attempt` capped at 32 seconds + jitter.
- On 401/403: rotate the session (new impersonation profile, new UA, fresh warmup) and retry.
- After N consecutive network errors (N=3), rotate session. Prevents a stuck keepalive from poisoning the rest of the run.

### Layer 5 — what 404 actually means

On the **past** and on a **weekday**: 404 means it was a holiday (NSE didn't publish). Record permanently in a `non_trading_days` table so you never re-probe it.

On **today's date**: 404 means "not yet published" (NSE publishes bhavcopy around 18:30 IST on trading days). Don't record as non-trading. Return "deferred" and move on — next run will pick it up.

Important: apply this distinction to **all** URLs in your fallback chain. Only mark as non-trading if *every* URL candidate returned 404. If one URL returns 403 (rotated session, retries available) and the other returns 404, the day is not non-trading, it's still retryable.

### What 403 signals you've already taken too long

If you start seeing 403s after a streak of 200s, it's usually because NSE has started flagging your session. Rotate + back off. If 403s persist across rotations, the source IP is probably tagged — wait 30 minutes.

We never hit that wall at "normal" pace (2s avg delay) across a full 20y backfill, so this is mostly defensive.

---

## 4. Storage design — why DuckDB + partitioned parquet

The combination of **partitioned parquet for durable data + DuckDB file as derived index** is a better fit for this problem than any of the obvious alternatives:

- **Everything in DuckDB**: single file churns on every write → massive git diffs. Also the file is binary-diff-hostile.
- **Everything in CSV**: text-friendly for git but slow to query at 7M rows.
- **SQLite**: pretty good, but parquet + DuckDB gives you 10× faster columnar scans and keeps raw data in a portable format.

Final layout:
```
data/
├── raw/YYYY/MMM/cmDDMMMYYYYbhav.csv.zip            # immutable, git-committed
├── parquet/year=YYYY/month=MM/YYYY-MM-DD.parquet   # deterministic, git-committed
├── actions/{SYMBOL}.parquet                         # yfinance snapshots, git-committed
├── state.json                                       # git-committed high-water marks
└── db/universe.duckdb                               # derived, gitignored
```

Hive partitioning (`year=YYYY/month=MM/`) lets DuckDB do partition pruning on date predicates automatically.

The DuckDB file is an **index, not a database**. If you delete it, `rebuild_from_parquet()` recreates everything derivable. The only data that is DuckDB-only is `universe_rank` and `adj_events`, both of which are recomputed by their own jobs.

### Git-friendly parquet

Parquet is *not* automatically byte-deterministic. Two runs can produce different bytes from the same input because:
- Row ordering depends on the input DataFrame's order.
- Pandas writes index metadata into the schema unless told not to.
- Parquet statistics embed file offsets.

Our writer:
```python
df = df.sort_values("symbol", kind="mergesort").reset_index(drop=True)
table = pa.Table.from_pandas(df, preserve_index=False)
table = table.replace_schema_metadata({})   # strip pandas-specific metadata
pq.write_table(
    table, tmp_path,
    compression="zstd", compression_level=3,
    row_group_size=50_000,
    use_dictionary=True,
    write_statistics=False,
    data_page_size=1 << 20,
)
tmp_path.replace(final_path)     # atomic rename
```

With this, re-ingesting the same zip produces byte-identical parquet. Git shows clean diffs on data updates.

---

## 5. DuckDB pitfalls you will hit

### View over glob fails on zero files

```python
con.execute("CREATE VIEW bhav_daily AS SELECT * FROM read_parquet('.../year=*/month=*/*.parquet', hive_partitioning=TRUE)")
```

On a fresh install where no parquet exists yet, `read_parquet` errors with "No files found matching the pattern." Fix: detect the empty case and create a typed empty view instead:

```python
if not any(PARQUET_DIR.glob("year=*/month=*/*.parquet")):
    con.execute("""CREATE VIEW bhav_daily AS SELECT CAST(NULL AS VARCHAR) AS symbol, ... WHERE FALSE""")
```

### Read-only opens cannot `CREATE VIEW`

If you register the view every time you open a connection, and some code path opens read-only, you get `InvalidInputException: Cannot execute statement of type "CREATE" on database ... attached in read-only mode!`. Fix: only register the view on writable opens, and trust that the view persists in the DuckDB file across read-only opens.

After writing new parquet files, the view must be re-registered (even if the SQL text is the same) for DuckDB to pick them up. The ingester does this at the end of every batch.

### Single writer

DuckDB holds an exclusive lock on the file while writing. Don't run two backfills in parallel. Readers during write: tolerated on some OSes, not others — on Linux we saw `IOException: Conflicting lock` even for read-only opens during active writes. Our policy: don't snapshot the DB during the backfill; wait for the process to quiesce between years, or query the parquet directly via a separate DuckDB connection.

### `pq.read_table(single_path)` is a dataset scan

**This cost a day to find.** `pyarrow.parquet.read_table(path)` when `path` is a file treats the path as a dataset root and scans its siblings for schema merging. If sibling files have slightly different partition column dtypes (e.g., `year` stored as `int64` in one, `dictionary<int32>` in another, which can happen if writers diverge), you get:
```
pyarrow.lib.ArrowTypeError: Unable to merge: Field year has incompatible types: int64 vs dictionary<...>
```

For **single-file roundtrip verification**, use the low-level API:
```python
pf = pq.ParquetFile(path)
tbl = pf.read()   # this file only, no sibling scan
```

Our verifier did this the wrong way first, decided all 165 files were corrupt, and wiped them all out (we had the raw zips to re-ingest, but still a scare).

### Parametric INSERT that passes a date-range window

DuckDB doesn't give you the row count from `INSERT ... SELECT` via the cursor handle reliably. Fetch a follow-up `COUNT(*)` if you need the inserted row count.

---

## 6. Ingestion quirks and the idempotency contract

The ingester is built around two invariants:

1. **Filesystem is the source of truth**. If a zip exists on disk and passes CRC, we have that day. DuckDB's `fetch_log` is a cache of this fact — if DB state disagrees with filesystem, filesystem wins on re-scan.
2. **Every operation is restart-safe**. Kill -9 at any point and re-running picks up cleanly. No locks to clean up, no half-written state files (atomic renames everywhere).

Trade-off: we accept extra work on restart (scan fetch_log, skip already-ingested rows) in exchange for not needing a write-ahead log.

### Quarantine on schema failure is destructive

When ingestion fails (bad CSV schema, unparseable timestamp, CRC failure), our first version:
- Moved the zip to `data/raw/_quarantine/`
- **Deleted the fetch_log row**

This meant: if you later fix the bug, re-running the tool doesn't find the file (it's not in fetch_log) and doesn't try to fetch again (it hasn't been marked for retry). You have to manually recover. We added a recovery recipe (move files back to original paths, restart the runner — the fetcher detects them via filesystem, re-registers fetch_log as `cached`, re-ingests with the fixed parser) and it worked fine, but the design is still "quarantine is lossy."

Recovery script for future us:
```python
import re, shutil
from pathlib import Path
from datetime import datetime
MONTHS = ("JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC")
for f in Path("data/raw/_quarantine").iterdir():
    m = re.match(r"(\d{4}-\d{2}-\d{2})", f.name)
    if not m: continue
    d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
    target = Path(f"data/raw/{d.year}/{MONTHS[d.month-1]}/cm{d.day:02d}{MONTHS[d.month-1]}{d.year}bhav.csv.zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(f, target)
```

A better design (we didn't build it — noted for v2): quarantine should preserve fetch_log but mark `ingested=FALSE, ingest_error='...'`. Then `ingest_all_pending` can retry automatically when the code is patched.

### The date-mismatch guard

After parsing the CSV, check that every row's `date` column matches the expected date derived from the filename. If not, raise. This catches: wrong file in a slot (e.g., you manually dropped a file), date format parse error that silently produced wrong dates (`%d-%m-%Y` vs `%d-%b-%Y` is a classic).

---

## 7. Ranker design — why 6-month median turnover

NSE's actual Nifty indices use free-float market cap. We don't have free-float (or even shares-outstanding) in bhavcopy. We do have turnover (`TOTTRDVAL` / `TtlTrfVal`, in rupees).

**6-month median daily turnover** is our rank metric. Why:
- **Stable**: median (not mean) rejects block-deal outliers.
- **Size-correlated**: large caps trade more in rupee terms than small caps. Correlation with free-float mcap for top-500 stocks is ~90%+.
- **Liquidity-biased, which is what you want anyway**: you can only trade what trades. A stock with huge shares-outstanding but thin volume is not a tradable member of your universe.

The disagreement with Nifty's actual constituents is ~5–10% of names at the margin (rank 450–500). Fine for factor backtests. Not fine for index replication strategies.

### IPO eligibility

A stock needs ≥126 trading days of history (6 months) before it becomes rank-eligible. This prevents a 10-day-old IPO with first-week trading frenzy from jumping into custom_nifty_50.

We enforce this inside the rank SQL (`HAVING COUNT(*) >= 126`), not as a separate filter. Clean.

### Monthly rebalance is good enough

Weekly rebalance would give finer-grain membership tracking but also introduce whipsaw in backtests (names bounce in/out as turnover wobbles). Monthly is a known-good cadence in index methodology, including Nifty's own.

First trading day of each month is `MIN(date) GROUP BY year, month` over `bhav_daily` — the calendar is derived from the data, not from a holiday table.

### The top-K cap

We store the top 1000 by rank, not just top 500. Two reasons:
- Headroom for users to define custom indices beyond `nifty_500` (e.g. `rank_1000` if they want). Fixing a tight cap at 500 precludes this without re-running the ranker.
- Storage cost is negligible — 1000 rows × 240 snapshots = 240k rows.

---

## 8. Corporate actions — yfinance coverage is 66%

Our backfill hit yfinance for 2577 NSE symbols. Results:
- **ok**: 1715 (67%)
- **no_data**: 756 (29%) — Yahoo has no record. Mostly delisted / suspended names.
- **errors**: 106 (4%) — transient (rate limit, network).

For backtests that need point-in-time price adjustment, that coverage is acceptable. Gaps are dominated by symbols that wouldn't meaningfully appear in a top-500 rank window anyway.

What yfinance **does not** cover:
- Bonus issues not modeled as splits
- Rights issues
- Mergers and symbol renames
- Spin-offs

For critical cases, supplement with NSE's Corporate Actions scraper (150–250 LOC, breaks quarterly). We did not build this — path (b) with graceful degradation was acceptable.

### Adjustment factor math

```python
# event_date is when the split goes effective
# ratio is (new / old) — e.g. 2.0 means 2-for-1 split (price halves)
# We want a factor F(d) such that raw_close(d) * F(d) == modern_share_adjusted_close(d)
# For all dates strictly before event_date of split i, divide by ratio_i.
# Equivalently, cumulative product of (1/ratio_i) for all splits i with event_date > d.

factors = (1.0 / splits["ratio"]).iloc[::-1].cumprod().iloc[::-1]
# e.g. RELIANCE with 4 × 2:1 splits → earliest factor = 0.0625 = 1/16
```

Reverse-cumulative product, indexed by event_date. Apply as `close * factor_right_before_date(close_date)`.

---

## 9. Performance observations (Ryzen 9 laptop, 100Mbit link)

At **normal pace** (1–3s jittered delay):
- ~2.2 seconds per bhavcopy request (median, including retry budget amortized).
- **~9 minutes per full trading year** (~250 days).
- **21 years ≈ 3 hours 10 minutes** for the fetch phase.
- Cached years re-scan in **1 second** (filesystem check dominates).

Ingest:
- ~2000 rows per NSE EQ day, filtered from ~3500 total CSV rows.
- ~200ms per day to parse + normalize + write parquet.
- **~50 minutes** to ingest 5000+ days serially.

Verify:
- 5256 zips + 5256 parquets in **~4 seconds** using `pq.ParquetFile(path).read()` path.

Rank:
- 248 monthly snapshots × 126-day window × 4000 symbols = ~120M row-comparisons.
- DuckDB does it in **51 seconds**. SQL expresses it cleanly:

```sql
WITH window_rows AS (
    SELECT symbol, turnover,
           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
      FROM bhav_daily
     WHERE date < :as_of AND date >= :window_start AND turnover IS NOT NULL
),
last126 AS (SELECT symbol, turnover FROM window_rows WHERE rn <= 126),
eligible AS (
    SELECT symbol, COUNT(*) AS n_days, MEDIAN(turnover) AS med_turnover
      FROM last126 GROUP BY symbol HAVING COUNT(*) >= 126
),
ranked AS (
    SELECT symbol, med_turnover,
           ROW_NUMBER() OVER (ORDER BY med_turnover DESC, symbol) AS rnk
      FROM eligible
)
INSERT INTO universe_rank SELECT :as_of, symbol, rnk, med_turnover, 'turnover_median_126d' FROM ranked WHERE rnk <= 1000
```

Actions:
- yfinance serial: ~1s per symbol. 2500 symbols = 40 minutes.
- With `ThreadPoolExecutor(max_workers=4)`: **~7 minutes**. Yahoo tolerates this well.

---

## 10. Storage volume — legacy vs new format, year over year

Measured at run-time:

| Era | Format | Avg zip/day | Avg parquet/day | Notes |
|-----|--------|-------------|-----------------|-------|
| 2005–2023 | Legacy | 80–90 KB | 80 KB | ~2000 EQ symbols/day |
| 2024 (pre-cutover) | Legacy | 131 KB | 86 KB | transition year |
| 2024–2026 (post-cutover) | New | 170 KB | 100 KB | ~3500 rows/day (derivatives mixed in before filter); extra columns |

**Full 21-year total**: 345 MB raw + 327 MB parquet + 7 MB actions + 25 MB DuckDB (local-only) = **~700 MB**.

This is under the 1 GB threshold where Git LFS starts making sense. For a 30+ year dataset, flip to LFS.

---

## 11. Operational patterns that paid off

### Menu-driven CLI, not cron

The user explicitly vetoed cron. The menu-driven CLI (`questionary` + `rich`) means:
- User runs it when they want, watches progress, interrupts if something looks off.
- No "why didn't my cron run last night" debugging.
- Delta detection is automatic: each action scans existing state and only fetches what's missing.

Menu actions: Sync · Ingest · Actions · Rank · Full pipeline · Query · Health · Verify · Rebuild DuckDB · Start API · Exit.

### Background runner + event-stream monitor

For the initial 20y backfill we wrote `scripts/backfill.py`, launched via `nohup` so it survives parent process death, with single-line structured log records (`YEAR=2024 SYNC_DONE in=474s counts={...}`). A `tail -F` + `grep` pipeline watches for year milestones and error signatures; events arrive as notifications. The filter must cover **failure signatures too** — if you only grep for success tokens, a crash produces silence that looks identical to "still running."

### Resume-from-state

Kill the backfill mid-request. Restart it with the same args. It:
1. Re-scans `fetch_log` and filesystem.
2. Skips already-fetched zips (cached).
3. Resumes from the first unfinished date.
4. Keeps partially-ingested state consistent via transactional per-day commits.

Total overhead of restart: ~5 seconds for the scan.

### Deterministic everything

Deterministic parquet + atomic renames + idempotent fetch = git diffs only show genuine data changes. Zero "noise commits" across dozens of test runs.

---

## 12. Bugs hit and fixed — in chronological order

1. **Verifier wiped 165 good parquet files** because `pq.read_table(path)` triggered dataset-scan across siblings and failed on partition dtype mismatch. Fix: `pq.ParquetFile(path).read()` for single-file reads.
2. **All of 2025 marked non_trading** because the legacy URL returns 404 for dates past mid-July 2024. Fix: dual-URL probe + dual-schema ingester. Cleared 266 bogus `holiday_404` rows before restart.
3. **New CSV schema missing**: `TckrSymb`, `SctySrs`, `ClsPric`, etc. Fix: added `NEW_COLS` schema + `_normalize_new()`.
4. **2-digit year**: 2020-07-13's TIMESTAMP is `13-Jul-20` not `13-Jul-2020`. Fix: multi-format fallback in `_parse_legacy_timestamp`.
5. **`TOTALTRADES` absent in pre-2012 CSVs** → 117 files quarantined in one ingest pass. Fix: make column optional (emit `pd.NA`), recover files from quarantine, restart.
6. **Runner's per-year verify was O(all_files) each year** → would have dominated runtime for 20y. Fix: one verify pass at the end.
7. **DuckDB read-only open tried to CREATE VIEW**. Fix: register view only on writable opens.
8. **DuckDB fresh install with zero parquet files**: `read_parquet` errored on empty glob. Fix: typed empty view fallback.

Every fix was a one-file patch + "recover files from quarantine + restart the background runner." No data loss across any of them.

---

## 13. What I would do differently next time

- **Quarantine should preserve `fetch_log` rows** with an `ingest_error` column instead of deleting them. Would have made bug #5 a zero-intervention recovery.
- **Schema detection should be more defensive** — treat every non-OHLC column as optional by default, only hard-require symbol/date/close/volume/turnover.
- **Scraper with a config-driven URL list** — adding a third URL format down the road should be an indices.yml-like config edit, not a code change.
- **A `data_gaps` report** that diffs the full weekday-date range against `fetch_log ∪ non_trading_days` and tells you exactly which days are missing. We have the pieces; never wired up the report.
- **yfinance calls should be batched** per symbol rather than one at a time. yfinance 0.2+ supports `yf.Tickers(...)` but the per-symbol actions API doesn't benefit from batching the way `download()` does. Revisit when yfinance API improves.

---

## 14. How to pick this up cold

```bash
cd nse500
uv sync
uv run nse-universe     # interactive menu
```

Menu → Full pipeline → accept defaults → wait 3 hours (first time) or 30 seconds (subsequent runs).

Or for one-shot backfill:
```bash
uv run python scripts/backfill.py --pace normal
```

Programmatic:
```python
from datetime import date
from nse_universe import Universe
u = Universe()
u.members(date(2018, 6, 1), "nifty_50")   # 50 symbols
u.is_member("RELIANCE", date(2010, 1, 1), "nifty_50")   # bool
for d, syms in u.walk(date(2015, 1, 1), date(2025, 1, 1), "midcap_150"):
    ...   # per-trading-day universe
```

Key files to read first if you're extending:
- `src/nse_universe/fetch/bhav.py` — the fetch state machine with URL fallback
- `src/nse_universe/ingest/bhav.py` — both CSV schemas in one module
- `src/nse_universe/rank/monthly.py` — the rank SQL
- `src/nse_universe/core/universe.py` — the library surface
- `scripts/backfill.py` — the resumable runner

Read `README.md` for user-facing docs; read this file for when things break.

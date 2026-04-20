"""Menu-driven CLI — user stays in control, no cron required.

Each action computes its own delta:
  - Bhavcopy sync: scans fetch_log + non_trading_days, fetches only the gaps
    from history_start to today.
  - Ranker: re-runs only as_of_dates not yet computed.
  - Actions: defaults to refreshing the active universe.

Interrupt-safe throughout (Ctrl-C between operations, state persists).
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

import questionary
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from nse_universe import Universe
from nse_universe.core import state as state_mod
from nse_universe.core.db import db, has_any_parquet, rebuild_from_parquet
from nse_universe.fetch.bhav import FetchConfig, sync_range
from nse_universe.ingest.bhav import ingest_all_pending
from nse_universe.paths import DB_PATH, RAW_DIR

console = Console()
log = logging.getLogger(__name__)

MENU_SYNC = "Sync bhavcopy (auto-detect missing trading days)"
MENU_INGEST = "Ingest pending zips into parquet"
MENU_ACTIONS = "Refresh corporate actions (yfinance)"
MENU_RANK = "Recompute monthly rankings"
MENU_FULL = "Full pipeline: sync → ingest → rank → actions"
MENU_QUERY = "Query universe (date + index)"
MENU_HEALTH = "Data health / stats"
MENU_VERIFY = "Verify integrity (zip CRC + parquet roundtrip)"
MENU_REBUILD = "Rebuild DuckDB from parquet (derived)"
MENU_API = "Start API server"
MENU_EXIT = "Exit"

MAIN_CHOICES = [
    MENU_SYNC, MENU_INGEST, MENU_ACTIONS, MENU_RANK, MENU_FULL,
    MENU_QUERY, MENU_HEALTH, MENU_VERIFY, MENU_REBUILD, MENU_API, MENU_EXIT,
]


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw.strip())


def _history_start() -> date:
    return _parse_date(state_mod.load().history_start)


def _make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        console=console,
    )


# -------------------- actions --------------------

def action_sync() -> None:
    start_default = _history_start()
    today = date.today()

    start_raw = questionary.text(
        f"Start date (YYYY-MM-DD)",
        default=start_default.isoformat(),
    ).ask()
    if start_raw is None:
        return
    end_raw = questionary.text(
        "End date (YYYY-MM-DD, inclusive)",
        default=today.isoformat(),
    ).ask()
    if end_raw is None:
        return

    try:
        start = _parse_date(start_raw)
        end = _parse_date(end_raw)
    except ValueError as e:
        console.print(f"[red]bad date: {e}[/red]")
        return

    order = questionary.select(
        "Order",
        choices=["reverse (latest first)", "forward (oldest first)"],
        default="reverse (latest first)",
    ).ask()
    if order is None:
        return
    reverse = order.startswith("reverse")

    polite_pool = ["polite (2-5s delay, safest)", "normal (1-3s)", "fast (0.5-1.5s)"]
    pace = questionary.select("Pacing", choices=polite_pool, default=polite_pool[0]).ask()
    if pace is None:
        return
    cfg_map = {
        polite_pool[0]: FetchConfig(min_delay_s=2.0, max_delay_s=5.0),
        polite_pool[1]: FetchConfig(min_delay_s=1.0, max_delay_s=3.0),
        polite_pool[2]: FetchConfig(min_delay_s=0.5, max_delay_s=1.5),
    }
    cfg = cfg_map[pace]

    state_mod.mark_sync_attempt()
    console.print(f"[cyan]syncing {start} → {end} ({order}) …[/cyan]")
    days_est = (end - start).days + 1
    with _make_progress() as prog:
        task = prog.add_task("sync", total=days_est)

        def cb(i: int, n: int, d: date, outcome):
            prog.update(task, completed=i, total=n,
                        description=f"[{outcome.value:<15s}] {d}")

        counts = sync_range(start, end, cfg=cfg, reverse=reverse, progress_cb=cb)
    state_mod.mark_sync_complete()
    _print_counts("Sync results", counts)


def action_ingest() -> None:
    console.print("[cyan]ingesting pending zips …[/cyan]")
    with _make_progress() as prog:
        task = prog.add_task("ingest", total=None)

        def cb(i: int, n: int, d: date, res):
            prog.update(task, completed=i, total=n,
                        description=f"[{res.status:<12s}] {d} rows={res.rows}")

        counts = ingest_all_pending(progress_cb=cb)
    _print_counts("Ingest results", counts)


def action_rank() -> None:
    from nse_universe.rank.monthly import recompute_all

    force = questionary.confirm(
        "Force recompute ALL as_of_dates (slow)? No = incremental.",
        default=False,
    ).ask()
    if force is None:
        return

    console.print("[cyan]computing monthly ranks …[/cyan]")
    with _make_progress() as prog:
        task = prog.add_task("rank", total=None)

        def cb(i: int, n: int, d: date, rows: int):
            prog.update(task, completed=i, total=n,
                        description=f"rank {d} ({rows} syms)")

        stats = recompute_all(force=force, progress_cb=cb)
    console.print(f"[green]ranked {stats.as_of_dates} snapshots, {stats.total_rows} rows[/green]")


def action_actions() -> None:
    from nse_universe.actions.fetch import refresh_actions

    use_full = questionary.confirm(
        "Refresh for full recent universe (slow, ~20 min for 2000 symbols)? "
        "No = pick a symbol list.",
        default=True,
    ).ask()
    if use_full is None:
        return

    symbols: list[str] | None = None
    if not use_full:
        raw = questionary.text("Symbols (comma-separated)", default="RELIANCE,TCS,INFY").ask()
        if raw is None:
            return
        symbols = [s.strip() for s in raw.split(",") if s.strip()]

    console.print("[cyan]fetching corporate actions via yfinance …[/cyan]")
    with _make_progress() as prog:
        task = prog.add_task("actions", total=None)

        def cb(i: int, n: int, sym: str, status: str):
            prog.update(task, completed=i, total=n,
                        description=f"[{status:<7s}] {sym}")

        r = refresh_actions(symbols=symbols, progress_cb=cb)
    state_mod.mark_actions_refreshed()
    console.print(
        f"[green]total={r.total}  ok={r.ok}  no_data={r.no_data}  errors={r.errors}  "
        f"splits={r.splits}  dividends={r.dividends}[/green]"
    )
    if r.gaps:
        console.print(f"[yellow]first 10 gaps: {r.gaps[:10]}[/yellow]")


def action_full_pipeline() -> None:
    console.print("[bold cyan]=== full pipeline start ===[/bold cyan]")
    action_sync()
    action_ingest()
    action_rank()
    if questionary.confirm("Also refresh corporate actions?", default=False).ask():
        action_actions()
    console.print("[bold green]=== pipeline done ===[/bold green]")


def action_query() -> None:
    u = Universe()
    raw = questionary.text("Date (YYYY-MM-DD)", default=date.today().isoformat()).ask()
    if raw is None:
        return
    try:
        d = _parse_date(raw)
    except ValueError as e:
        console.print(f"[red]bad date: {e}[/red]")
        return
    idx = questionary.select("Index", choices=u.indices()).ask()
    if idx is None:
        return
    asof = u.as_of_for(d)
    console.print(f"[cyan]query {d}  •  index {idx}  •  as_of {asof}[/cyan]")
    members = u.members(d, idx)
    if not members:
        console.print("[yellow]no members — did you run rank yet?[/yellow]")
        return

    show_ranks = questionary.confirm("Show with rank + turnover?", default=True).ask()
    if not show_ranks:
        console.print(", ".join(members))
        return
    spec = u.index_spec(idx)
    with db(read_only=True) as con:
        rows = con.execute(
            """
            SELECT rank, symbol, metric_value
              FROM universe_rank
             WHERE as_of_date = ? AND rank BETWEEN ? AND ?
             ORDER BY rank
            """,
            [asof, spec.rank_lo, spec.rank_hi],
        ).fetchall()
    tbl = Table(title=f"{idx} on {d} (as_of {asof})")
    tbl.add_column("rank", justify="right")
    tbl.add_column("symbol")
    tbl.add_column("6mo median turnover (Rs cr)", justify="right")
    for rank_, sym, mv in rows[:200]:
        tbl.add_row(str(rank_), sym, f"{(mv or 0)/1e7:,.2f}")
    console.print(tbl)
    if len(rows) > 200:
        console.print(f"[dim]… ({len(rows) - 200} more)[/dim]")


def action_health() -> None:
    u = Universe()
    h = u.health()
    s = state_mod.load()
    tbl = Table(title="Data Health", show_header=False)
    tbl.add_column("metric")
    tbl.add_column("value", justify="right")
    for k, v in h.items():
        tbl.add_row(k, str(v))
    tbl.add_row("", "")
    tbl.add_row("history_start", s.history_start)
    tbl.add_row("last_sync_completed_at", s.last_sync_completed_at or "-")
    tbl.add_row("last_rank_computed_at", s.last_rank_computed_at or "-")
    tbl.add_row("last_actions_refreshed_at", s.last_actions_refreshed_at or "-")
    tbl.add_row("raw_zip_dir", str(RAW_DIR))
    tbl.add_row("duckdb_path", str(DB_PATH))
    console.print(tbl)


def action_verify() -> None:
    from nse_universe.core.verify import verify_all

    console.print("[cyan]scanning zips + parquet for corruption …[/cyan]")
    with _make_progress() as prog:
        task = prog.add_task("verify", total=None)

        def cb(i: int, n: int, name: str, status: str):
            prog.update(task, completed=i, total=n,
                        description=f"[{status:<10s}] {name}")

        report = verify_all(progress_cb=cb)
    _print_counts("Verify results", report.as_dict())
    if report.zips_quarantined or report.parquets_removed:
        console.print(
            "[yellow]corruption found — re-run Sync + Ingest to re-fetch the affected days[/yellow]"
        )


def action_rebuild() -> None:
    if not questionary.confirm(
        "Rebuild DuckDB derived tables from parquet? (Safe, idempotent.)",
        default=True,
    ).ask():
        return
    console.print("[cyan]rebuilding derived tables from parquet …[/cyan]")
    if not has_any_parquet():
        console.print("[yellow]no parquet files — nothing to rebuild[/yellow]")
        return
    stats = rebuild_from_parquet()
    console.print(f"[green]rebuilt: symbols={stats['symbols']}  rows={stats['rows']}[/green]")


def action_api() -> None:
    import uvicorn
    host = questionary.text("Host", default="127.0.0.1").ask()
    port_raw = questionary.text("Port", default="8765").ask()
    if host is None or port_raw is None:
        return
    try:
        port = int(port_raw)
    except ValueError:
        console.print("[red]bad port[/red]")
        return
    console.print(f"[cyan]starting API on http://{host}:{port}  (Ctrl-C to stop)[/cyan]")
    console.print(f"[dim]openapi → http://{host}:{port}/openapi.json  • docs → /docs[/dim]")
    uvicorn.run("nse_universe.api.app:app", host=host, port=port, reload=False)


# -------------------- plumbing --------------------

def _print_counts(title: str, counts: dict) -> None:
    tbl = Table(title=title, show_header=False)
    tbl.add_column("key")
    tbl.add_column("count", justify="right")
    for k, v in counts.items():
        tbl.add_row(k, str(v))
    console.print(tbl)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    console.print("[bold]NSE Universe Manager[/bold]  —  local, menu-driven, survivorship-free")
    while True:
        try:
            choice = questionary.select("Action", choices=MAIN_CHOICES).ask()
        except KeyboardInterrupt:
            console.print("[yellow]bye[/yellow]")
            return
        if choice is None or choice == MENU_EXIT:
            return
        try:
            dispatch = {
                MENU_SYNC: action_sync,
                MENU_INGEST: action_ingest,
                MENU_ACTIONS: action_actions,
                MENU_RANK: action_rank,
                MENU_FULL: action_full_pipeline,
                MENU_QUERY: action_query,
                MENU_HEALTH: action_health,
                MENU_VERIFY: action_verify,
                MENU_REBUILD: action_rebuild,
                MENU_API: action_api,
            }[choice]
            dispatch()
        except KeyboardInterrupt:
            console.print("[yellow]interrupted — returning to menu[/yellow]")
        except Exception:
            console.print_exception()
            console.print("[red]action failed — returning to menu[/red]")


if __name__ == "__main__":
    main()

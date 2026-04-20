"""FastAPI wrapper around Universe. Thin and deterministic — all logic
lives in the library; this file only translates.

OpenAPI spec at /openapi.json, interactive docs at /docs. Use the spec
to generate Go / Java / TypeScript clients without hand-rolling code.

Language-agnostic consumers should prefer this surface. Python consumers
can import `nse_universe.Universe` directly for better performance.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from nse_universe import Universe
from nse_universe.core.universe import UnknownIndexError

app = FastAPI(
    title="NSE Universe",
    version="0.1.0",
    description=(
        "Point-in-time custom index membership oracle. Given a date and a "
        "custom index (e.g. `nifty_50`, `midcap_150`), returns the eligible "
        "NSE symbols. Survivorship-bias free; backed by NSE bhavcopy archives."
    ),
)

_universe_singleton: Universe | None = None


def _u() -> Universe:
    global _universe_singleton
    if _universe_singleton is None:
        _universe_singleton = Universe()
    return _universe_singleton


# --------- schemas ---------

class IndexSpecOut(BaseModel):
    name: str
    rank_lo: int
    rank_hi: int
    description: str = ""


class RankRow(BaseModel):
    rank: int
    symbol: str
    metric_value: float = Field(..., description="6mo median daily turnover, in rupees")


class MembersOut(BaseModel):
    date: date
    index: str
    as_of_date: date | None
    count: int
    symbols: list[str]


class RankOut(BaseModel):
    symbol: str
    date: date
    as_of_date: date | None
    rank: int | None


class IsMemberOut(BaseModel):
    symbol: str
    date: date
    index: str
    is_member: bool


class UniverseAtOut(BaseModel):
    date: date
    as_of_date: date | None
    count: int
    rows: list[RankRow]


class HealthOut(BaseModel):
    trading_days: int
    first_date: date | None
    last_date: date | None
    rows_bhav: int
    distinct_symbols: int
    non_trading_days_recorded: int
    rank_snapshots: int
    adj_events: int
    symbols_with_actions: int


# --------- endpoints ---------

@app.get("/health", response_model=HealthOut)
def health() -> Any:
    h = _u().health()
    return h


@app.get("/indices", response_model=list[IndexSpecOut])
def indices() -> Any:
    u = _u()
    return [
        IndexSpecOut(name=n, rank_lo=s.rank_lo, rank_hi=s.rank_hi, description=s.description)
        for n, s in ((n, u.index_spec(n)) for n in u.indices())
    ]


@app.get("/members", response_model=MembersOut)
def members(
    d: Annotated[date, Query(alias="date", description="Query date (trading day)")],
    index: Annotated[str, Query(description="Index name — see GET /indices")],
) -> Any:
    u = _u()
    try:
        syms = u.members(d, index)
    except UnknownIndexError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return MembersOut(
        date=d, index=index, as_of_date=u.as_of_for(d), count=len(syms), symbols=syms
    )


@app.get("/rank", response_model=RankOut)
def rank(
    symbol: Annotated[str, Query(description="NSE symbol, e.g. RELIANCE")],
    d: Annotated[date, Query(alias="date")],
) -> Any:
    u = _u()
    return RankOut(symbol=symbol, date=d, as_of_date=u.as_of_for(d), rank=u.rank(symbol, d))


@app.get("/is_member", response_model=IsMemberOut)
def is_member(
    symbol: Annotated[str, Query()],
    d: Annotated[date, Query(alias="date")],
    index: Annotated[str, Query()],
) -> Any:
    u = _u()
    try:
        ok = u.is_member(symbol, d, index)
    except UnknownIndexError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return IsMemberOut(symbol=symbol, date=d, index=index, is_member=ok)


@app.get("/universe_at", response_model=UniverseAtOut)
def universe_at(
    d: Annotated[date, Query(alias="date")],
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> Any:
    u = _u()
    df = u.universe_at(d)
    if df.empty:
        return UniverseAtOut(date=d, as_of_date=None, count=0, rows=[])
    rows = [
        RankRow(rank=int(r["rank"]), symbol=r["symbol"], metric_value=float(r["metric_value"]))
        for _, r in df.head(limit).iterrows()
    ]
    return UniverseAtOut(
        date=d, as_of_date=df["as_of_date"].iloc[0], count=len(df), rows=rows
    )

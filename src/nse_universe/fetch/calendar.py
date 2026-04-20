"""Trading-day candidate generator.

We don't ship a hardcoded NSE holiday calendar — holidays are *discovered* at
fetch time: a 404 for a weekday means NSE never published a bhavcopy, so it
was a non-trading day. The discovery is persisted to `non_trading_days`.

This generator yields weekdays only; weekends are filed under non_trading_days
on first encounter.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterator


def weekday_range(start: date, end_inclusive: date) -> Iterator[date]:
    """Yield Mon–Fri dates in [start, end_inclusive]."""
    d = start
    while d <= end_inclusive:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5

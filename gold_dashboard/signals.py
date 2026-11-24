"""Single source of truth for "is this indicator's signal gold-friendly?"

Every place that decides whether a close sits on the gold-friendly side of
its own moving average — the main dashboard's cell highlighting
(build_table.py), the backtest's green_count (backtest.py), and the main
dashboard's chart shading (app.py) — must call `gold_friendly_vs_ma()`
below rather than re-implementing the close-vs-MA comparison. This keeps
the three surfaces provably consistent instead of risking silent drift
between separately hand-written comparisons.
"""

import pandas as pd

from . import config
from . import metrics


def gold_friendly_vs_ma(value: pd.Series, sma: pd.Series, direction: str) -> pd.Series:
    """Boolean series: True on days `value` sits on the gold-friendly side of
    its own SMA, given the indicator's correlation `direction`
    (config.CORRELATION_DIRECTION):

    - "inverse" (real rate, DXY): gold-friendly when value <= sma (at/below
      its own MA — a rate/dollar breakout *above* its MA is bearish for gold).
    - "positive" (WTI, VIX): gold-friendly when value > sma (breakout above).
    """
    if direction == "inverse":
        return (value <= sma).fillna(False)
    return (value > sma).fillna(False)


def indicator_direction(indicator_key: str) -> str:
    return config.CORRELATION_DIRECTION[indicator_key]


def compute_green_count(real_rate: pd.Series, dxy: pd.Series) -> pd.Series:
    """0-6 count of how many of real_rate/dxy's config.MA_WINDOWS (3 windows
    each) currently sit on the gold-friendly side of their own calendar-day
    SMA — the backtest's core macro signal (see gold_dashboard.backtest's
    BUY_GREEN_COUNT/SELL_GREEN_COUNT).

    Deliberately a pure function of real_rate/dxy ALONE, with no dependency
    on "gold" or which gold-proxy basis is in play —
    timeseries.fetch_backtest_frame() calls this on a real_rate+dxy-only
    merged frame, BEFORE joining in the basis-specific "gold" column, and
    that separation is load-bearing, not cosmetic: metrics.compute_sma()'s
    rolling window is a per-ROW mean over whatever dates happen to be in its
    input series, not a true calendar-weighted integral — so if green_count
    were instead computed after merging in "gold" (as it was before
    2026-11-24), a basis whose own trading calendar contributes extra dates
    into that outer join (e.g. ⑦ 비트코인 trades every single calendar day,
    including weekends, unlike real_rate/dxy's own weekday-only calendar)
    would silently add duplicate (forward-filled) real_rate/dxy rows into
    the SMA's rolling window and change green_count on some days — even
    though green_count is conceptually defined purely in terms of
    real_rate/dxy and must come out identical across every basis (up to the
    known, intentional ±1-day shift some bases apply to real_rate/dxy before
    calling this — see fetch_backtest_frame's own docstring). Confirmed
    empirically 2026-11-24, before this split: ⑦ 비트코인 disagreed with
    every other (shifted) basis on ~5% of trading days (102/1893 over a
    5-year window), and even ①/② disagreed on ~10% of days after accounting
    for their 1-day shift — both symptoms of this same row-density artifact,
    not of the shift itself.
    """
    frame = pd.DataFrame({"real_rate": real_rate, "dxy": dxy})
    gf_cols = []
    for col in ("real_rate", "dxy"):
        direction = indicator_direction(col)
        for window in config.MA_WINDOWS:
            sma = metrics.compute_sma(frame[col], window)
            gf_col = f"{col}_gf_{window}"
            frame[gf_col] = gold_friendly_vs_ma(frame[col], sma, direction)
            gf_cols.append(gf_col)
    return frame[gf_cols].sum(axis=1).astype(int)


def all_windows_gold_friendly_for(indicator_key: str, value: pd.Series, smas: dict) -> pd.Series:
    """AND across every {window: sma_series} in `smas` (e.g. {7: sma7, 30:
    sma30, 90: sma90}): True only on days ALL of those windows agree the
    indicator (resolving its correlation direction from config) is
    gold-friendly. This is the condition used for the main dashboard's
    per-indicator chart shading — a stricter, single-indicator condition
    than any one MA-row's cell highlight, and different from (though built
    from the same gold_friendly_vs_ma() primitive as) the backtest's
    green_count, which sums single-window flags across real_rate AND dxy.
    """
    direction = indicator_direction(indicator_key)
    flags = [gold_friendly_vs_ma(value, sma, direction) for sma in smas.values()]
    result = flags[0]
    for flag in flags[1:]:
        result = result & flag
    return result

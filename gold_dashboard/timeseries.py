"""Shared time series fetchers, reused by both the backtest
(gold_dashboard/backtest.py) and the main dashboard's per-indicator charts
(app.py) so the fetch-window logic lives in exactly one place. Each fetcher
takes an explicit `years` argument (default: YEARS below) — the backtest page
lets the user adjust this per its own independent session state, while the
main dashboard always passes its own fixed value, so the two never affect
each other.
"""

from datetime import date, timedelta

import pandas as pd

from . import config
from . import data_sources as ds
from . import metrics
from .timeutil import today_kst

DXY_TICKERS = ["DX-Y.NYB", "^DXY", "DX=F"]
YEARS = 7
BUFFER_DAYS = 90  # extra calendar days of history fetched before the display/analysis
# start, so a 60-day SMA already has a full window on day 1 of that period.

# Raw single-series sources, keyed by a short id (not all of these are dashboard
# indicator keys — "gold"/"silver" are the two legs of the gold/silver ratio).
_FRED_SERIES_IDS = {"real_rate": "DFII10", "wti": "DCOILWTICO"}
_YFINANCE_TICKERS = {"dxy": DXY_TICKERS, "gold": "GC=F", "silver": "SI=F", "vix": "^VIX"}


def fetch_raw_series(
    key: str, as_of: date | None = None, years: int = YEARS, buffer_days: int = BUFFER_DAYS
) -> pd.Series:
    """Fetch one raw series (real_rate/dxy/gold/silver/wti/vix) covering
    `years` + `buffer_days` of history ending at `as_of` (default: today, KST).
    `buffer_days` defaults to BUFFER_DAYS (enough to warm up a 60-day SMA) but
    callers needing a longer rolling window (e.g. the backtest's 200-day
    long-term trend filter) can pass a bigger value."""
    end_date = as_of or today_kst()
    fetch_start = end_date - timedelta(days=years * 365 + buffer_days)
    yf_end = end_date + timedelta(days=1)  # yfinance's `end` is exclusive

    if key in _FRED_SERIES_IDS:
        series = ds.fetch_fred_series(_FRED_SERIES_IDS[key], end=end_date)
        return series[series.index >= pd.Timestamp(fetch_start)]
    if key in _YFINANCE_TICKERS:
        return ds.fetch_yfinance_close(_YFINANCE_TICKERS[key], start=fetch_start, end=yf_end)
    raise ValueError(f"unknown series key: {key}")


def gold_fetch_start(as_of: date | None, years: int, buffer_days: int) -> date:
    """The (unclamped) start date a gold price fetch would use for this
    years/buffer_days window — shared by fetch_gold_price_series (which clamps
    it to KRX_GOLD_EARLIEST_DATE under the "krx" basis) and the UI (which uses
    the same, unclamped, computation to decide whether to show a heads-up that
    clamping will happen)."""
    end_date = as_of or today_kst()
    return end_date - timedelta(days=years * 365 + buffer_days)


def gold_window_would_clamp_to_krx(as_of: date | None, years: int, buffer_days: int) -> bool:
    """True if fetch_gold_price_series(..., basis=KRX) would have to clamp this
    window's start date forward to KRX_GOLD_EARLIEST_DATE (i.e. the requested
    window reaches further back than KRX's gold-spot market has ever traded)."""
    return gold_fetch_start(as_of, years, buffer_days) < config.KRX_GOLD_EARLIEST_DATE


def fetch_gold_price_series(
    as_of: date | None = None,
    years: int = YEARS,
    buffer_days: int = BUFFER_DAYS,
    basis: str = config.GOLD_PRICE_BASIS_DEFAULT,
) -> pd.Series:
    """Fetch the gold price series used for signals/MAs/P&L under the given
    basis: GC=F (USD/oz) for "intl" (unchanged from before this option
    existed), or KRX's actual domestic gold-spot market (04020000, KRW/g) for
    "krx" — a real quote, not a currency conversion. The "krx" window is
    silently clamped forward to KRX_GOLD_EARLIEST_DATE if it would otherwise
    start before the market existed (see gold_window_would_clamp_to_krx, which
    the UI calls with the same arguments to warn the user before this happens)."""
    if basis == config.GOLD_PRICE_BASIS_KRX:
        end_date = as_of or today_kst()
        fetch_start = max(gold_fetch_start(end_date, years, buffer_days), config.KRX_GOLD_EARLIEST_DATE)
        full = ds.fetch_krx_gold_krw_per_gram()
        series = full[(full.index >= pd.Timestamp(fetch_start)) & (full.index <= pd.Timestamp(end_date))]
        if series.empty:
            raise RuntimeError("선택한 분석 기간에 해당하는 KRX 금현물 데이터가 없습니다.")
        return series
    return fetch_raw_series("gold", as_of, years=years, buffer_days=buffer_days)


def fetch_backtest_frame(
    as_of: date | None = None,
    years: int = YEARS,
    buffer_days: int = BUFFER_DAYS,
    gold_price_basis: str = config.GOLD_PRICE_BASIS_DEFAULT,
) -> pd.DataFrame:
    """Fetch real_rate/dxy/gold/gold_intl/silver as one date-aligned, forward-
    filled frame for the trading backtest, covering `years` of history (+
    `buffer_days` ahead of it, to warm up rolling-window signals before the
    display start — see fetch_raw_series). Different markets close on
    different days (rates vs. commodities vs. KRX), so the series are joined
    on the union of their dates and gaps are forward-filled from the prior
    available value.

    `gold_price_basis` selects what the "gold" column (used for every MA/trend
    filter/trigger/P&L computation) actually is — see fetch_gold_price_series.
    "gold_intl" is always GC=F regardless of that choice: the gold/silver
    ratio trigger is deliberately computed only from international USD/oz
    prices (matching the main dashboard's own gold/silver ratio row, which is
    never affected by this setting either), since a KRW/g-over-USD/oz ratio
    would be meaningless.
    """
    end_date = as_of or today_kst()
    real_rate = fetch_raw_series("real_rate", end_date, years=years, buffer_days=buffer_days)
    dxy = fetch_raw_series("dxy", end_date, years=years, buffer_days=buffer_days)
    gold = fetch_gold_price_series(end_date, years=years, buffer_days=buffer_days, basis=gold_price_basis)
    gold_intl = (
        gold
        if gold_price_basis == config.GOLD_PRICE_BASIS_INTL
        else fetch_raw_series("gold", end_date, years=years, buffer_days=buffer_days)
    )
    silver = fetch_raw_series("silver", end_date, years=years, buffer_days=buffer_days)

    df = pd.concat(
        [
            real_rate.rename("real_rate"),
            dxy.rename("dxy"),
            gold.rename("gold"),
            gold_intl.rename("gold_intl"),
            silver.rename("silver"),
        ],
        axis=1,
        join="outer",
    ).sort_index()
    df = df[df.index <= pd.Timestamp(end_date)]
    df = df.ffill()
    df = df.dropna()  # drop the leading stretch before all five series have started
    return df


# Dashboard indicator key -> the raw series id that carries its value.
_INDICATOR_SOURCE = {"real_rate": "real_rate", "dxy": "dxy", "wti": "wti", "vix": "vix"}


def build_indicator_chart_data(key: str, as_of: date | None = None, years: int = YEARS) -> dict:
    """Data for one indicator's history chart: its own daily series (trimmed to
    the last `years`), 5/30/60-day SMAs of it (skipped for gold_silver_ratio,
    which has no MA concept), and gold's own daily series for comparison.

    Each returned series keeps its own native trading-calendar dates (no
    cross-series alignment/forward-fill) since they're drawn as independent
    chart layers, not walked day-by-day like the backtest.
    """
    end_date = as_of or today_kst()
    start_date = end_date - timedelta(days=years * 365)

    gold_full = fetch_raw_series("gold", end_date, years=years)
    gold_display = gold_full[gold_full.index >= pd.Timestamp(start_date)]

    if key == "gold_silver_ratio":
        silver_full = fetch_raw_series("silver", end_date, years=years)
        combined = pd.concat([gold_full, silver_full], axis=1, keys=["gold", "silver"]).dropna()
        ratio_full = (combined["gold"] / combined["silver"]).rename("value")
        ratio_display = ratio_full[ratio_full.index >= pd.Timestamp(start_date)]
        return {"kind": "ratio", "indicator": ratio_display, "gold": gold_display, "smas": {}}

    raw_full = fetch_raw_series(_INDICATOR_SOURCE[key], end_date, years=years)
    smas_full = {window: metrics.compute_sma(raw_full, window) for window in config.MA_WINDOWS}
    indicator_display = raw_full[raw_full.index >= pd.Timestamp(start_date)]
    smas_display = {
        window: sma[sma.index >= pd.Timestamp(start_date)] for window, sma in smas_full.items()
    }
    return {"kind": "ma", "indicator": indicator_display, "gold": gold_display, "smas": smas_display}

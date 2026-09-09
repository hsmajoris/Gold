"""Assembles the full dashboard payload: fetches each indicator's series,
computes SMA breakout streaks, and formats display values."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from . import config
from . import data_sources as ds
from . import metrics

# Lookback window behind the as-of date, long enough for the 60-day SMA plus
# a comfortable margin for breakout-streak history (mirrors the old "2y" default).
LOOKBACK_DAYS = 730


def _fetch_series(key: str, start_date: date, as_of_date: date, yf_end_date: date) -> pd.Series:
    if key == "real_rate":
        return ds.fetch_fred_series("DFII10", end=as_of_date)
    if key == "dxy":
        return ds.fetch_yfinance_close(
            ["DX-Y.NYB", "^DXY", "DX=F"], start=start_date, end=yf_end_date
        )
    if key == "gold_silver_ratio":
        return ds.fetch_gold_silver_ratio(start=start_date, end=yf_end_date)
    if key == "wti":
        return ds.fetch_fred_series("DCOILWTICO", end=as_of_date)
    if key == "vix":
        return ds.fetch_yfinance_close("^VIX", start=start_date, end=yf_end_date)
    raise ValueError(f"unknown indicator key: {key}")


def _format_value(key: str, value: float) -> str:
    meta = config.INDICATOR_META[key]
    if value is None or pd.isna(value):
        return "-"
    text = f"{value:.{meta['decimals']}f}"
    if meta["unit"] == "%":
        return f"{text}%"
    if meta["unit"] == "$":
        return f"${text}"
    return text


def build_indicator(key: str, as_of: date | None = None) -> dict:
    as_of_date = as_of if as_of is not None else date.today()
    start_date = as_of_date - timedelta(days=LOOKBACK_DAYS)
    yf_end_date = as_of_date + timedelta(days=1)  # yfinance's `end` is exclusive

    series = _fetch_series(key, start_date, as_of_date, yf_end_date).sort_index()
    series = series[~series.index.duplicated(keep="last")]
    series = series[series.index <= pd.Timestamp(as_of_date)]
    if series.empty:
        raise RuntimeError(f"no data available for indicator {key!r} on or before {as_of_date}")

    latest_date = series.index[-1]
    latest_value = series.iloc[-1]

    sma_info = {}
    for window in config.MA_WINDOWS:
        ma = metrics.compute_sma(series, window)
        streak = metrics.breakout_streak(series, ma)
        sma_info[str(window)] = {
            "streak": streak,
            "breakout": streak > 0,
            "display": f"{streak}일" if streak > 0 else "0일",
        }

    return {
        "label": config.INDICATOR_META[key]["label"],
        "source": config.INDICATOR_META[key]["source"],
        "as_of": latest_date.strftime("%Y-%m-%d"),
        "prev_close": {
            "value": round(float(latest_value), 4),
            "display": _format_value(key, latest_value),
        },
        "sma": sma_info,
    }


def build(as_of: date | None = None) -> dict:
    """Build the dashboard payload.

    `as_of`: view the table as of this date (uses each indicator's last
    close on or before that date) instead of the latest available data.
    """
    indicators = {}
    as_of_dates = []
    for key in config.INDICATOR_ORDER:
        info = build_indicator(key, as_of=as_of)
        indicators[key] = info
        as_of_dates.append(info["as_of"])

    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "requested_as_of": as_of.strftime("%Y-%m-%d") if as_of else None,
        "as_of": max(as_of_dates) if as_of_dates else None,
        "indicator_order": config.INDICATOR_ORDER,
        "indicators": indicators,
        "static_rows": config.STATIC_ROWS,
        "row_order": config.ROW_ORDER,
        "ma_windows": config.MA_WINDOWS,
        "footnotes": config.FOOTNOTES,
    }

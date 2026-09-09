"""Assembles the full dashboard payload: fetches each indicator's series,
computes SMA breakout streaks, and formats display values."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from . import config
from . import data_sources as ds
from . import metrics


def _fetch_series(key: str) -> pd.Series:
    if key == "real_rate":
        return ds.fetch_fred_series("DFII10")
    if key == "dxy":
        return ds.fetch_yfinance_close(["DX-Y.NYB", "^DXY", "DX=F"])
    if key == "gold_silver_ratio":
        return ds.fetch_gold_silver_ratio()
    if key == "wti":
        return ds.fetch_fred_series("DCOILWTICO")
    if key == "vix":
        return ds.fetch_yfinance_close("^VIX")
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


def build_indicator(key: str) -> dict:
    series = _fetch_series(key).sort_index()
    series = series[~series.index.duplicated(keep="last")]
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


def build() -> dict:
    indicators = {}
    as_of_dates = []
    for key in config.INDICATOR_ORDER:
        info = build_indicator(key)
        indicators[key] = info
        as_of_dates.append(info["as_of"])

    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "as_of": max(as_of_dates) if as_of_dates else None,
        "indicator_order": config.INDICATOR_ORDER,
        "indicators": indicators,
        "static_rows": config.STATIC_ROWS,
        "row_order": config.ROW_ORDER,
        "ma_windows": config.MA_WINDOWS,
        "footnotes": config.FOOTNOTES,
    }

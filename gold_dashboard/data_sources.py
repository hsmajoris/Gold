"""Fetches raw daily price/rate series from free data sources (FRED CSV export
and Yahoo Finance via yfinance)."""

import io

import pandas as pd
import requests
import yfinance as yf

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"


def fetch_fred_series(series_id: str) -> pd.Series:
    """Fetch a FRED series as a date-indexed float Series (no API key required)."""
    url = FRED_CSV_URL.format(series_id=series_id)
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"]).set_index("date")
    return df["value"].rename(series_id)


def fetch_yfinance_close(tickers, period: str = "2y") -> pd.Series:
    """Fetch daily close prices for the first working ticker in `tickers`."""
    if isinstance(tickers, str):
        tickers = [tickers]
    last_error = None
    for ticker in tickers:
        try:
            hist = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
            close = hist["Close"].dropna()
            if close.index.tz is not None:
                close.index = close.index.tz_localize(None)
            if not close.empty:
                return close.rename(ticker)
        except Exception as exc:  # noqa: BLE001 - try the next ticker candidate
            last_error = exc
            continue
    raise RuntimeError(f"failed to fetch data for tickers {tickers}: {last_error}")


def fetch_gold_silver_ratio(period: str = "2y") -> pd.Series:
    """Daily gold/silver ratio computed from GC=F and SI=F closes."""
    gold = fetch_yfinance_close("GC=F", period=period)
    silver = fetch_yfinance_close("SI=F", period=period)
    df = pd.concat([gold, silver], axis=1, keys=["gold", "silver"]).dropna()
    return (df["gold"] / df["silver"]).rename("gold_silver_ratio")

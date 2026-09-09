"""Fetches raw daily price/rate series from free data sources (FRED CSV export
and Yahoo Finance via yfinance)."""

import io
import logging

import pandas as pd
import requests
import yfinance as yf
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
REQUEST_TIMEOUT = 60

logger = logging.getLogger(__name__)

# Max 3 attempts, waiting 5-10s between retries with exponential backoff.
_retry_network_call = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=5, min=5, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
    before_sleep=lambda state: logger.warning(
        "retrying %s after attempt %d failed: %r",
        state.fn.__name__ if state.fn else "call",
        state.attempt_number,
        state.outcome.exception() if state.outcome else None,
    ),
)


@_retry_network_call
def _download_fred_csv(series_id: str) -> str:
    url = FRED_CSV_URL.format(series_id=series_id)
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.text


def fetch_fred_series(series_id: str) -> pd.Series:
    """Fetch a FRED series as a date-indexed float Series (no API key required).

    Retries up to 3 times with a 5-10s exponential backoff on any failure
    (e.g. read timeouts); raises the last error if all attempts fail.
    """
    try:
        csv_text = _download_fred_csv(series_id)
    except Exception as exc:
        raise RuntimeError(
            f"failed to fetch FRED series {series_id!r} after 3 attempts: {exc!r}"
        ) from exc

    df = pd.read_csv(io.StringIO(csv_text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"]).set_index("date")
    return df["value"].rename(series_id)


@_retry_network_call
def _download_yfinance_close(ticker: str, period: str) -> pd.Series:
    hist = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    close = hist["Close"].dropna()
    if close.index.tz is not None:
        close.index = close.index.tz_localize(None)
    if close.empty:
        raise RuntimeError(f"no data returned for ticker {ticker!r}")
    return close.rename(ticker)


def fetch_yfinance_close(tickers, period: str = "2y") -> pd.Series:
    """Fetch daily close prices for the first working ticker in `tickers`.

    Each ticker is retried up to 3 times (5-10s exponential backoff) before
    falling through to the next candidate ticker.
    """
    if isinstance(tickers, str):
        tickers = [tickers]
    last_error = None
    for ticker in tickers:
        try:
            return _download_yfinance_close(ticker, period)
        except Exception as exc:  # try the next ticker candidate
            last_error = exc
            continue
    raise RuntimeError(
        f"failed to fetch data for tickers {tickers} after 3 attempts each: {last_error!r}"
    )


def fetch_gold_silver_ratio(period: str = "2y") -> pd.Series:
    """Daily gold/silver ratio computed from GC=F and SI=F closes."""
    gold = fetch_yfinance_close("GC=F", period=period)
    silver = fetch_yfinance_close("SI=F", period=period)
    df = pd.concat([gold, silver], axis=1, keys=["gold", "silver"]).dropna()
    return (df["gold"] / df["silver"]).rename("gold_silver_ratio")

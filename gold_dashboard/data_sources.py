"""Fetches raw daily price/rate series from free data sources (FRED CSV export
and Yahoo Finance via yfinance)."""

import logging
import os

import pandas as pd
import requests
import yfinance as yf
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
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
def _download_fred_observations(series_id: str, api_key: str, end=None) -> list:
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
    }
    if end is not None:
        params["observation_end"] = pd.Timestamp(end).strftime("%Y-%m-%d")
    resp = requests.get(FRED_OBSERVATIONS_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["observations"]


def fetch_fred_series(series_id: str, end=None) -> pd.Series:
    """Fetch a FRED series via the official FRED API as a date-indexed float Series.

    `end` (a date/str, optional) limits the series to observations on or
    before that date, so a historical as-of date can be requested. Requires
    the FRED_API_KEY environment variable. Retries up to 3 times with a
    5-10s exponential backoff on any failure (e.g. read timeouts); raises
    the last error if all attempts fail.
    """
    api_key = os.environ["FRED_API_KEY"]
    try:
        observations = _download_fred_observations(series_id, api_key, end)
    except Exception as exc:
        raise RuntimeError(
            f"failed to fetch FRED series {series_id!r} after 3 attempts: {exc!r}"
        ) from exc

    df = pd.DataFrame(observations)[["date", "value"]]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"]).set_index("date")
    return df["value"].rename(series_id)


@_retry_network_call
def _download_yfinance_close(ticker: str, start, end) -> pd.Series:
    hist = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=False)
    close = hist["Close"].dropna()
    if close.index.tz is not None:
        close.index = close.index.tz_localize(None)
    if close.empty:
        raise RuntimeError(f"no data returned for ticker {ticker!r}")
    return close.rename(ticker)


def fetch_yfinance_close(tickers, start=None, end=None) -> pd.Series:
    """Fetch daily close prices for the first working ticker in `tickers`.

    `start`/`end` (dates/strings, optional) bound the fetch window so a
    historical as-of date can be requested; `end` is exclusive, matching
    yfinance's convention. Each ticker is retried up to 3 times (5-10s
    exponential backoff) before falling through to the next candidate ticker.
    """
    if isinstance(tickers, str):
        tickers = [tickers]
    last_error = None
    for ticker in tickers:
        try:
            return _download_yfinance_close(ticker, start, end)
        except Exception as exc:  # try the next ticker candidate
            last_error = exc
            continue
    raise RuntimeError(
        f"failed to fetch data for tickers {tickers} after 3 attempts each: {last_error!r}"
    )


def fetch_gold_silver_ratio(start=None, end=None) -> pd.Series:
    """Daily gold/silver ratio computed from GC=F and SI=F closes."""
    gold = fetch_yfinance_close("GC=F", start=start, end=end)
    silver = fetch_yfinance_close("SI=F", start=start, end=end)
    df = pd.concat([gold, silver], axis=1, keys=["gold", "silver"]).dropna()
    return (df["gold"] / df["silver"]).rename("gold_silver_ratio")

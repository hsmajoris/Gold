"""Fetches raw daily price/rate series from free data sources (FRED CSV export,
Yahoo Finance via yfinance, and Naver's stock-data API for KRX's gold-spot
market)."""

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

from . import config

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
# Naver's stock-data API mirrors KRX's own gold-spot market data verbatim — the
# {ticker} path segment is literally KRX's own code (e.g. M04020000 for
# 04020000, "금 99.99_1kg"), and this is a read-only JSON GET with no
# authentication, unlike KRX's own data.krx.co.kr (which requires a logged-in
# session for this kind of query — see fetch_krx_gold_krw_per_gram).
NAVER_METALS_PRICES_URL = "https://api.stock.naver.com/marketindex/metals/{ticker}/prices"
# Naver rejects any pageSize above this.
NAVER_METALS_MAX_PAGE_SIZE = 60
# Safety cap on pagination so a misbehaving endpoint (e.g. one that never
# returns an empty page) can't loop forever. 2014-03-25 to today is currently
# (2026) under 3,100 trading days at pageSize=60 — this leaves generous room
# for years of future growth without ever being a realistic ceiling.
NAVER_METALS_MAX_PAGES = 4000
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


@_retry_network_call
def _download_krx_gold_page(page: int, page_size: int = NAVER_METALS_MAX_PAGE_SIZE) -> list:
    resp = requests.get(
        NAVER_METALS_PRICES_URL.format(ticker=config.KRX_GOLD_TICKER),
        params={"page": page, "pageSize": page_size},
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://stock.naver.com/marketindex/metals/{config.KRX_GOLD_TICKER}/price",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_krx_gold_krw_per_gram() -> pd.Series:
    """Fetch the full daily price history of KRX's gold-spot market (04020000,
    "금 99.99_1kg", quoted in KRW per gram) via Naver's stock-data API, which
    mirrors KRX's own official data under the same ticker (as M04020000) with
    no login required — unlike data.krx.co.kr's own query endpoints, which
    require an authenticated session for this kind of historical lookup.

    Pages backwards from today in NAVER_METALS_MAX_PAGE_SIZE-row chunks until
    an empty page is returned (i.e. all the way back to the market's
    KRX_GOLD_EARLIEST_DATE launch). Each page fetch is retried up to 3 times
    (the same 5-10s exponential backoff as every other network call in this
    module) before giving up.
    """
    rows: list[dict] = []
    page = 1
    while page <= NAVER_METALS_MAX_PAGES:
        try:
            batch = _download_krx_gold_page(page)
        except Exception as exc:
            raise RuntimeError(
                f"failed to fetch KRX gold price (page {page}) after 3 attempts: {exc!r}"
            ) from exc
        if not batch:
            break
        rows.extend(batch)
        page += 1

    if not rows:
        raise RuntimeError("no KRX gold price data returned")

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["localTradedAt"]).dt.tz_localize(None).dt.normalize()
    df["close"] = df["closePrice"].str.replace(",", "", regex=False).astype(float)
    df = df.drop_duplicates(subset="date").set_index("date").sort_index()
    return df["close"].rename("krx_gold_krw_per_g")

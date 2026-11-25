"""Fetches raw daily price/rate series from free data sources (FRED CSV export,
Yahoo Finance via yfinance, Naver's stock-data API for KRX's gold-spot
market, and a monthly-refreshed local cache of 국토교통부 반포자이 실거래가
— see gold_dashboard/update_banpo_data.py for how that cache is built)."""

import json
import logging
import os
from pathlib import Path

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
# blockchain.info's free, no-auth daily USD bitcoin price chart — the only
# practical source for bitcoin's actual 2010-era trading history (yfinance's
# own BTC-KRW/BTC-USD tickers start no earlier than 2014-09-17; see
# fetch_bitcoin_full_history_krw). sampled=false asks for full daily
# resolution instead of the API's default down-sampled (~1,600-point) series.
BLOCKCHAIN_MARKET_PRICE_URL = "https://api.blockchain.info/charts/market-price"
REQUEST_TIMEOUT = 60
# data_sources.py가 속한 패키지(gold_dashboard/)의 부모 = 레포 루트.
_REPO_ROOT = Path(__file__).resolve().parent.parent

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


@_retry_network_call
def fetch_yfinance_dividends(ticker: str) -> pd.Series:
    """Fetch a stock's full ex-dividend history (date -> per-share dividend
    amount, in the stock's own trading currency) via yfinance. Unlike
    `_download_yfinance_close`, this has no start/end window — yfinance's
    `Ticker.dividends` always returns the whole available history, and the
    backtest's own analysis window is applied by the caller (reindexing onto
    whatever price series' date index it's paired with) rather than here.
    Returns an empty Series (not an error) for a ticker with no dividend
    history at all, since "never paid a dividend" is a legitimate state, not
    a fetch failure."""
    dividends = yf.Ticker(ticker).dividends
    if dividends.index.tz is not None:
        dividends.index = dividends.index.tz_localize(None)
    dividends.index = dividends.index.normalize()
    return dividends.rename(ticker)


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


@_retry_network_call
def _download_blockchain_market_price_usd() -> list:
    resp = requests.get(
        BLOCKCHAIN_MARKET_PRICE_URL,
        params={"timespan": "all", "format": "json", "sampled": "false"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"blockchain.info market-price request failed: {payload}")
    return payload["values"]


def fetch_bitcoin_full_history_krw() -> pd.Series:
    """Daily BTC-KRW back to bitcoin's actual trading start (mid-2010) —
    far earlier than yfinance's own BTC-KRW history, which starts no earlier
    than 2014-09-17 (Yahoo's common cutover date for crypto tickers).

    From 2014-09-17 onward this is yfinance's own KRW-quoted close (the real
    local market price, premium included). Before that date — no KRW-quoted
    bitcoin market existed yet — it's a synthetic estimate: blockchain.info's
    daily USD market-price average (free, no auth) times FRED's DEXKOUS
    (원/달러 현물환율) spot rate for the same day, reusing the same
    FRED_API_KEY every other FRED series in this app already requires (no
    new secret needed). The two segments are simply concatenated at the
    cutover with no rescaling — they already land within ~1-2% of each other
    there, so no visible jump — since this is a synthetic USD×FX estimate,
    not an actual traded KRW price, for everything before 2014-09-17.

    Used by pages/2_자산비교.py so 비트코인 can be compared against 반포자이
    across its (almost) full trading history instead of being truncated to
    2014-09+."""
    try:
        raw_values = _download_blockchain_market_price_usd()
    except Exception as exc:
        raise RuntimeError(f"failed to fetch blockchain.info market price: {exc!r}") from exc

    usd = pd.Series(
        {pd.Timestamp(p["x"], unit="s").normalize(): float(p["y"]) for p in raw_values}
    ).sort_index()
    usd = usd[usd > 0]  # zero before bitcoin actually started trading (~2010-07)

    fx = fetch_fred_series(config.USD_KRW_FRED_SERIES_ID)
    fx = fx.reindex(usd.index, method="ffill").dropna()

    synthetic = (usd.reindex(fx.index) * fx).dropna()

    real = fetch_yfinance_close(config.BITCOIN_TICKER, start=pd.Timestamp("2014-01-01"))
    cutover = real.index.min()

    combined = pd.concat([synthetic[synthetic.index < cutover], real[real.index >= cutover]])
    return combined.sort_index().rename("btc_krw_full_history")


def fetch_banpo_xi_trend_series() -> pd.Series:
    """Read the monthly-refreshed cache of 반포자이 전용 84.943㎡ 실거래가
    추세 (config.BANPO_TREND_DATA_PATH, written by update_banpo_data.py from
    국토교통부 아파트매매 실거래가 공개 데이터— see that module's docstring).

    Raises a clear RuntimeError (not a bare FileNotFoundError) if the cache
    file is missing, since this is the one failure mode callers most need to
    surface distinctly (see the 유효성 검증 page's ⑧ 반포자이 기준, which
    must degrade to "unavailable" rather than crash the whole page when the
    cache hasn't been generated yet — e.g. a fresh clone before the first
    scheduled refresh has run).

    Returns a date-indexed float Series keyed by each cached row's
    `available_date` (already adjusted for reporting lag by
    update_banpo_data.py — no further look-ahead correction needed here),
    with no rebasing applied (see timeseries.fetch_gold_price_series, which
    rebases the analysis-window slice it actually uses).
    """
    path = _REPO_ROOT / config.BANPO_TREND_DATA_PATH
    if not path.exists():
        raise RuntimeError(
            f"{path}가 없습니다 — 반포자이 실거래가(⑧) 기준은 월 1회 GitHub Actions가 생성하는 "
            "캐시 파일이 있어야 조회할 수 있습니다(gold_dashboard/update_banpo_data.py 참고)."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    df = pd.DataFrame(payload["rows"])
    if df.empty:
        raise RuntimeError(f"{path}에 반포자이 실거래가 데이터가 없습니다.")
    df["date"] = pd.to_datetime(df["available_date"])
    df["value"] = pd.to_numeric(df["trend_value"], errors="coerce")
    df = df.dropna(subset=["value"]).drop_duplicates(subset="date").set_index("date").sort_index()
    return df["value"].rename("banpo_xi_trend")


def fetch_banpo_xi_raw_and_trend() -> pd.DataFrame:
    """Like fetch_banpo_xi_trend_series(), but also returns each individual
    transaction's own (pre-smoothing) raw deal amount alongside the trend —
    used by pages/2_자산비교.py to plot the raw transactions as scatter points
    next to the smoothed trend line. Columns: "raw", "trend", both float,
    indexed by the same look-ahead-safe `available_date` as the trend-only
    series."""
    path = _REPO_ROOT / config.BANPO_TREND_DATA_PATH
    if not path.exists():
        raise RuntimeError(
            f"{path}가 없습니다 — 반포자이 실거래가(⑧) 기준은 월 1회 GitHub Actions가 생성하는 "
            "캐시 파일이 있어야 조회할 수 있습니다(gold_dashboard/update_banpo_data.py 참고)."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    df = pd.DataFrame(payload["rows"])
    if df.empty:
        raise RuntimeError(f"{path}에 반포자이 실거래가 데이터가 없습니다.")
    df["date"] = pd.to_datetime(df["available_date"])
    df["raw"] = pd.to_numeric(df["raw_value"], errors="coerce")
    df["trend"] = pd.to_numeric(df["trend_value"], errors="coerce")
    df = df.dropna(subset=["raw", "trend"]).drop_duplicates(subset="date").set_index("date").sort_index()
    return df[["raw", "trend"]]

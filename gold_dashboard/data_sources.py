"""Fetches raw daily price/rate series from free data sources (FRED CSV export,
Yahoo Finance via yfinance, Naver's stock-data API for KRX's gold-spot
market, and 한국부동산원(REB) R-ONE Open API for the Seoul apartment price
index)."""

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
# 한국부동산원(REB) R-ONE Open API — 데이터 조회 상세기능 URL. 인증키는
# REB_API_KEY 환경변수에서 읽는다(절대 코드/커밋에 하드코딩하지 않음 —
# FRED_API_KEY와 동일한 방식). data.go.kr이 중계하는 것이 아니라 reb.or.kr이
# 직접 서비스하는 엔드포인트(활용가이드 문서 "부동산통계 Open API 활용가이드"
# 기준).
REB_DATA_URL = "https://www.reb.or.kr/r-one/openapi/SttsApiTblData.do"
REB_MAX_PAGE_SIZE = 1000
# 안전장치용 페이지 상한(주간 통계가 2012년부터 있어도 현재 기준 1,000건
# 미만이라 사실상 1페이지로 끝나지만, 향후 데이터가 쌓여도 무한 루프에
# 빠지지 않도록).
REB_MAX_PAGES = 50
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
def _download_reb_data_page(
    statbl_id: str, cls_id: int, dtacycle_cd: str, itm_id: int, api_key: str, page: int, page_size: int
) -> dict:
    params = {
        "KEY": api_key,
        "Type": "json",
        "pIndex": page,
        "pSize": page_size,
        "STATBL_ID": statbl_id,
        "DTACYCLE_CD": dtacycle_cd,
        "CLS_ID": cls_id,
        "ITM_ID": itm_id,
    }
    resp = requests.get(REB_DATA_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_reb_index_series(
    statbl_id: str, cls_id: int, dtacycle_cd: str, itm_id: int
) -> pd.Series:
    """Fetch a full REB R-ONE statistics-table time series (통계표ID+분류ID+
    항목ID, e.g. the Seoul apartment monthly sale-price index:
    config.REB_SEOUL_APT_STATBL_ID/CLS_ID/DTACYCLE_CD/ITM_ID) via the
    "통계 조회 조건 설정" (SttsApiTblData.do) detail function of 한국부동산원의
    부동산통계 Open API 서비스.

    Requires the REB_API_KEY environment variable (never hardcoded — same
    convention as FRED_API_KEY). Raises a clear RuntimeError (not a bare
    KeyError) if it's unset, since this is the one failure mode callers most
    need to surface distinctly (see the 유효성 검증 page's ⑧ 서울 아파트
    기준, which must degrade to "unavailable" rather than crash the whole
    page when this key is missing/invalid).

    Paginates in REB_MAX_PAGE_SIZE-row chunks (the API's own per-request cap)
    until `list_total_count` rows have been collected, up to REB_MAX_PAGES as
    a safety bound. A response of INFO-200 ("해당하는 데이터가 없습니다") is
    treated as "no rows" rather than an error — the API uses this for an
    empty-but-valid query, not a failure. Any other non-INFO-000 result code
    raises.

    Returns a date-indexed float Series with no look-ahead/publication-lag
    adjustment applied — that correction is a concern of how this series
    gets USED (see timeseries.fetch_gold_price_series and
    config.REB_MONTHLY_PUBLICATION_DAY), not of fetching it. The index date
    itself is derived differently depending on `dtacycle_cd`, since the
    API's own WRTTIME_DESC field format differs by cycle (confirmed
    2026-11-24 by direct inspection of both):
    - "WK" (weekly): WRTTIME_DESC is already a real, parseable calendar date
      (the Monday of that week) — used directly.
    - "MM" (monthly): WRTTIME_DESC is a Korean free-text label (e.g. "2026년
      10월"), NOT a parseable date — the index date is derived instead from
      WRTTIME_IDTFR_ID itself ("YYYYMM"), anchored to that month's 1st day.
    """
    api_key = os.environ.get("REB_API_KEY")
    if not api_key:
        raise RuntimeError(
            "REB_API_KEY 환경변수가 설정되지 않았습니다 — 서울 아파트 매매가격지수(⑧) 기준은 "
            "한국부동산원 R-ONE Open API 인증키가 있어야 조회할 수 있습니다."
        )

    rows: list[dict] = []
    total = None
    page = 1
    while page <= REB_MAX_PAGES:
        try:
            data = _download_reb_data_page(statbl_id, cls_id, dtacycle_cd, itm_id, api_key, page, REB_MAX_PAGE_SIZE)
        except Exception as exc:
            raise RuntimeError(
                f"REB API 호출 실패(통계표 {statbl_id}, {page}페이지, 3회 재시도 후): {exc!r}"
            ) from exc

        body = data.get("SttsApiTblData")
        if body is None:
            # 데이터 없음(INFO-200)인 경우 "SttsApiTblData" 래퍼 없이 바로
            # {"RESULT": {...}} 형태로 응답이 온다(요청/응답 메시지 예제와
            # 별개로 실제 호출에서 확인된 동작).
            result = data.get("RESULT", {})
            if result.get("CODE") == "INFO-200":
                break
            raise RuntimeError(f"REB API가 예상치 못한 응답을 반환했습니다(통계표 {statbl_id}): {data!r}")

        head = body[0]["head"]
        result = head[1]["RESULT"]
        if result["CODE"] not in ("INFO-000",):
            raise RuntimeError(f"REB API 오류(통계표 {statbl_id}): {result}")
        total = head[0]["list_total_count"]
        page_rows = body[1].get("row", []) if len(body) > 1 else []
        rows.extend(page_rows)
        if not page_rows or (total is not None and len(rows) >= total):
            break
        page += 1

    if not rows:
        raise RuntimeError(f"REB API에서 통계표 {statbl_id}(CLS_ID={cls_id})에 대한 데이터를 받지 못했습니다.")

    df = pd.DataFrame(rows)
    if dtacycle_cd == "MM":
        df["date"] = pd.to_datetime(df["WRTTIME_IDTFR_ID"], format="%Y%m")
    else:
        df["date"] = pd.to_datetime(df["WRTTIME_DESC"]).dt.normalize()
    df["value"] = pd.to_numeric(df["DTA_VAL"], errors="coerce")
    df = df.dropna(subset=["value"]).drop_duplicates(subset="date").set_index("date").sort_index()
    return df["value"].rename(statbl_id)

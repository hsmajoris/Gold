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
BUFFER_DAYS = 110  # extra calendar days of history fetched before the display/analysis
# start, so the longest calendar-day SMA (config.MA_WINDOWS' 90-day window)
# already has a full window on day 1 of that period, with a margin over the
# bare minimum of 90.

# Raw single-series sources, keyed by a short id (not all of these are dashboard
# indicator keys — "gold" is also fetched on its own for the backtest/chart
# comparisons below).
_FRED_SERIES_IDS = {"real_rate": "DFII10", "wti": "DCOILWTICO"}
_YFINANCE_TICKERS = {"dxy": DXY_TICKERS, "gold": "GC=F", "vix": "^VIX"}


def fetch_raw_series(
    key: str, as_of: date | None = None, years: int = YEARS, buffer_days: int = BUFFER_DAYS
) -> pd.Series:
    """Fetch one raw series (real_rate/dxy/gold/wti/vix) covering
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
    if basis in config.ALL_PRICE_PROXY_TICKERS:
        # 고려아연/미래에셋증권/비트코인 등은 실제 금이 아니라 "매수·매도 대상
        # 가격"만 대체하는 대리 자산이므로, 별도의 최저 상장일 클램프 없이
        # KRX 금현물과 같은 방식(fetch_start/end_date 그대로)으로 받아온다 —
        # 코스피 상장 역사도, 비트코인(BTC-KRW, 2014년부터 거래)의 역사도 이
        # 프로젝트가 요구하는 어떤 분석 기간(최대 MAX_BACKTEST_YEARS)보다
        # 훨씬 길어 클램핑이 실질적으로 발생할 일이 없다.
        end_date = as_of or today_kst()
        fetch_start = gold_fetch_start(end_date, years, buffer_days)
        yf_end = end_date + timedelta(days=1)  # yfinance's `end` is exclusive
        return ds.fetch_yfinance_close(config.ALL_PRICE_PROXY_TICKERS[basis], start=fetch_start, end=yf_end)
    if basis == config.GOLD_PRICE_BASIS_SEOUL_APT:
        # ⑧ 서울 주간 매매가격지수(한국부동산원 R-ONE API) — 실거래 불가한
        # 참조 지수. 두 가지 보정을 적용한다:
        # (1) 공표 지연 보정: API가 돌려주는 날짜(WRTTIME_DESC)는 조사기준일
        #     (항상 월요일)이지만 그 주차 지수가 실제로 "확정 공표"되는 날은
        #     그 주 목요일(config.REB_PUBLICATION_LAG_DAYS, 기본 3일) —
        #     이 보정 없이 월요일 날짜 그대로 쓰면 아직 공표되지도 않은
        #     그 주 지수를 월~수요일에 이미 알고 있었던 것처럼 계산하는
        #     look-ahead bias가 생긴다.
        # (2) 리베이스 보정: 이 함수가 반환하는 구간의 첫 값이 정확히 100이
        #     되도록 재정규화한다. REB가 주기적으로 자체 기준시점을 바꾸므로
        #     (예: 2026-07-06=100.0) 원본 값 자체의 절대 수준은 그때그때
        #     달라지는데, green_count/52주 신고가·신저가/수익률 계산은 전부
        #     같은 두 시점의 '비율'만 보기 때문에 상수배 재정규화는 그 계산
        #     결과에 수학적으로 아무 영향을 주지 않는다(분자·분모에 똑같이
        #     곱해지는 상수는 비율에서 소거됨) — 화면에 보여줄 때만 "분석
        #     시작 시점=100"이라는 직관적인 기준을 주기 위한 재정규화다.
        raw = ds.fetch_reb_index_series(
            config.REB_SEOUL_APT_STATBL_ID,
            config.REB_SEOUL_APT_CLS_ID,
            config.REB_SEOUL_APT_DTACYCLE_CD,
            config.REB_SEOUL_APT_ITM_ID,
        )
        raw = raw.set_axis(raw.index + timedelta(days=config.REB_PUBLICATION_LAG_DAYS))
        end_date = as_of or today_kst()
        fetch_start = gold_fetch_start(end_date, years, buffer_days)
        series = raw[(raw.index >= pd.Timestamp(fetch_start)) & (raw.index <= pd.Timestamp(end_date))]
        if series.empty:
            raise RuntimeError("선택한 분석 기간에 해당하는 서울 아파트 매매가격지수 데이터가 없습니다.")
        return (series / series.iloc[0] * 100.0).rename("seoul_apt_index")
    return fetch_raw_series("gold", as_of, years=years, buffer_days=buffer_days)


def fetch_backtest_frame(
    as_of: date | None = None,
    years: int = YEARS,
    buffer_days: int = BUFFER_DAYS,
    gold_price_basis: str = config.GOLD_PRICE_BASIS_DEFAULT,
) -> pd.DataFrame:
    """Fetch real_rate/dxy/gold as one date-aligned, forward-filled frame for
    the trading backtest, covering `years` of history (+ `buffer_days` ahead
    of it, to warm up rolling-window signals before the display start — see
    fetch_raw_series). Different markets close on different days (rates vs.
    commodities vs. KRX), so the series are joined on the union of their
    dates and gaps are forward-filled from the prior available value.

    `gold_price_basis` selects what the "gold" column (used for every MA/trend
    filter/trigger/P&L computation) actually is — see fetch_gold_price_series.

    Under the ② KRX basis and any ③~⑧ 대리 자산 basis (see
    config.ALL_PRICE_PROXY_TICKERS and ⑧'s own GOLD_PRICE_BASIS_SEOUL_APT
    check below), real_rate/dxy's date index is shifted forward one calendar
    day before the join below (see the comment at that shift) to correct a
    one-day look-ahead bias: FRED/Yahoo date real_rate/dxy by the US trading
    day (session closes ~16-17:00 ET ≈ 06:00-07:00 KST the NEXT calendar
    day), while KRX's gold-spot close and every KOSPI 대리 자산's close are
    all dated by the KST trading day itself, which ends hours EARLIER the
    same day — and ⑦ 비트코인(BTC-KRW)'s yfinance daily bar for calendar
    date D is timestamped at 00:00 UTC on date D, i.e. 09:00 KST on that SAME
    date D, so it's "known" even earlier in the day than a KRX close is;
    either way the fix is the same shift. ⑧ 서울 아파트 매매가격지수(이미
    fetch_gold_price_series에서 공표 지연만큼 그 자신의 날짜를 보정한 뒤)도
    같은 보정을 한 번 더 받는다 — 목요일 공표가 정확히 몇 시인지는 알 수
    없어(오전/오후 불확실) 실질금리/달러인덱스보다 먼저인지 나중인지 단정할
    수 없으므로, 둘 다 적용해 look-ahead 방향으로는 절대 새지 않도록 보수적
    으로 처리한다 — 어차피 주간 데이터라 하루 정도의 오차는 신호 타이밍에
    실질적 영향이 없다. Joining on identical date labels without the shift
    would pair a US date-D observation with gold's date-D KST/UTC close even
    though that US value isn't actually confirmed until the morning of KST
    date D+1 (verified 2026-09-15 against a third-party code review). Not
    applicable under the ① GC=F basis, since GC=F is itself dated on the US
    trading calendar, same as real_rate/dxy — no cross-timezone skew to
    correct there.
    """
    end_date = as_of or today_kst()
    real_rate = fetch_raw_series("real_rate", end_date, years=years, buffer_days=buffer_days)
    dxy = fetch_raw_series("dxy", end_date, years=years, buffer_days=buffer_days)
    gold = fetch_gold_price_series(end_date, years=years, buffer_days=buffer_days, basis=gold_price_basis)

    if (
        gold_price_basis == config.GOLD_PRICE_BASIS_KRX
        or gold_price_basis in config.ALL_PRICE_PROXY_TICKERS
        or gold_price_basis == config.GOLD_PRICE_BASIS_SEOUL_APT
    ):
        # A US date-D observation becomes usable starting KST date D+1; the
        # ffill()-after-outer-join below then naturally carries it forward
        # through any KST weekend/holiday gap to the next actual KST trading
        # day, with no separate trading-calendar lookup needed.
        real_rate = real_rate.set_axis(real_rate.index + timedelta(days=1))
        dxy = dxy.set_axis(dxy.index + timedelta(days=1))

    df = pd.concat(
        [
            real_rate.rename("real_rate"),
            dxy.rename("dxy"),
            gold.rename("gold"),
        ],
        axis=1,
        join="outer",
    ).sort_index()
    df = df[df.index <= pd.Timestamp(end_date)]
    df = df.ffill()
    df = df.dropna()  # drop the leading stretch before all five series have started
    return df


def fetch_dividend_yield_series(gold: pd.Series, ticker: str, dividend_tax_pct: float = 0.0) -> pd.Series:
    """Only meaningful when `gold` is actually a KOSPI 대리 자산's price series
    (③ 고려아연/④ 미래에셋증권 등 — see config.ALL_KOSPI_PROXY_TICKERS) and
    `ticker` is that same basis's own ticker — converts its raw per-share
    dividend history into a same-index daily yield series (0.0 on every day
    except an ex-dividend date, where it's that day's per-share dividend
    divided by `gold`'s own close that day, net of `dividend_tax_pct` if
    given). This is what backtest.run_backtest()/compute_hybrid_cagr() add to
    a held position's daily return — see their `dividend_yield_series`
    parameter.

    Ex-dividend dates not present in `gold.index` (a rare source-alignment
    edge case — yfinance's dividend calendar should otherwise match its own
    price calendar) are silently dropped rather than shifted to the nearest
    trading day, since misattributing a dividend to the wrong day would
    silently corrupt a specific day's return.
    """
    raw_dividends = ds.fetch_yfinance_dividends(ticker)
    per_share = raw_dividends.reindex(gold.index).fillna(0.0)
    net_per_share = per_share * (1.0 - dividend_tax_pct / 100.0)
    return (net_per_share / gold).rename("dividend_yield")


# Dashboard indicator key -> the raw series id that carries its value.
_INDICATOR_SOURCE = {"real_rate": "real_rate", "dxy": "dxy", "wti": "wti", "vix": "vix"}


def build_indicator_chart_data(key: str, as_of: date | None = None, years: int = YEARS) -> dict:
    """Data for one indicator's history chart: its own daily series (trimmed to
    the last `years`), config.MA_WINDOWS calendar-day SMAs of it, and gold's
    own daily series for comparison.

    Each returned series keeps its own native trading-calendar dates (no
    cross-series alignment/forward-fill) since they're drawn as independent
    chart layers, not walked day-by-day like the backtest.
    """
    end_date = as_of or today_kst()
    start_date = end_date - timedelta(days=years * 365)

    gold_full = fetch_raw_series("gold", end_date, years=years)
    gold_display = gold_full[gold_full.index >= pd.Timestamp(start_date)]

    raw_full = fetch_raw_series(_INDICATOR_SOURCE[key], end_date, years=years)
    smas_full = {window: metrics.compute_sma(raw_full, window) for window in config.MA_WINDOWS}
    indicator_display = raw_full[raw_full.index >= pd.Timestamp(start_date)]
    smas_display = {
        window: sma[sma.index >= pd.Timestamp(start_date)] for window, sma in smas_full.items()
    }
    return {"indicator": indicator_display, "gold": gold_display, "smas": smas_display}

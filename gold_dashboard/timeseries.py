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
from . import signals
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
        # ⑧ 반포자이 전용 84.943㎡ 실거래가 추세 — 국토교통부 실거래가 데이터를
        # 월 1회 GitHub Actions가 미리 받아둔 캐시(config.BANPO_TREND_DATA_PATH,
        # update_banpo_data.py가 생성)를 읽는다. 이미 캐시 생성 시점에
        # (1) look-ahead 방지용 신고기한 보정(config.BANPO_REPORTING_LAG_DAYS)과
        # (2) 거래 단위 평활화(config.BANPO_TREND_WINDOW_TRANSACTIONS건 이동평균)가
        # 다 적용된 값이라 여기서는 분석 기간으로 자르고 리베이스만 한다.
        # 리베이스(구간 첫 값=100)는 REB 지수 때와 같은 이유로 수행 — 비율
        # 기반 계산(green_count/52주 신고가·신저가/수익률)에는 영향이 없고,
        # 화면 표시용 기준점만 맞추는 것이다.
        raw = ds.fetch_banpo_xi_trend_series()
        end_date = as_of or today_kst()
        fetch_start = gold_fetch_start(end_date, years, buffer_days)
        series = raw[(raw.index >= pd.Timestamp(fetch_start)) & (raw.index <= pd.Timestamp(end_date))]
        if series.empty:
            raise RuntimeError("선택한 분석 기간에 해당하는 반포자이 실거래가 데이터가 없습니다.")
        return (series / series.iloc[0] * 100.0).rename("banpo_xi_trend_index")
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
    fetch_gold_price_series에서 월간 공표 지연만큼 그 자신의 날짜를 보정한
    뒤)도 같은 보정을 한 번 더 받는다 — 공표일 당일 정확히 몇 시인지는 알 수
    없어(오전/오후 불확실) 실질금리/달러인덱스보다 먼저인지 나중인지 단정할
    수 없으므로, 둘 다 적용해 look-ahead 방향으로는 절대 새지 않도록 보수적
    으로 처리한다 — 어차피 월간 데이터라 하루 정도의 오차는 신호 타이밍에
    실질적 영향이 없다. Joining on identical date labels without the shift
    would pair a US date-D observation with gold's date-D KST/UTC close even
    though that US value isn't actually confirmed until the morning of KST
    date D+1 (verified 2026-09-15 against a third-party code review). Not
    applicable under the ① GC=F basis, since GC=F is itself dated on the US
    trading calendar, same as real_rate/dxy — no cross-timezone skew to
    correct there.

    green_count is computed HERE, on a real_rate+dxy-only merge, deliberately
    BEFORE "gold" is ever joined in (see signals.compute_green_count's
    docstring for exactly why this separation is load-bearing and not just a
    style preference — in short, merging "gold" in first and computing the
    SMA afterward let whichever basis's own trading calendar happened to be
    selected silently change green_count on some days, since
    metrics.compute_sma()'s rolling window is a per-row mean, not a true
    calendar-weighted integral, and a basis with a denser calendar than
    real_rate/dxy's own weekday-only one — e.g. ⑦ 비트코인, which trades
    every single day including weekends — adds duplicate forward-filled
    real_rate/dxy rows into that window; bug found and fixed 2026-11-24).
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

    # real_rate+dxy merged on ONLY their own two series — "gold" is
    # deliberately excluded from this join (see this function's own
    # docstring and signals.compute_green_count's). Every basis's
    # green_count therefore comes out identical (up to the ±1-day shift
    # above, which is itself intentional) regardless of which gold-proxy's
    # own trading calendar gets unioned in next.
    rd = pd.concat([real_rate.rename("real_rate"), dxy.rename("dxy")], axis=1, join="outer").sort_index()
    rd = rd[rd.index <= pd.Timestamp(end_date)]
    rd = rd.ffill().dropna()
    rd["green_count"] = signals.compute_green_count(rd["real_rate"], rd["dxy"])

    df = pd.concat([rd, gold.rename("gold")], axis=1, join="outer").sort_index()
    df = df[df.index <= pd.Timestamp(end_date)]
    df = df.ffill()
    df = df.dropna()  # drop the leading stretch before all three series have started
    df["green_count"] = df["green_count"].astype(int)
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

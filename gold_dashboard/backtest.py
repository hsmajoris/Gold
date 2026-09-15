"""Signal-based backtest: real-rate/DXY MA breakout signals and a 52-week
new-high/new-low breakout, compared against a same-period Buy & Hold
benchmark.

Every moving-average window in this module (green_count's real_rate/dxy SMAs,
the noise filters' shared long-term SMA) is a calendar-day (역일) window, not
a trading-day count — see metrics.compute_sma. The day-to-day state machine in
run_backtest() itself (minimum holding period, the noise filters' D0+N check)
is likewise calendar-day based.

Buy (while flat): green_count >= BUY_GREEN_COUNT OR a fresh 52-week high
  (gold_new_52w_high)
Sell (while holding): green_count == SELL_GREEN_COUNT OR a fresh 52-week low
  (gold_new_52w_low)

All fills happen at the signal day's own close, immediately — there is no
delay/lag setting for any trigger (subject to the noise filters below
deferring execution pending confirmation).
"""

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from . import config
from . import metrics
from . import signals
from . import timeseries as ts
from .timeutil import today_kst

# Shared with the main dashboard's own MA columns/highlighting/chart shading
# (config.py) so both can never drift apart — see compute_signals below.
MA_WINDOWS = config.MA_WINDOWS

MIN_BACKTEST_YEARS = 1
MAX_BACKTEST_YEARS = 15
# Default analysis period a fresh session starts on; the 유효성 검증 page lets
# the user override this per-session (1-15 years) without affecting the main
# dashboard's own fixed-window charts (app.py's CHART_YEARS, unrelated to
# this). Deliberately independent of timeseries.YEARS. Deliberately NOT tied
# to MAX_BACKTEST_YEARS (raising the max shouldn't silently raise the default
# a fresh session starts on).
BACKTEST_YEARS = 10
# Extra calendar days of history fetched before the analysis start. The
# noise filters' LONG_TREND_WINDOW-day (calendar) SMA would only need 180
# calendar days minimum, but the 52-week new-high/new-low triggers' own
# FIFTY_TWO_WEEK_WINDOW_DAYS (365) window is the larger, binding requirement
# — deliberately independent of timeseries.BUFFER_DAYS, which only needs to
# cover MA_WINDOWS' own max (90 days) for the main dashboard's per-indicator
# chart fetches.
BUFFER_DAYS = 430

BUY_GREEN_COUNT = 6
SELL_GREEN_COUNT = 0
# No minimum holding period by default: a qualifying sell (green_count,
# 52-week-low, or the sell-noise filter's own D0+N resolution) can fire the
# day after entry. User-adjustable — raise this to simulate a longer-horizon
# strategy that ignores sell triggers for a while after buying.
DEFAULT_MIN_HOLDING_DAYS = 0

# gold's own long-term SMA, shared by the sell- and buy-signal noise filters
# below as the "in a clear uptrend/downtrend" gate they compare price
# against — one shared column (gold_sma_long), two independent consumers.
LONG_TREND_WINDOW = 180  # 6 calendar months

# 52-week new-high/new-low breakout: independent buy/sell triggers (OR'd in
# alongside green_count on the buy side; OR'd in alongside green_count on the
# sell side) — NOT the same thing as the old "금의 신고가 경신" trigger this
# module's docstring used to mention (that one used the gold's entire
# all-time high and fully replaced the buy-side trigger; these are a
# narrower 52-week/365-calendar-day window, fire alongside every other
# buy/sell trigger rather than instead of them, and have no cooldown/
# frequency limit of their own). Each fires only on the day gold's close is
# strictly above (new-high) or below (new-low) the highest/lowest close of
# the prior FIFTY_TWO_WEEK_WINDOW_DAYS calendar days (a fresh breakout, not
# "currently at/above/below the 52-week high/low" — see compute_signals).
FIFTY_TWO_WEEK_WINDOW_DAYS = 365
DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER = True
DEFAULT_USE_FIFTY_TWO_WEEK_LOW_TRIGGER = True

# Sell-signal noise filter: while gold is well above its LONG_TREND_WINDOW-day
# (calendar) SMA (a possible sign the sell signal is a blip in an ongoing
# uptrend rather than a genuine reversal), a qualifying sell signal is ignored and a
# 7-calendar-day "wait and see" period starts instead of executing it
# immediately. See run_backtest's docstring for the exact mechanism.
# Entry gate: how far above the LONG_TREND_WINDOW-day SMA gold's close must
# be (on the day a sell signal fires) for the filter to engage at all instead
# of selling immediately.
DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT = 5.0

# Buy-signal noise filter — the exact mirror image of the sell-signal filter
# above, direction flipped: while gold is well BELOW its LONG_TREND_WINDOW-day
# (calendar) SMA (a possible sign a buy signal is a bounce in an ongoing
# downtrend rather than a genuine reversal), a qualifying buy signal is
# ignored and a 7-calendar-day "wait and see" period starts instead of buying
# immediately. Shares the exact same √t-band derivation as the sell filter
# (see NOISE_BAND_* below) — only the confirmation direction (a further RISE
# instead of a further drop) differs. See run_backtest's docstring for the
# exact mechanism.
# Entry gate: how far below the LONG_TREND_WINDOW-day SMA gold's close must
# be (on the day a buy signal fires) for the filter to engage at all instead
# of buying immediately.
DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT = 5.0

# Exit/entry confirmation, method ① (default) — "매일 갱신 2시그마 밴드": every
# trading day of the observation window gets its OWN, wider confirmation
# threshold instead of one fixed checkpoint, because a real multi-week
# move should be allowed to clear a wider band the longer it's had to
# develop, while a single-day air-pocket has to clear a much tighter one.
# Shared, direction-agnostic math for BOTH the sell- and buy-signal noise
# filters (only the comparison direction differs — see noise_band_pct and
# run_backtest).
#
# Derivation (√t rule for a random walk's cumulative volatility):
#   daily_vol   = monthly_vol / sqrt(trading_days_per_month)
#   band(t)     = sigma_multiplier * daily_vol * sqrt(t)     (t = trading
#                 days elapsed since D0, 1..max)
# With monthly_vol = 4.9% (gold's ~30-year historical monthly volatility),
# trading_days_per_month = 21, sigma_multiplier = 2:
#   daily_vol = 4.9% / sqrt(21) = 1.0694%
#   band(1)  = 2 * 1.0694% * sqrt(1)  =  2.14%
#   band(5)  = 2 * 1.0694% * sqrt(5)  =  4.78%
#   band(10) = 2 * 1.0694% * sqrt(10) =  6.76%
#   band(14) = 2 * 1.0694% * sqrt(14) =  8.00%
#   band(21) = 2 * 1.0694% * sqrt(21) =  9.80%  (= 2 * monthly_vol exactly,
#              since sqrt(21) cancels the /sqrt(21) above)
# A sell/buy actually executes the first day the close is at or beyond
# `d0_close * (1 -/+ band(t))`. If no day in 1..NOISE_BAND_MAX_TRADING_DAYS
# reaches its own band, the episode is released (D0 is discarded, as if that
# first candidate never happened).
#
# The band is never even checked for the first NOISE_BAND_MIN_CHECK_DAY-1
# trading days: over a handful of days, plain random-walk noise has a
# non-trivial chance of producing a move that looks significant purely by
# chance, so days 1..(MIN_CHECK_DAY-1) hold unconditionally no matter how far
# price has moved, and only day MIN_CHECK_DAY through MAX_TRADING_DAYS are
# actually evaluated against their band.
DEFAULT_SELL_NOISE_USE_DAILY_BAND = True
DEFAULT_BUY_NOISE_USE_DAILY_BAND = True
NOISE_BAND_SIGMA_MULTIPLIER = 2.0
NOISE_BAND_MONTHLY_VOL_PCT = 4.9  # gold's ~30-year historical monthly volatility
NOISE_BAND_TRADING_DAYS_PER_MONTH = 21
NOISE_BAND_MIN_CHECK_TRADING_DAYS = 8  # first day the band is actually checked
NOISE_BAND_MAX_TRADING_DAYS = 21  # observation window cap (~3 weeks)

# Confirmation, method ② (legacy, used when the checkbox above is OFF) — a
# single fixed checkpoint at D0+7 calendar days: sell only if the close then
# is at least DEFAULT_SELL_NOISE_FILTER_DROP_PCT% below D0's close, or (buy
# side) buy only if it's at least DEFAULT_BUY_NOISE_FILTER_RISE_PCT% above
# D0's close.
SELL_NOISE_FILTER_WINDOW_DAYS = 7
BUY_NOISE_FILTER_WINDOW_DAYS = 7
DEFAULT_SELL_NOISE_FILTER_DROP_PCT = 5.0
DEFAULT_BUY_NOISE_FILTER_RISE_PCT = 5.0


def noise_band_pct(elapsed_trading_days: int) -> float:
    """The method-① confirmation threshold (as a fraction, e.g. 0.0214 for
    2.14%) for a D0+`elapsed_trading_days`-trading-day check — see the
    derivation above DEFAULT_SELL_NOISE_USE_DAILY_BAND. Shared by both the
    sell- and buy-signal noise filters (identical math; only the direction
    the caller compares against differs). `elapsed_trading_days` counts
    trading days (rows in the signal frame), not calendar days: the 30-year
    monthly volatility was itself de-annualized by
    sqrt(NOISE_BAND_TRADING_DAYS_PER_MONTH), so scaling by calendar days
    (which include non-trading weekends) would overstate the band."""
    daily_vol_pct = NOISE_BAND_MONTHLY_VOL_PCT / (NOISE_BAND_TRADING_DAYS_PER_MONTH ** 0.5)
    return NOISE_BAND_SIGMA_MULTIPLIER * daily_vol_pct * (elapsed_trading_days ** 0.5) / 100.0

# Default assumed annual yield for the "미보유기간 채권투자 가정" hybrid CAGR
# below. Adjustable per-run via simulate()'s bond_annual_yield argument.
DEFAULT_BOND_ANNUAL_YIELD = 0.10

# KRX gold-spot's real trading costs (미래에셋증권 fee schedule), split into the
# two genuinely different cost types the old single "annual holding fee"
# conflated:
# - Transaction fee: a ONE-TIME % of the traded notional, charged at every
#   buy and every sell fill — independent of how long the position is held.
#   Buy and sell each default to the same 0.165%, but are separate, both
#   user-adjustable (a round trip is buy + sell ≈ 0.33% combined).
# - Holding fee: a custody charge on whatever quantity is actually held,
#   proportional to elapsed TIME, not to trade count. The exact accrual
#   schedule (daily accrual billed monthly vs. a single charge on month-end
#   balance) isn't confirmed, so this assumes "매일 0.00022%씩(일률, 연율
#   아님) 누적, 월초 청구" (flat daily rate, compounded once per elapsed
#   calendar day) until the real schedule is confirmed — only
#   DEFAULT_DAILY_HOLDING_FEE_PCT itself would need to change if it turns out
#   to be a different cadence/coefficient.
# Both are meaningless for the international GC=F basis (a paper reference
# price, not a custodied/traded physical asset) — the UI is responsible for
# passing 0.0 there; simulate()/run_backtest() themselves don't know or care
# which basis is in use, only the fee rates they're given.
DEFAULT_BUY_FEE_PCT = 0.165
DEFAULT_SELL_FEE_PCT = 0.165
DEFAULT_DAILY_HOLDING_FEE_PCT = 0.00022


def _daily_fee_decay(elapsed_days: float, daily_fee_pct: float) -> float:
    """Multiplicative factor for a holding fee expressed as a flat DAILY rate
    (see DEFAULT_DAILY_HOLDING_FEE_PCT's "일할 계산" assumption above) —
    `daily_fee_pct` is compounded once per elapsed calendar day (never
    divided by 365; it's already a per-day rate). 1.0 (no-op) when
    `daily_fee_pct` is 0."""
    return (1.0 - daily_fee_pct / 100.0) ** elapsed_days


def fetch_raw_data(
    as_of: date | None = None,
    years: int = BACKTEST_YEARS,
    gold_price_basis: str = config.GOLD_PRICE_BASIS_DEFAULT,
) -> pd.DataFrame:
    """Fetch real_rate/dxy/gold as one date-aligned, forward-filled frame
    covering `years` + BUFFER_DAYS of history ending at `as_of` (default
    today, KST). Thin wrapper around the shared fetcher in timeseries.py.
    See fetch_backtest_frame for what `gold_price_basis` does."""
    return ts.fetch_backtest_frame(
        as_of, years=years, buffer_days=BUFFER_DAYS, gold_price_basis=gold_price_basis
    )


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Adds SMA-based gold-friendly flags for real_rate/dxy, green_count (0-6),
    gold's own long-term SMA, and the 52-week new-high/new-low flags:
    - gold_sma_long: gold's own LONG_TREND_WINDOW-day SMA — the gate the
      sell-/buy-signal noise filters compare price against (see
      DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT/DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT).
    - gold_new_52w_high / gold_new_52w_low: gold's close today is strictly
      above the highest / below the lowest close of the prior
      FIFTY_TWO_WEEK_WINDOW_DAYS calendar days (a fresh breakout day, not
      merely "currently at/above/below the 52-week high/low").
    """
    df = df.copy()
    gf_cols = []
    for col in ("real_rate", "dxy"):
        direction = signals.indicator_direction(col)
        for window in MA_WINDOWS:
            sma = metrics.compute_sma(df[col], window)
            gf_col = f"{col}_gf_{window}"
            # Delegates to the single shared comparison in signals.py — the same
            # function app.py's build_table highlighting and chart shading use —
            # so this can never silently drift from those.
            df[gf_col] = signals.gold_friendly_vs_ma(df[col], sma, direction)
            gf_cols.append(gf_col)
    df["green_count"] = df[gf_cols].sum(axis=1).astype(int)

    df["gold_sma_long"] = metrics.compute_sma(df["gold"], LONG_TREND_WINDOW)

    # shift(1) before the rolling max/min excludes today's own close from "the
    # prior N days' high/low" — otherwise every day sitting at its own new
    # high/low would trivially compare equal to (never above/below) that
    # high/low, and no breakout could ever be flagged.
    prior_52w = df["gold"].shift(1).rolling(f"{FIFTY_TWO_WEEK_WINDOW_DAYS}D", min_periods=1)
    df["gold_new_52w_high"] = (df["gold"] > prior_52w.max()).fillna(False)
    df["gold_new_52w_low"] = (df["gold"] < prior_52w.min()).fillna(False)
    return df


def trim_to_backtest_window(
    df: pd.DataFrame, as_of: date | None = None, years: int = BACKTEST_YEARS
) -> pd.DataFrame:
    """Trims to [end_date - years, end_date]. The upper bound matters even
    though every caller today fetches `df` already end-bounded at `as_of` (so
    there's normally no later data to trim off) — it's what makes a historical
    `as_of` (see 유효성 검증 page's "기준일" input) safe regardless of how `df`
    was built, instead of silently relying on the fetch step alone."""
    end_date = as_of or today_kst()
    start_date = end_date - timedelta(days=years * 365)
    trimmed = df[(df.index >= pd.Timestamp(start_date)) & (df.index <= pd.Timestamp(end_date))]
    if trimmed.empty:
        raise RuntimeError("no data available in the requested backtest window")
    return trimmed


def _buy_reason(
    gc: int,
    buy_green_count: int,
    include_new_high: bool = False,
) -> str:
    reasons = []
    if gc >= buy_green_count:
        reasons.append(f"green_count≥{buy_green_count}")
    if include_new_high:
        reasons.append("52주 신고가 갱신")
    return ", ".join(reasons)


def _sell_reason(gc: int, sell_green_count: int, include_new_low: bool = False) -> str:
    reasons = []
    if gc <= sell_green_count:
        reasons.append(f"green_count≤{sell_green_count}")
    if include_new_low:
        reasons.append("52주 신저가 갱신")
    return ", ".join(reasons)


def run_backtest(
    signals: pd.DataFrame,
    use_new_high_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER,
    use_new_low_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_LOW_TRIGGER,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
    use_sell_noise_filter: bool = True,
    sell_noise_filter_buffer_pct: float = DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT,
    use_daily_band_confirmation: bool = DEFAULT_SELL_NOISE_USE_DAILY_BAND,
    sell_noise_filter_drop_pct: float = DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
    use_buy_noise_filter: bool = True,
    buy_noise_filter_buffer_pct: float = DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT,
    use_buy_daily_band_confirmation: bool = DEFAULT_BUY_NOISE_USE_DAILY_BAND,
    buy_noise_filter_rise_pct: float = DEFAULT_BUY_NOISE_FILTER_RISE_PCT,
    buy_fee_pct: float = 0.0,
    sell_fee_pct: float = 0.0,
    daily_holding_fee_pct: float = 0.0,
) -> tuple[list[dict], pd.Series, pd.Series, pd.Series, list[dict], list[dict]]:
    """Walks the signal frame day by day applying the buy/sell rules.

    Every trigger fills immediately at its own signal day's close — there is
    no delay/lag setting anywhere in this state machine:
    - **green_count** (buy: `>= buy_green_count`, sell: `<= sell_green_count`).
    - The **52-week new-high trigger** (`use_new_high_trigger`, buy side, on
      by default — gold_new_52w_high, see compute_signals: today's close is a
      fresh breakout above the prior FIFTY_TWO_WEEK_WINDOW_DAYS calendar
      days' high) and the **52-week new-low trigger** (`use_new_low_trigger`,
      sell side, on by default — gold_new_52w_low, the mirror-image fresh
      breakdown below the prior FIFTY_TWO_WEEK_WINDOW_DAYS calendar days'
      low). Neither has a cooldown/frequency limit of its own.

    `min_holding_days`: once a position is opened, every sell trigger
    (green_count or 52-week new-low) is ignored entirely until at least this
    many calendar days have passed since entry — a minimum holding period for
    a longer-horizon strategy, not day-trading. Sell evaluation resumes
    normally once it has elapsed.

    Each trade records the reason(s) that triggered its order, as of the day
    the signal fired.

    Sell-signal noise filter (`use_sell_noise_filter`, on by default):
    applies to any sell signal
    (green_count or 52-week new-low) that fires on a day gold's close (D0) is
    more than `sell_noise_filter_buffer_pct`% above its LONG_TREND_WINDOW-day
    calendar SMA (`gold_sma_long`) — i.e. still in a clear uptrend, where an isolated sell
    signal is more likely noise than a genuine reversal. Below that buffer,
    every sell signal executes immediately exactly as if this filter didn't
    exist. Above it, the signal is ignored (position stays open) and an
    observation episode starts, recording D0's date and close price. New
    qualifying sell signals that fire while an episode is already open are
    recorded for reference only (see `occurrence_count` below) — they never
    change or restart D0. Confirmation happens one of two ways:

    - **Method ① — daily band, `use_daily_band_confirmation=True` (default)**:
      days t = 1..`NOISE_BAND_MIN_CHECK_TRADING_DAYS - 1` after D0 are
      never checked at all (too short a window for a drop to mean anything
      beyond random-walk noise) — the position just holds regardless of price.
      From t = `NOISE_BAND_MIN_CHECK_TRADING_DAYS` through
      `NOISE_BAND_MAX_TRADING_DAYS`, every trading day's close is compared
      against a confirmation band that widens with `sqrt(t)` — see
      `noise_band_pct()` for the exact derivation. The first day the
      close is at or below `d0_close * (1 - noise_band_pct(t))`, the
      position sells that day. If no day through the window's end reaches its
      own band, the episode is released unsold and D0 is discarded, as if
      that first candidate never happened — the next qualifying signal starts
      a brand new episode.
    - **Method ② — fixed D0+7, `use_daily_band_confirmation=False`**: a
      single checkpoint at D0+`SELL_NOISE_FILTER_WINDOW_DAYS` calendar days
      (the first trading day on or after that date). Sells there only if
      that close is at least `sell_noise_filter_drop_pct`% below D0's close;
      otherwise released unsold, same as method ①.

    Every completed episode (sold, released, or still unresolved when the
    backtest window ends) is recorded in the returned `sell_noise_log`,
    including `occurrence_count` (how many qualifying sell signals fired
    during the episode, D0 included) as a reference-only stat that never
    affects the sell decision itself.

    Buy-signal noise filter (`use_buy_noise_filter`, on by default,
    independent of everything above): the EXACT mirror of the sell-signal
    noise filter, direction flipped — applies to any buy signal (green_count
    or 52-week new-high) that fires on a day gold's close (D0) is
    more than `buy_noise_filter_buffer_pct`% BELOW its LONG_TREND_WINDOW-day
    calendar SMA (`gold_sma_long`) — i.e. still in a clear downtrend, where an
    isolated buy signal is more likely a dead-cat bounce than a genuine
    reversal. Below that buffer (i.e. not deep enough under the SMA), every
    buy signal executes immediately exactly as if this filter didn't exist.
    Beyond it, the signal is ignored (stays flat) and an observation episode
    starts, recording D0's date and close price. New qualifying buy signals
    that fire while an episode is already open are recorded for reference
    only — they never change or restart D0. Confirmation mirrors the sell
    side exactly, just checking for a RISE instead of a drop:

    - **Method ① — daily band, `use_buy_daily_band_confirmation=True`
      (default)**: same `NOISE_BAND_MIN_CHECK_TRADING_DAYS`/
      `NOISE_BAND_MAX_TRADING_DAYS` window and `noise_band_pct()` band widths
      as the sell side. The first day the close is at or above
      `d0_close * (1 + noise_band_pct(t))`, the position buys that day. If no
      day through the window's end reaches its own band, the episode is
      released unbought and D0 is discarded.
    - **Method ② — fixed D0+7, `use_buy_daily_band_confirmation=False`**: a
      single checkpoint at D0+`BUY_NOISE_FILTER_WINDOW_DAYS` calendar days.
      Buys there only if that close is at least `buy_noise_filter_rise_pct`%
      above D0's close; otherwise released unbought, same as method ①.

    Every completed buy-filter episode is recorded in the returned
    `buy_noise_log`, in the same shape as `sell_noise_log` (see its return-
    value description below, with "sold"/"drop" replaced by "bought"/"rise").

    `buy_fee_pct`/`sell_fee_pct`: a one-time % of the traded notional charged
    exactly at the moment of that fill — `buy_fee_pct` is applied to
    `equity_at_entry` the instant a position opens (so it also covers a
    position opened on this window's very first day), `sell_fee_pct` to
    `running_equity` the instant it closes. Both 0.0 (the default) reproduce
    pre-fee behavior exactly.

    `daily_holding_fee_pct`: a custody cost accrued only on days the position
    is actually held, compounded once per elapsed calendar day (see
    _daily_fee_decay and DEFAULT_DAILY_HOLDING_FEE_PCT's "일할 계산"
    assumption) — each day's return while holding is multiplied by
    `(1 - daily_holding_fee_pct / 100) ** elapsed_days`. 0.0 (the default)
    reproduces pre-fee behavior exactly.

    Returns (trades, equity_curve, bh_equity_curve, holding_curve,
    sell_noise_log, buy_noise_log). Both equity curves start at 1.0 on the
    first date. The strategy curve is flat (1.0x, i.e. 0% return) while in
    cash and compounds only through held periods (net of the holding fee, if
    any); the Buy & Hold curve is always invested from that same first date
    (also net of the holding fee). holding_curve is a same-index boolean
    Series (True = holding gold that day), used by compute_hybrid_cagr() to
    build the "미보유기간 채권투자 가정" hybrid equity curve. sell_noise_log is a
    list of dicts, one per completed/released noise-filter episode: {d0_date,
    d0_price, check_date (the resolving trading day, once known),
    check_price (None until resolved), elapsed_trading_days (None under
    method ②), band_pct (the method-① confirmation threshold that applied at
    resolution, None under method ②), occurrence_count (signals within the
    episode, D0 included), outcome ("sold_on_drop" | "released_no_drop" |
    "unresolved_at_window_end"), sold_date (None unless sold)}. buy_noise_log
    is the exact same shape, for the buy-side filter, with the corresponding
    fields named "bought_date" and outcomes "bought_on_rise" |
    "released_no_rise" | "unresolved_at_window_end".
    """
    dates = signals.index
    gold = signals["gold"]
    # Pre-extracted as plain numpy arrays, indexed by integer position `i`
    # alongside `dates` in the loop below (all share `signals.index`, so
    # `*_arr[i]` is always the exact same value `series.loc[dates[i]]` would
    # return) — repeatedly calling `.loc[dt]` inside a several-thousand-
    # iteration Python loop routes through pandas' full label-lookup machinery
    # (hashing, type dispatch, bounds/type checks) on every single access,
    # which dominates this function's cost when it runs (as it does here, up
    # to 4x per page load — once for the selected threshold, three more for
    # the participation cards' 6/0·5/1·4/2 comparison). Plain numpy scalar
    # indexing skips all of that while computing the identical value.
    gold_arr = gold.to_numpy()
    green_count_arr = signals["green_count"].to_numpy()
    gold_sma_long_arr = signals["gold_sma_long"].to_numpy()
    new_high_trigger_arr = signals["gold_new_52w_high"].to_numpy()
    new_low_trigger_arr = signals["gold_new_52w_low"].to_numpy()

    holding = False
    entry_date = None
    entry_price = None
    entry_reason = None
    equity_at_entry = None  # strategy equity value at the moment this position was opened
    running_equity = 1.0
    # {"d0_date", "d0_price", "occurrence_dates", "use_daily_band",
    # "elapsed_trading_days" (method ① running counter), "check_date"
    # (method ② fixed checkpoint)} while an episode is open, else None.
    sell_noise_state = None
    sell_noise_log: list[dict] = []
    # Exact mirror of sell_noise_state/sell_noise_log for the buy side (see
    # the buy-signal noise filter section of this function's docstring).
    buy_noise_state = None
    buy_noise_log: list[dict] = []
    trades: list[dict] = []
    equity_values = []
    holding_values = []

    for i, dt in enumerate(dates):
        gc = int(green_count_arr[i])
        price = float(gold_arr[i])

        if not holding:
            # No cooldown of its own — fires every time it's true and we're
            # not already holding.
            new_high_ready = use_new_high_trigger and bool(new_high_trigger_arr[i])

            raw_entry_reason_today = None
            if new_high_ready or gc >= buy_green_count:
                raw_entry_reason_today = _buy_reason(
                    gc,
                    buy_green_count,
                    include_new_high=new_high_ready,
                )

            # Buy-signal noise filter. Exact mirror of the sell-signal noise
            # filter below (see this function's docstring) — two mutually
            # exclusive cases:
            # - An episode is already open: today's own signal, if any, is
            #   recorded for reference only and never affects the outcome by
            #   itself — resolution instead follows whichever method was
            #   locked in at "use_daily_band" when the episode opened.
            # - No episode is open: a qualifying signal today (in the
            #   downtrend zone) starts a fresh one instead of buying
            #   immediately.
            entry_reason_today = None
            if buy_noise_state is not None:
                if raw_entry_reason_today is not None:
                    buy_noise_state["occurrence_dates"].append(dt)
                # suppressed unconditionally while an episode is open

                d0_date = buy_noise_state["d0_date"]
                d0_price = buy_noise_state["d0_price"]
                resolved = False
                bought = False
                band_pct = None
                elapsed_trading_days = None

                if buy_noise_state["use_daily_band"]:
                    # Method ①: every trading day since D0 gets its own,
                    # progressively wider confirmation band (see
                    # noise_band_pct's derivation above), but the first
                    # NOISE_BAND_MIN_CHECK_TRADING_DAYS-1 days are never even
                    # checked (too short a window for a rise to mean anything
                    # beyond random-walk noise).
                    elapsed_trading_days = buy_noise_state["elapsed_trading_days"] + 1
                    buy_noise_state["elapsed_trading_days"] = elapsed_trading_days
                    if elapsed_trading_days >= NOISE_BAND_MIN_CHECK_TRADING_DAYS:
                        band_pct = noise_band_pct(elapsed_trading_days)
                        price_rose = price >= d0_price * (1.0 + band_pct)
                    else:
                        band_pct = None
                        price_rose = False
                    if price_rose:
                        resolved = True
                        bought = True
                    elif elapsed_trading_days >= NOISE_BAND_MAX_TRADING_DAYS:
                        resolved = True
                        bought = False
                else:
                    # Method ② (legacy): a single fixed checkpoint at D0+7
                    # calendar days, buy only if today's close cleared the
                    # flat rise threshold.
                    if dt >= buy_noise_state["check_date"]:
                        resolved = True
                        bought = price >= d0_price * (1.0 + buy_noise_filter_rise_pct / 100.0)

                if resolved:
                    occurrence_dates = buy_noise_state["occurrence_dates"]
                    occurrence_count = len(occurrence_dates)
                    outcome = "bought_on_rise" if bought else "released_no_rise"
                    buy_noise_log.append(
                        {
                            "d0_date": d0_date,
                            "d0_price": d0_price,
                            "check_date": dt,
                            "check_price": price,
                            "elapsed_trading_days": elapsed_trading_days,
                            "band_pct": band_pct,
                            "occurrence_count": occurrence_count,
                            "outcome": outcome,
                            "bought_date": dt if bought else None,
                        }
                    )
                    if bought:
                        base_reason = _buy_reason(gc, buy_green_count) or "관찰모드 종료"
                        rise_actual_pct = (price / d0_price - 1.0) * 100.0
                        if band_pct is not None:
                            entry_reason_today = (
                                f"{base_reason} (매수노이즈필터: D0={d0_date.date()} 종가 {d0_price:g} 대비 "
                                f"{elapsed_trading_days}거래일차 종가 {price:g}, {rise_actual_pct:.1f}% 상승"
                                f"[{band_pct * 100:.2f}%↑ 밴드 도달] → 매수)"
                            )
                        else:
                            entry_reason_today = (
                                f"{base_reason} (매수노이즈필터: D0={d0_date.date()} 종가 {d0_price:g} 대비 "
                                f"D0+7일 종가 {price:g}, {rise_actual_pct:.1f}% 상승[{buy_noise_filter_rise_pct:g}%"
                                "↑ 조건 충족] → 매수)"
                            )
                    buy_noise_state = None
            elif raw_entry_reason_today is not None:
                sma_long_today = gold_sma_long_arr[i]
                in_downtrend_zone = (
                    use_buy_noise_filter
                    and not pd.isna(sma_long_today)
                    and price < sma_long_today * (1.0 - buy_noise_filter_buffer_pct / 100.0)
                )
                if in_downtrend_zone:
                    buy_noise_state = {
                        "d0_date": dt,
                        "d0_price": price,
                        "use_daily_band": use_buy_daily_band_confirmation,
                        "elapsed_trading_days": 0,
                        "check_date": dt + timedelta(days=BUY_NOISE_FILTER_WINDOW_DAYS),
                        "occurrence_dates": [dt],
                    }
                else:
                    entry_reason_today = raw_entry_reason_today

            if entry_reason_today is not None:
                holding = True
                entry_date = dt
                entry_price = price
                entry_reason = entry_reason_today
                # Buy-side transaction fee: an instant haircut on the equity
                # just committed, right as the position opens (including a
                # position opened on this window's very first day).
                equity_at_entry = running_equity * (1.0 - buy_fee_pct / 100.0)
                sell_noise_state = None  # defensive: a fresh position starts with no open episode
        else:
            exit_reason_today = None
            # Minimum holding period: no sell trigger is even evaluated until
            # this elapses. `.days` on a Timestamp difference is a fixed
            # calendar-day count (date2 - date1), unaffected by
            # weekends/holidays even though `dates` itself only contains
            # trading days.
            if (dt - entry_date).days >= min_holding_days:
                # No cooldown of its own (mirrors the new-high buy trigger) —
                # fires every time it's true while holding.
                new_low_ready = use_new_low_trigger and bool(new_low_trigger_arr[i])
                if new_low_ready or gc <= sell_green_count:
                    exit_reason_today = _sell_reason(gc, sell_green_count, include_new_low=new_low_ready)

            # Sell-signal noise filter. Two mutually exclusive cases:
            # - An episode is already open: today's own signal, if any, is
            #   recorded for reference only and never affects the outcome by
            #   itself — resolution instead follows whichever method was
            #   locked in at "use_daily_band" when the episode opened, so a
            #   single episode never switches methods mid-flight.
            # - No episode is open: a qualifying signal today (in the uptrend
            #   zone) starts a fresh one instead of executing immediately.
            if sell_noise_state is not None:
                if exit_reason_today is not None:
                    sell_noise_state["occurrence_dates"].append(dt)
                exit_reason_today = None  # suppressed unconditionally while an episode is open

                d0_date = sell_noise_state["d0_date"]
                d0_price = sell_noise_state["d0_price"]
                resolved = False
                sold = False
                band_pct = None
                elapsed_trading_days = None

                if sell_noise_state["use_daily_band"]:
                    # Method ①: every trading day since D0 gets its own,
                    # progressively wider confirmation band (see
                    # noise_band_pct's derivation above), but the first
                    # NOISE_BAND_MIN_CHECK_TRADING_DAYS-1 days are never
                    # even checked (too short a window for a drop to mean
                    # anything beyond random-walk noise).
                    elapsed_trading_days = sell_noise_state["elapsed_trading_days"] + 1
                    sell_noise_state["elapsed_trading_days"] = elapsed_trading_days
                    if elapsed_trading_days >= NOISE_BAND_MIN_CHECK_TRADING_DAYS:
                        band_pct = noise_band_pct(elapsed_trading_days)
                        price_dropped = price <= d0_price * (1.0 - band_pct)
                    else:
                        band_pct = None
                        price_dropped = False
                    if price_dropped:
                        resolved = True
                        sold = True
                    elif elapsed_trading_days >= NOISE_BAND_MAX_TRADING_DAYS:
                        resolved = True
                        sold = False
                else:
                    # Method ② (legacy): a single fixed checkpoint at D0+7
                    # calendar days, sell only if today's close cleared the
                    # flat drop threshold.
                    if dt >= sell_noise_state["check_date"]:
                        resolved = True
                        sold = price <= d0_price * (1.0 - sell_noise_filter_drop_pct / 100.0)

                if resolved:
                    occurrence_dates = sell_noise_state["occurrence_dates"]
                    occurrence_count = len(occurrence_dates)
                    outcome = "sold_on_drop" if sold else "released_no_drop"
                    sell_noise_log.append(
                        {
                            "d0_date": d0_date,
                            "d0_price": d0_price,
                            "check_date": dt,
                            "check_price": price,
                            "elapsed_trading_days": elapsed_trading_days,
                            "band_pct": band_pct,
                            "occurrence_count": occurrence_count,
                            "outcome": outcome,
                            "sold_date": dt if sold else None,
                        }
                    )
                    if sold:
                        base_reason = _sell_reason(gc, sell_green_count) or "관찰모드 종료"
                        drop_actual_pct = (price / d0_price - 1.0) * 100.0
                        if band_pct is not None:
                            exit_reason_today = (
                                f"{base_reason} (노이즈필터: D0={d0_date.date()} 종가 {d0_price:g} 대비 "
                                f"{elapsed_trading_days}거래일차 종가 {price:g}, {drop_actual_pct:.1f}% 하락"
                                f"[{band_pct * 100:.2f}%↓ 밴드 도달] → 매도)"
                            )
                        else:
                            exit_reason_today = (
                                f"{base_reason} (노이즈필터: D0={d0_date.date()} 종가 {d0_price:g} 대비 "
                                f"D0+7일 종가 {price:g}, {drop_actual_pct:.1f}% 하락[{sell_noise_filter_drop_pct:g}%"
                                "↓ 조건 충족] → 매도)"
                            )
                    sell_noise_state = None
            elif exit_reason_today is not None:
                sma_long_today = gold_sma_long_arr[i]
                in_uptrend_zone = (
                    use_sell_noise_filter
                    and not pd.isna(sma_long_today)
                    and price > sma_long_today * (1.0 + sell_noise_filter_buffer_pct / 100.0)
                )
                if in_uptrend_zone:
                    sell_noise_state = {
                        "d0_date": dt,
                        "d0_price": price,
                        "use_daily_band": use_daily_band_confirmation,
                        "elapsed_trading_days": 0,
                        "check_date": dt + timedelta(days=SELL_NOISE_FILTER_WINDOW_DAYS),
                        "occurrence_dates": [dt],
                    }
                    exit_reason_today = None

            if exit_reason_today is not None:
                exit_price = price
                fee_factor = _daily_fee_decay((dt - entry_date).days, daily_holding_fee_pct)
                # Sell-side transaction fee: an instant haircut on the
                # proceeds, right as the position closes.
                running_equity = (
                    equity_at_entry * (exit_price / entry_price) * fee_factor * (1.0 - sell_fee_pct / 100.0)
                )
                trades.append(
                    {
                        "entry_date": entry_date,
                        "entry_price": entry_price,
                        "entry_reason": entry_reason,
                        "exit_date": dt,
                        "exit_price": exit_price,
                        "exit_reason": exit_reason_today,
                        "hold_days": (dt - entry_date).days,
                        "period_return": exit_price / entry_price - 1.0,
                        "open": False,
                    }
                )
                holding = False
                entry_date = None
                entry_price = None
                entry_reason = None
                equity_at_entry = None

        if holding:
            fee_factor = _daily_fee_decay((dt - entry_date).days, daily_holding_fee_pct)
            equity_values.append(equity_at_entry * (price / entry_price) * fee_factor)
        else:
            equity_values.append(running_equity)
        holding_values.append(holding)

    if holding:
        last_dt = dates[-1]
        last_price = float(gold_arr[-1])
        trades.append(
            {
                "entry_date": entry_date,
                "entry_price": entry_price,
                "entry_reason": entry_reason,
                "exit_date": None,
                "exit_price": last_price,
                "exit_reason": None,
                "hold_days": (last_dt - entry_date).days,
                "period_return": last_price / entry_price - 1.0,
                "open": True,
            }
        )

    if sell_noise_state is not None:
        # The backtest window ended before the episode ever resolved (sold or
        # released). Recorded as its own outcome so sell_noise_log always
        # accounts for every episode that was opened.
        occurrence_dates = sell_noise_state["occurrence_dates"]
        sell_noise_log.append(
            {
                "d0_date": sell_noise_state["d0_date"],
                "d0_price": sell_noise_state["d0_price"],
                "check_date": None,
                "check_price": None,
                "elapsed_trading_days": (
                    sell_noise_state["elapsed_trading_days"] if sell_noise_state["use_daily_band"] else None
                ),
                "band_pct": None,
                "occurrence_count": len(occurrence_dates),
                "outcome": "unresolved_at_window_end",
                "sold_date": None,
            }
        )

    if buy_noise_state is not None:
        # Mirror of the sell_noise_state handling above: the backtest window
        # ended before the episode ever resolved (bought or released).
        occurrence_dates = buy_noise_state["occurrence_dates"]
        buy_noise_log.append(
            {
                "d0_date": buy_noise_state["d0_date"],
                "d0_price": buy_noise_state["d0_price"],
                "check_date": None,
                "check_price": None,
                "elapsed_trading_days": (
                    buy_noise_state["elapsed_trading_days"] if buy_noise_state["use_daily_band"] else None
                ),
                "band_pct": None,
                "occurrence_count": len(occurrence_dates),
                "outcome": "unresolved_at_window_end",
                "bought_date": None,
            }
        )

    equity_curve = pd.Series(equity_values, index=dates, name="strategy_equity")
    # Buy & Hold holds continuously from the first date, so the holding fee
    # compounds over each row's elapsed calendar days since the very start
    # (unlike the strategy curve, which resets its clock at each entry_date).
    # Transaction fees: one buy (day 1, a constant haircut applied to the
    # whole curve from the start) + — per the 2026-09-14 confirmation that
    # B&H is treated as "sold at the end of the analysis window" rather than
    # "still open" — one sell (applied only to the final day's value).
    elapsed_since_start = (dates - dates[0]).days.to_numpy()
    bh_holding_fee_decay = (1.0 - daily_holding_fee_pct / 100.0) ** elapsed_since_start
    bh_equity_curve = (gold / gold.iloc[0]) * bh_holding_fee_decay * (1.0 - buy_fee_pct / 100.0)
    bh_equity_curve = bh_equity_curve.rename("bh_equity")
    bh_equity_curve.iloc[-1] *= 1.0 - sell_fee_pct / 100.0
    holding_curve = pd.Series(holding_values, index=dates, name="holding")
    return trades, equity_curve, bh_equity_curve, holding_curve, sell_noise_log, buy_noise_log


def compute_metrics(trades: list[dict], equity_curve: pd.Series, bh_equity_curve: pd.Series) -> dict:
    closed_trades = [t for t in trades if not t["open"]]
    open_trade = next((t for t in trades if t["open"]), None)

    invested_days = sum(t["hold_days"] for t in trades)
    final_equity = float(equity_curve.iloc[-1])
    strategy_total_return = final_equity - 1.0
    strategy_cagr = (
        final_equity ** (365.25 / invested_days) - 1.0 if invested_days > 0 else None
    )

    total_days = (equity_curve.index[-1] - equity_curve.index[0]).days
    bh_final_equity = float(bh_equity_curve.iloc[-1])
    bh_total_return = bh_final_equity - 1.0
    bh_cagr = bh_final_equity ** (365.25 / total_days) - 1.0 if total_days > 0 else None

    running_max = equity_curve.cummax()
    drawdown = equity_curve / running_max - 1.0
    max_drawdown = float(drawdown.min())

    win_rate = (
        sum(1 for t in closed_trades if t["period_return"] > 0) / len(closed_trades)
        if closed_trades
        else None
    )

    return {
        "closed_trade_count": len(closed_trades),
        "win_rate": win_rate,
        "strategy_total_return": strategy_total_return,
        "bh_total_return": bh_total_return,
        "strategy_cagr": strategy_cagr,
        "bh_cagr": bh_cagr,
        "max_drawdown": max_drawdown,
        "invested_days": invested_days,
        "total_days": total_days,
        "has_open_position": open_trade is not None,
    }


def compute_hybrid_cagr(
    holding_curve: pd.Series,
    gold: pd.Series,
    bond_annual_yield: float,
    buy_fee_pct: float = 0.0,
    sell_fee_pct: float = 0.0,
    daily_holding_fee_pct: float = 0.0,
) -> dict:
    """The full-period CAGR variant that fills non-holding days with an
    assumed bond return instead of leaving them flat: holding days compound at
    gold's actual day-over-day return (net of daily_holding_fee_pct, if any),
    non-holding days compound at `bond_annual_yield` annualized over the
    elapsed calendar days since the previous row. This directly complements
    compute_metrics()'s "strategy_cagr" (which is invested-days-only) with a
    whole-period figure, so the two can be compared side by side.

    Each step from day i-1 to day i is classified by whether the position was
    already held going INTO that step (holding_curve.iloc[i-1]), not whether
    it ends the step held — matching run_backtest()'s own "buy/sell at that
    day's close" convention, where the entry day itself earns no gold return
    (bought at today's close, so today's price move isn't captured) and the
    exit day earns the full gold return (held all day, sold at today's
    close). Using the current day's flag instead would double-count the
    entry day's price move that the strategy never actually captured.

    `buy_fee_pct`/`sell_fee_pct` (one-time, on the traded notional) are
    applied to the step in which a position opens/closes — a step classified
    as "not holding" (bond-yield step) that ENDS in a fresh entry also pays
    the buy fee there, and a step classified as "holding" (gold-return step)
    that ENDS in a close also pays the sell fee there, mirroring exactly
    where run_backtest() charges each — plus a special case for a position
    that's already open on this window's very first day (impossible to
    express as a "step", since there's no step before day 0), where the buy
    fee is charged directly against the initial 1.0 starting equity instead.

    The returned "hybrid_equity_curve" (same index as holding_curve, starting
    at 1.0 on the first date, or at (1 - buy_fee_pct%) if day 0 is already
    holding) is the day-by-day equity behind hybrid_cagr/hybrid_total_return —
    built by the same loop, so a chart plotting it is guaranteed to agree
    with those two scalars exactly (no separate recomputation to drift out
    of sync).
    """
    dates = holding_curve.index
    # Pre-extracted to plain numpy arrays for the same reason as run_backtest's
    # loop — repeated `.iloc[]` calls on a pandas Series inside a several-
    # thousand-iteration Python loop carry real per-call overhead that plain
    # array indexing skips, with no change to the values read (both series
    # share `dates`, so `*_arr[i]` is always `*.iloc[i]`).
    holding_arr = holding_curve.to_numpy()
    gold_arr = gold.to_numpy()
    hybrid_equity = 1.0 - buy_fee_pct / 100.0 if bool(holding_arr[0]) else 1.0
    equity_values = [hybrid_equity]
    non_holding_days = 0
    total_days = 0
    for i in range(1, len(dates)):
        elapsed_days = (dates[i] - dates[i - 1]).days
        total_days += elapsed_days
        was_holding = bool(holding_arr[i - 1])
        is_holding = bool(holding_arr[i])
        if was_holding:
            factor = float(gold_arr[i] / gold_arr[i - 1]) * _daily_fee_decay(
                elapsed_days, daily_holding_fee_pct
            )
            if not is_holding:  # closes exactly on day i
                factor *= 1.0 - sell_fee_pct / 100.0
        else:
            factor = (1.0 + bond_annual_yield) ** (elapsed_days / 365.25)
            non_holding_days += elapsed_days
            if is_holding:  # opens exactly on day i
                factor *= 1.0 - buy_fee_pct / 100.0
        hybrid_equity *= factor
        equity_values.append(hybrid_equity)

    hybrid_total_return = hybrid_equity - 1.0
    hybrid_cagr = hybrid_equity ** (365.25 / total_days) - 1.0 if total_days > 0 else None
    non_holding_fraction = non_holding_days / total_days if total_days > 0 else None
    hybrid_equity_curve = pd.Series(equity_values, index=dates, name="hybrid_equity")

    return {
        "bond_annual_yield": bond_annual_yield,
        "hybrid_total_return": hybrid_total_return,
        "hybrid_cagr": hybrid_cagr,
        "non_holding_days": non_holding_days,
        "non_holding_fraction": non_holding_fraction,
        "hybrid_equity_curve": hybrid_equity_curve,
    }


def yearly_returns(equity_curve: pd.Series, bh_equity_curve: pd.Series) -> pd.DataFrame:
    """Calendar-year returns for both curves, both raw (realized over whatever
    span of that year falls inside the backtest window) and annualized to that
    same span so partial first/last years are comparable to full years.

    `equity_curve` is generic — pass `hybrid_equity_curve` (the default the
    caller in `simulate()` uses, so this matches the ④ 신호전략(기대수익률
    포함) summary metric: a year spent entirely out of the market shows the
    assumed `bond_annual_yield`, not 0%) or the plain `equity_curve` (③
    신호전략(보유기간), where a year spent entirely in cash naturally comes
    out to 0% since that curve doesn't move during cash periods) — whichever
    matches what the caller wants to display.
    """
    years = sorted(set(equity_curve.index.year))
    rows = []
    prev_strategy = 1.0
    prev_bh = 1.0
    prev_date = equity_curve.index[0]
    for year in years:
        year_dates = equity_curve.index[equity_curve.index.year == year]
        last_date = year_dates[-1]
        days_span = max((last_date - prev_date).days, 1)
        year_end_strategy = float(equity_curve.loc[last_date])
        year_end_bh = float(bh_equity_curve.loc[last_date])

        strategy_return = year_end_strategy / prev_strategy - 1.0
        bh_return = year_end_bh / prev_bh - 1.0
        rows.append(
            {
                "year": year,
                "days_span": days_span,
                "strategy_return": strategy_return,
                "bh_return": bh_return,
                "strategy_return_annualized": (1.0 + strategy_return) ** (365.25 / days_span)
                - 1.0,
                "bh_return_annualized": (1.0 + bh_return) ** (365.25 / days_span) - 1.0,
            }
        )
        prev_strategy = year_end_strategy
        prev_bh = year_end_bh
        prev_date = last_date
    return pd.DataFrame(rows)


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_max_window_signals(as_of_iso: str, gold_price_basis: str) -> pd.DataFrame:
    """fetch + compute_signals over the WIDEST window the "분석 기간(N년)"
    slider can ever request (MAX_BACKTEST_YEARS + BUFFER_DAYS), cached only on
    (as_of, gold_price_basis) — deliberately NOT on `years`. prepare_signals()
    below trims this down to whatever narrower `years` was actually asked
    for, entirely in memory.

    This is why changing just the "분석 기간" slider no longer triggers a
    fresh network fetch: every rolling-window computation in compute_signals
    (SMAs, the 52-week high/low) is strictly backward-looking (never uses a
    future row), so trimming a wider, already-computed result down to a
    narrower window produces byte-for-byte the same values as computing that
    narrower window directly — the extra leading history only ever adds
    *more* warmup context, never less, since BUFFER_DAYS(430) already covers
    the 52-week (365-day) window on top of any requested `years`. The
    expensive part this actually saves is the KRX gold-spot fetch
    (`data_sources.fetch_krx_gold_krw_per_gram`), which always re-pages
    through its *entire* history from Naver's API regardless of how narrow a
    window is requested — previously that full re-fetch fired on every single
    "years" change; now it fires once per (as_of, gold_price_basis) per hour.
    """
    as_of = date.fromisoformat(as_of_iso)
    raw = fetch_raw_data(as_of, years=MAX_BACKTEST_YEARS, gold_price_basis=gold_price_basis)
    return compute_signals(raw)


def prepare_signals(
    as_of: date | None = None,
    years: int = BACKTEST_YEARS,
    gold_price_basis: str = config.GOLD_PRICE_BASIS_DEFAULT,
) -> pd.DataFrame:
    """The network-bound half of the pipeline: fetch + compute signals + trim
    to the backtest window. Independent of the buy/sell delay settings, so
    callers can cache this and re-run `simulate()` cheaply when only the
    delay changes. The fetch+compute step itself is cached at the widest
    possible window (see _cached_max_window_signals) so that changing only
    `years` — the common case, e.g. the 유효성 검증 page's "분석 기간" slider —
    never re-hits the network; only a change in `as_of` or `gold_price_basis`
    does."""
    as_of_date = as_of if as_of is not None else today_kst()
    full_signals = _cached_max_window_signals(as_of_date.isoformat(), gold_price_basis)
    return trim_to_backtest_window(full_signals, as_of_date, years=years)


def simulate(
    signals: pd.DataFrame,
    use_new_high_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER,
    use_new_low_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_LOW_TRIGGER,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
    bond_annual_yield: float = DEFAULT_BOND_ANNUAL_YIELD,
    use_sell_noise_filter: bool = True,
    sell_noise_filter_buffer_pct: float = DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT,
    use_daily_band_confirmation: bool = DEFAULT_SELL_NOISE_USE_DAILY_BAND,
    sell_noise_filter_drop_pct: float = DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
    use_buy_noise_filter: bool = True,
    buy_noise_filter_buffer_pct: float = DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT,
    use_buy_daily_band_confirmation: bool = DEFAULT_BUY_NOISE_USE_DAILY_BAND,
    buy_noise_filter_rise_pct: float = DEFAULT_BUY_NOISE_FILTER_RISE_PCT,
    buy_fee_pct: float = 0.0,
    sell_fee_pct: float = 0.0,
    daily_holding_fee_pct: float = 0.0,
) -> dict:
    """The pure-computation half: run the trade state machine over already-
    prepared signals and derive trades/equity curves/metrics/yearly returns."""
    trades, equity_curve, bh_equity_curve, holding_curve, sell_noise_log, buy_noise_log = run_backtest(
        signals,
        use_new_high_trigger=use_new_high_trigger,
        use_new_low_trigger=use_new_low_trigger,
        buy_green_count=buy_green_count,
        sell_green_count=sell_green_count,
        min_holding_days=min_holding_days,
        use_sell_noise_filter=use_sell_noise_filter,
        sell_noise_filter_buffer_pct=sell_noise_filter_buffer_pct,
        use_daily_band_confirmation=use_daily_band_confirmation,
        sell_noise_filter_drop_pct=sell_noise_filter_drop_pct,
        use_buy_noise_filter=use_buy_noise_filter,
        buy_noise_filter_buffer_pct=buy_noise_filter_buffer_pct,
        use_buy_daily_band_confirmation=use_buy_daily_band_confirmation,
        buy_noise_filter_rise_pct=buy_noise_filter_rise_pct,
        buy_fee_pct=buy_fee_pct,
        sell_fee_pct=sell_fee_pct,
        daily_holding_fee_pct=daily_holding_fee_pct,
    )
    metrics_out = compute_metrics(trades, equity_curve, bh_equity_curve)
    hybrid = compute_hybrid_cagr(
        holding_curve, signals["gold"], bond_annual_yield, buy_fee_pct, sell_fee_pct, daily_holding_fee_pct
    )
    hybrid_equity_curve = hybrid.pop("hybrid_equity_curve")
    metrics_out.update(hybrid)
    # Matches the ④ 신호전략(기대수익률 포함) summary metric, not ③ — a year
    # spent entirely out of the market shows bond_annual_yield, not 0%. The
    # 유효성 검증 page's yearly bar chart can recompute this with the plain
    # `equity_curve` instead if it wants to display ③ (보유기간만) here.
    yearly = yearly_returns(hybrid_equity_curve, bh_equity_curve)
    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "bh_equity_curve": bh_equity_curve,
        "holding_curve": holding_curve,
        "hybrid_equity_curve": hybrid_equity_curve,
        "metrics": metrics_out,
        "yearly_returns": yearly,
        "sell_noise_log": sell_noise_log,
        "buy_noise_log": buy_noise_log,
    }


def run(
    as_of: date | None = None,
    years: int = BACKTEST_YEARS,
    use_new_high_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER,
    use_new_low_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_LOW_TRIGGER,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
    bond_annual_yield: float = DEFAULT_BOND_ANNUAL_YIELD,
    gold_price_basis: str = config.GOLD_PRICE_BASIS_DEFAULT,
    use_sell_noise_filter: bool = True,
    sell_noise_filter_buffer_pct: float = DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT,
    use_daily_band_confirmation: bool = DEFAULT_SELL_NOISE_USE_DAILY_BAND,
    sell_noise_filter_drop_pct: float = DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
    use_buy_noise_filter: bool = True,
    buy_noise_filter_buffer_pct: float = DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT,
    use_buy_daily_band_confirmation: bool = DEFAULT_BUY_NOISE_USE_DAILY_BAND,
    buy_noise_filter_rise_pct: float = DEFAULT_BUY_NOISE_FILTER_RISE_PCT,
    buy_fee_pct: float = 0.0,
    sell_fee_pct: float = 0.0,
    daily_holding_fee_pct: float = 0.0,
) -> dict:
    signals = prepare_signals(as_of, years=years, gold_price_basis=gold_price_basis)
    result = simulate(
        signals,
        use_new_high_trigger=use_new_high_trigger,
        use_new_low_trigger=use_new_low_trigger,
        buy_green_count=buy_green_count,
        sell_green_count=sell_green_count,
        min_holding_days=min_holding_days,
        bond_annual_yield=bond_annual_yield,
        use_sell_noise_filter=use_sell_noise_filter,
        sell_noise_filter_buffer_pct=sell_noise_filter_buffer_pct,
        use_daily_band_confirmation=use_daily_band_confirmation,
        sell_noise_filter_drop_pct=sell_noise_filter_drop_pct,
        use_buy_noise_filter=use_buy_noise_filter,
        buy_noise_filter_buffer_pct=buy_noise_filter_buffer_pct,
        use_buy_daily_band_confirmation=use_buy_daily_band_confirmation,
        buy_noise_filter_rise_pct=buy_noise_filter_rise_pct,
        buy_fee_pct=buy_fee_pct,
        sell_fee_pct=sell_fee_pct,
        daily_holding_fee_pct=daily_holding_fee_pct,
    )
    result["signals"] = signals
    return result

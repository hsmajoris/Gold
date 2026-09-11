"""Signal-based backtest: real-rate/DXY MA breakout signals plus a gold/silver-ratio
threshold, compared against a same-period Buy & Hold benchmark.

Every moving-average window in this module (green_count's real_rate/dxy SMAs,
the reentry trigger's long/short SMAs) is a calendar-day (역일) window, not a
trading-day count — see metrics.compute_sma. Only the day-to-day state machine
in run_backtest() itself (buy/sell delays, minimum holding period, reentry
frequency limit, the sell-noise filter's D0+N check) was already calendar-day
based from the start.

Buy (while flat): green_count >= BUY_GREEN_COUNT OR gold/silver ratio >= buy_ratio
  OR the reentry trigger — both of:
    ① long-term trend filter (necessary condition): gold close is at least
      long_trend_buffer_pct% above its LONG_TREND_WINDOW-day (calendar) SMA,
      AND that SMA itself is higher than it was LONG_TREND_SLOPE_LOOKBACK_DAYS
      calendar days ago (the SMA must itself be trending up, not just be
      under price).
    ② short-term re-breakout trigger: gold closes back above its
      SHORT_REENTRY_WINDOW-day (calendar) SMA today, having been at/below it
      yesterday.
  See compute_reentry_trigger(). Optionally rate-limited to at most once per
  REENTRY_FREQ_LIMIT_DAYS calendar days.
Sell (while holding): green_count == SELL_GREEN_COUNT OR gold/silver ratio <= sell_ratio

All fills happen at the signal day's own close. The green_count trigger can be
delayed by a configurable number of days; the ratio and reentry triggers are
always immediate — see run_backtest() for the full timing rules.
"""

from datetime import date, timedelta

import pandas as pd

from . import config
from . import metrics
from . import signals
from . import timeseries as ts
from .timeutil import today_kst

# Shared with the main dashboard's own MA columns/highlighting/chart shading
# (config.py) so both can never drift apart — see compute_signals below.
MA_WINDOWS = config.MA_WINDOWS

MIN_BACKTEST_YEARS = 3
MAX_BACKTEST_YEARS = 10
# Default analysis period a fresh session starts on; the 유효성 검증 page lets
# the user override this per-session (3-10 years) without affecting the main
# dashboard's own fixed-window charts (app.py's CHART_YEARS, unrelated to
# this). Deliberately independent of timeseries.YEARS.
BACKTEST_YEARS = MAX_BACKTEST_YEARS
# Extra calendar days of history fetched before the analysis start. Sized for
# the reentry trigger's LONG_TREND_WINDOW-day (calendar) SMA plus the
# LONG_TREND_SLOPE_LOOKBACK_DAYS the slope check additionally looks back
# beyond that (365 + 30 = 395 calendar days minimum) — deliberately
# independent of timeseries.BUFFER_DAYS, which only needs to cover
# MA_WINDOWS' own max (90 days) for the main dashboard's per-indicator chart
# fetches.
BUFFER_DAYS = 430

BUY_GREEN_COUNT = 6
# Shared with the main dashboard's chart shading/reference lines (config.py)
# so both can never drift apart.
BUY_RATIO = config.DEFAULT_GS_RATIO_BUY_THRESHOLD
SELL_GREEN_COUNT = 0
SELL_RATIO = config.DEFAULT_GS_RATIO_SELL_THRESHOLD
# No minimum holding period by default: a qualifying sell (green_count,
# ratio, or the sell-noise filter's own D0+N resolution) can fire the day
# after entry. User-adjustable — raise this to simulate a longer-horizon
# strategy that ignores sell triggers for a while after buying.
DEFAULT_MIN_HOLDING_DAYS = 0

# Reentry trigger (replaces the previous "fresh gold record high" trigger):
# necessary condition is a long-term uptrend filter on gold's LONG_TREND_WINDOW
# -day calendar SMA (see compute_reentry_trigger: a % buffer above it, and the
# SMA itself sloping up over LONG_TREND_SLOPE_LOOKBACK_DAYS calendar days);
# the trigger itself fires the day gold closes back above its
# SHORT_REENTRY_WINDOW-day calendar SMA, having been at/below it the previous
# day (a short-term re-breakout). Standardized to 세달(quarter, 90 calendar
# days) and 한달(month, 30 calendar days) respectively.
LONG_TREND_WINDOW = 365
SHORT_REENTRY_WINDOW = 30
# How far above its own 365-day SMA gold's close must be (as a %) for the
# long-term trend filter to hold. User-adjustable per run.
DEFAULT_LONG_TREND_BUFFER_PCT = 5.0
# How many calendar days back the 365-day SMA's slope is measured over
# (today's SMA must exceed the SMA as it stood this many calendar days ago —
# see metrics.value_n_days_ago, a calendar-day lookup rather than a
# trading-day row shift).
LONG_TREND_SLOPE_LOOKBACK_DAYS = 30
# Default cap on how often the reentry trigger alone (not other buy triggers)
# may fire — at most once per this many calendar days. User-togglable per run.
DEFAULT_REENTRY_FREQ_LIMIT_DAYS = 30

# 52-week new-high breakout: an independent buy trigger (OR'd in alongside
# green_count/ratio/reentry), separate from the 재진입 조건 above — NOT the
# same thing as the old "금의 신고가 경신" trigger this module's docstring
# mentions being replaced by the reentry logic (that one used the gold's
# entire all-time high and fully replaced the buy-side trigger; this one is
# a narrower 52-week/365-calendar-day window, fires alongside every other
# buy trigger rather than instead of them, and — unlike the reentry
# trigger — has no cooldown/frequency limit of its own). Fires only on the
# day gold's close is strictly above the highest close of the prior
# FIFTY_TWO_WEEK_HIGH_WINDOW_DAYS calendar days (a fresh breakout, not
# "currently at/above the 52-week high" — see compute_signals).
FIFTY_TWO_WEEK_HIGH_WINDOW_DAYS = 365
DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER = True

# Sell-signal noise filter: while gold is well above its 365-day (calendar)
# SMA (a possible sign the sell signal is a blip in an ongoing uptrend rather
# than a genuine reversal), a qualifying sell signal is ignored and a
# 7-calendar-day "wait and see" period starts instead of executing it
# immediately. See run_backtest's docstring for the exact mechanism.
# Entry gate: how far above the 365-day SMA gold's close must be (on the day
# a sell signal fires) for the filter to engage at all instead of selling
# immediately.
DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT = 5.0

# Exit confirmation, method ① (default) — "매일 갱신 2시그마 밴드": every
# trading day of the observation window gets its OWN, wider confirmation
# threshold instead of one fixed checkpoint, because a real multi-week
# decline should be allowed to clear a wider band the longer it's had to
# develop, while a single-day air-pocket has to clear a much tighter one.
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
# A sell actually executes the first day the close is at or below
# `d0_close * (1 - band(t))`. If no day in 1..SELL_NOISE_BAND_MAX_TRADING_DAYS
# reaches its own band, the episode is released unsold (D0 is discarded, as
# if that first candidate never happened).
#
# The band is never even checked for the first SELL_NOISE_BAND_MIN_CHECK_DAY-1
# trading days: over a handful of days, plain random-walk noise has a
# non-trivial chance of producing a drop that looks significant purely by
# chance, so days 1..(MIN_CHECK_DAY-1) hold unconditionally no matter how far
# price has moved, and only day MIN_CHECK_DAY through MAX_TRADING_DAYS are
# actually evaluated against their band.
DEFAULT_SELL_NOISE_USE_DAILY_BAND = True
SELL_NOISE_BAND_SIGMA_MULTIPLIER = 2.0
SELL_NOISE_BAND_MONTHLY_VOL_PCT = 4.9  # gold's ~30-year historical monthly volatility
SELL_NOISE_BAND_TRADING_DAYS_PER_MONTH = 21
SELL_NOISE_BAND_MIN_CHECK_TRADING_DAYS = 8  # first day the band is actually checked
SELL_NOISE_BAND_MAX_TRADING_DAYS = 21  # observation window cap (~3 weeks)

# Exit confirmation, method ② (legacy, used when the checkbox above is OFF)
# — a single fixed checkpoint at D0+7 calendar days, sell only if the close
# then is at least DEFAULT_SELL_NOISE_FILTER_DROP_PCT% below D0's close.
SELL_NOISE_FILTER_WINDOW_DAYS = 7
DEFAULT_SELL_NOISE_FILTER_DROP_PCT = 5.0


def sell_noise_band_pct(elapsed_trading_days: int) -> float:
    """The method-① confirmation threshold (as a fraction, e.g. 0.0214 for
    2.14%) for a D0+`elapsed_trading_days`-trading-day check — see the
    derivation above DEFAULT_SELL_NOISE_USE_DAILY_BAND. `elapsed_trading_days`
    counts trading days (rows in the signal frame), not calendar days: the
    30-year monthly volatility was itself de-annualized by
    sqrt(SELL_NOISE_BAND_TRADING_DAYS_PER_MONTH), so scaling by calendar days
    (which include non-trading weekends) would overstate the band."""
    daily_vol_pct = SELL_NOISE_BAND_MONTHLY_VOL_PCT / (SELL_NOISE_BAND_TRADING_DAYS_PER_MONTH ** 0.5)
    return SELL_NOISE_BAND_SIGMA_MULTIPLIER * daily_vol_pct * (elapsed_trading_days ** 0.5) / 100.0

# Default assumed annual yield for the "미보유기간 채권투자 가정" hybrid CAGR
# below. Adjustable per-run via simulate()'s bond_annual_yield argument.
DEFAULT_BOND_ANNUAL_YIELD = 0.10

# KRX gold-spot's real custody/holding fee — an annual %, deducted
# continuously (compounded over elapsed calendar days) from both the
# strategy's holding periods and the Buy & Hold curve, whenever gold is
# actually held. Meaningless for the international GC=F basis (a paper
# reference price, not a custodied physical asset) — the UI is responsible
# for passing 0.0 there and this default only when the KRX basis is active;
# simulate()/run_backtest() themselves don't know or care which basis is in
# use, only the fee rate they're given.
DEFAULT_KRX_HOLDING_FEE_ANNUAL_PCT = 0.15


def _fee_decay(elapsed_days: float, annual_fee_pct: float) -> float:
    """Multiplicative factor for a continuous annual holding fee compounded
    over `elapsed_days` calendar days. 1.0 (no-op) when annual_fee_pct is 0."""
    return (1.0 - annual_fee_pct / 100.0) ** (elapsed_days / 365.25)


def fetch_raw_data(
    as_of: date | None = None,
    years: int = BACKTEST_YEARS,
    gold_price_basis: str = config.GOLD_PRICE_BASIS_DEFAULT,
) -> pd.DataFrame:
    """Fetch real_rate/dxy/gold/gold_intl/silver as one date-aligned, forward-
    filled frame covering `years` + BUFFER_DAYS of history ending at `as_of`
    (default today, KST). Thin wrapper around the shared fetcher in
    timeseries.py. See fetch_backtest_frame for what `gold_price_basis` does."""
    return ts.fetch_backtest_frame(
        as_of, years=years, buffer_days=BUFFER_DAYS, gold_price_basis=gold_price_basis
    )


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Adds SMA-based gold-friendly flags for real_rate/dxy, green_count (0-6),
    the raw gold/silver ratio (no MA needed for the ratio itself), and the two
    raw ingredients the "재진입" buy condition needs (see compute_reentry_trigger
    for how they combine — kept separate here because that combination depends
    on a user-adjustable buffer %, while these rolling-window computations
    don't and would otherwise be needlessly redone on every parameter tweak):
    - gold_sma_long: gold's own LONG_TREND_WINDOW-day SMA (raw value, not yet
      compared to price — the long-term trend filter's buffer % and slope
      check are applied downstream in compute_reentry_trigger).
    - gold_short_ma_crossover_up: gold closes above its SHORT_REENTRY_WINDOW-day
      SMA today, having been at/below it the previous day (a fresh short-term
      re-breakout, not merely "currently above").
    - gold_new_52w_high: gold's close today is strictly above the highest
      close of the prior FIFTY_TWO_WEEK_HIGH_WINDOW_DAYS calendar days (a
      fresh 52-week-high breakout day, not merely "currently at/above the
      52-week high" — see the new-high trigger's own comment above its
      constants for how this differs from the reentry trigger).
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
    # Always computed from international USD/oz prices (gold_intl), never the
    # basis-dependent "gold" column: a KRX KRW/g close divided by an SI=F
    # USD/oz close would be a meaningless number, and this must match the main
    # dashboard's own gold/silver ratio row, which is likewise unaffected by
    # the gold_price_basis setting.
    df["gold_silver_ratio"] = df["gold_intl"] / df["silver"]

    short_sma = metrics.compute_sma(df["gold"], SHORT_REENTRY_WINDOW)
    above_short = (df["gold"] > short_sma).fillna(False)
    df["gold_sma_long"] = metrics.compute_sma(df["gold"], LONG_TREND_WINDOW)
    # shift(..., fill_value=False) keeps this bool dtype end to end. Plain
    # .shift(1) introduces a leading NaN, which silently upcasts the Series to
    # object dtype; .fillna(False) doesn't undo that, so the ~ below would be
    # bitwise NOT on Python ints (True/False as 1/0) instead of logical NOT —
    # ~True == -2 and ~False == -1, both truthy, making this condition always
    # true (i.e. "currently above the short-term SMA") instead of "freshly
    # crossed above it today". Confirmed against real KRX gold history (back
    # when SHORT_REENTRY_WINDOW was a 20-trading-day window, before it became
    # the current 30-calendar-day one): the buggy form marked every one of
    # 1,675 "still above" days as a crossover, against 168 genuine fresh
    # crossings with this fix.
    df["gold_short_ma_crossover_up"] = above_short & ~above_short.shift(1, fill_value=False)

    # shift(1) before the rolling max excludes today's own close from "the
    # prior N days' high" — otherwise every day sitting at its own new high
    # would trivially compare equal to (never above) that high, and no
    # breakout could ever be flagged.
    prior_52w_high = (
        df["gold"].shift(1).rolling(f"{FIFTY_TWO_WEEK_HIGH_WINDOW_DAYS}D", min_periods=1).max()
    )
    df["gold_new_52w_high"] = (df["gold"] > prior_52w_high).fillna(False)
    return df


def compute_reentry_trigger(
    df: pd.DataFrame, long_trend_buffer_pct: float = DEFAULT_LONG_TREND_BUFFER_PCT
) -> pd.Series:
    """The "재진입" buy trigger, built from compute_signals()'s gold_sma_long
    and gold_short_ma_crossover_up columns. Split out from compute_signals()
    because only this final comparison depends on long_trend_buffer_pct — the
    rolling SMAs themselves don't, so callers can cheaply re-evaluate this for
    a different buffer % without recomputing (or refetching) anything else.

    ① long-term trend filter (necessary condition, both must hold):
       - gold close is at least `long_trend_buffer_pct`% above its 365-day
         (calendar) SMA (not just barely above it).
       - that SMA is itself higher than it stood LONG_TREND_SLOPE_LOOKBACK_DAYS
         calendar days ago (the 365-day SMA must be sloping up, i.e. gold is
         in a genuine uptrend, not just a flat/declining SMA that price
         happens to sit above) — a calendar-day lookup via
         metrics.value_n_days_ago, not a trading-day row shift.
    ② short-term re-breakout: gold_short_ma_crossover_up (see compute_signals).

    Fires only on days both ① and ② hold.
    """
    long_sma = df["gold_sma_long"]
    above_buffer = (df["gold"] > long_sma * (1.0 + long_trend_buffer_pct / 100.0)).fillna(False)
    long_sma_prior = metrics.value_n_days_ago(long_sma, LONG_TREND_SLOPE_LOOKBACK_DAYS)
    long_sma_rising = (long_sma > long_sma_prior).fillna(False)
    gold_above_long_trend = above_buffer & long_sma_rising
    return gold_above_long_trend & df["gold_short_ma_crossover_up"]


def trim_to_backtest_window(
    df: pd.DataFrame, as_of: date | None = None, years: int = BACKTEST_YEARS
) -> pd.DataFrame:
    end_date = as_of or today_kst()
    start_date = end_date - timedelta(days=years * 365)
    trimmed = df[df.index >= pd.Timestamp(start_date)]
    if trimmed.empty:
        raise RuntimeError("no data available in the requested backtest window")
    return trimmed


def _buy_reason(
    gc: int,
    r: float,
    buy_ratio: float,
    buy_green_count: int,
    include_reentry: bool = False,
    long_trend_buffer_pct: float = DEFAULT_LONG_TREND_BUFFER_PCT,
    include_new_high: bool = False,
) -> str:
    reasons = []
    if gc >= buy_green_count:
        reasons.append(f"green_count≥{buy_green_count}")
    if r >= buy_ratio:
        reasons.append(f"금/은비율≥{buy_ratio:g}")
    if include_reentry:
        reasons.append(f"재진입(365일선+{long_trend_buffer_pct:g}%·우상향, 30일선 상향돌파)")
    if include_new_high:
        reasons.append("52주 신고가 갱신")
    return ", ".join(reasons)


def _sell_reason(gc: int, r: float, sell_ratio: float, sell_green_count: int) -> str:
    reasons = []
    if gc <= sell_green_count:
        reasons.append(f"green_count≤{sell_green_count}")
    if r <= sell_ratio:
        reasons.append(f"금/은비율≤{sell_ratio:g}")
    return ", ".join(reasons)


def run_backtest(
    signals: pd.DataFrame,
    entry_delay_days: int = 0,
    exit_delay_days: int = 0,
    use_reentry_trigger: bool = True,
    use_reentry_freq_limit: bool = True,
    reentry_freq_limit_days: int = DEFAULT_REENTRY_FREQ_LIMIT_DAYS,
    long_trend_buffer_pct: float = DEFAULT_LONG_TREND_BUFFER_PCT,
    use_new_high_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER,
    buy_ratio: float = BUY_RATIO,
    sell_ratio: float = SELL_RATIO,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
    use_sell_noise_filter: bool = True,
    sell_noise_filter_buffer_pct: float = DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT,
    use_daily_band_confirmation: bool = DEFAULT_SELL_NOISE_USE_DAILY_BAND,
    sell_noise_filter_drop_pct: float = DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
    gold_holding_fee_annual_pct: float = 0.0,
) -> tuple[list[dict], pd.Series, pd.Series, pd.Series, list[dict]]:
    """Walks the signal frame day by day applying the buy/sell rules.

    Two kinds of triggers, with different timing:
    - **green_count** (buy: `>= buy_green_count`, sell: `<= sell_green_count`) is
      *delayed*: `entry_delay_days`/`exit_delay_days` let it be simulated with a
      lag instead of an immediate fill, e.g. "buy 1 month after 5 signals fire"
      (entry_delay_days=30). Once the condition is met, the order is scheduled
      for (signal_date + delay) and fills at the close of the first trading day
      on or after that date — with no reconfirmation of the condition in
      between (a plain timer). While an order is pending, new green_count
      signals on the same side are ignored (only one pending order per side).
    - **gold/silver ratio** (buy: `>= buy_ratio`, sell: `<= sell_ratio`), the
      **reentry trigger** (see compute_reentry_trigger: gold sufficiently above
      a rising long-term SMA AND a fresh short-term SMA re-breakout today),
      and the **52-week new-high trigger** (`use_new_high_trigger`, on by
      default — gold_new_52w_high, see compute_signals: today's close is a
      fresh breakout above the prior FIFTY_TWO_WEEK_HIGH_WINDOW_DAYS calendar
      days' high) are all *immediate*: they always fill the same day, ignoring
      the delay settings, and preempt any green_count order still pending.
      Unlike the reentry trigger, the new-high trigger has no cooldown/
      frequency limit of its own.

    `use_reentry_trigger`: master switch for the reentry trigger, mainly meant
    for A/B comparisons (e.g. the 유효성 검증 page's "재진입 로직 적용 전/후"
    check) rather than everyday use — turning it off reproduces the strategy's
    behavior from before this trigger existed (green_count/ratio only).

    Reentry-trigger frequency limit: if `use_reentry_freq_limit` is on (the
    default), the reentry trigger alone is only allowed to actually fire once
    every `reentry_freq_limit_days` calendar days — if it's been less than
    that since the last day it fired, it's suppressed even if the underlying
    condition is met (other buy triggers are unaffected and evaluated
    normally). Turning this off lets the reentry trigger fire every time its
    condition is met, with no cooldown. The cooldown clock is a simple rolling
    "last time this specific trigger fired" timestamp — independent of
    holding state, so it persists across intervening sells.

    `min_holding_days`: once a position is opened, every sell trigger (both the
    immediate ratio sell and the delayed green_count sell) is ignored entirely
    until at least this many calendar days have passed since entry — a minimum
    holding period for a longer-horizon strategy, not day-trading. No pending
    sell order can even be scheduled during this window; sell evaluation
    resumes normally (delay settings included) once it has elapsed.

    Each trade records the reason(s) that triggered its order, as of the day
    the signal fired — for a delayed green_count order this is the day it was
    scheduled, not necessarily still true by execution day.

    Sell-signal noise filter (`use_sell_noise_filter`, on by default,
    independent of the buy-side reentry filter): applies only to a sell
    signal (whichever kind — immediate ratio or a delayed green_count order
    reaching its execution day) that fires on a day gold's close (D0) is more
    than `sell_noise_filter_buffer_pct`% above its 365-day calendar SMA
    (`gold_sma_long`) — i.e. still in a clear uptrend, where an isolated sell
    signal is more likely noise than a genuine reversal. Below that buffer,
    every sell signal executes immediately exactly as if this filter didn't
    exist. Above it, the signal is ignored (position stays open) and an
    observation episode starts, recording D0's date and close price. New
    qualifying sell signals that fire while an episode is already open are
    recorded for reference only (see `occurrence_count` below) — they never
    change or restart D0. Confirmation happens one of two ways:

    - **Method ① — daily band, `use_daily_band_confirmation=True` (default)**:
      days t = 1..`SELL_NOISE_BAND_MIN_CHECK_TRADING_DAYS - 1` after D0 are
      never checked at all (too short a window for a drop to mean anything
      beyond random-walk noise) — the position just holds regardless of price.
      From t = `SELL_NOISE_BAND_MIN_CHECK_TRADING_DAYS` through
      `SELL_NOISE_BAND_MAX_TRADING_DAYS`, every trading day's close is compared
      against a confirmation band that widens with `sqrt(t)` — see
      `sell_noise_band_pct()` for the exact derivation. The first day the
      close is at or below `d0_close * (1 - sell_noise_band_pct(t))`, the
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

    `gold_holding_fee_annual_pct`: a continuous annual cost (e.g. KRX gold's
    real custody fee) deducted from both the strategy's equity while holding
    and the Buy & Hold curve throughout, compounded over elapsed calendar
    days: each day's return is multiplied by
    `(1 - gold_holding_fee_annual_pct / 100) ** (elapsed_days / 365.25)`.
    0.0 (the default) reproduces pre-fee behavior exactly.

    Returns (trades, equity_curve, bh_equity_curve, holding_curve,
    sell_noise_log). Both equity curves start at 1.0 on the first date. The
    strategy curve is flat (1.0x, i.e. 0% return) while in cash and compounds
    only through held periods (net of the holding fee, if any); the Buy &
    Hold curve is always invested from that same first date (also net of the
    holding fee). holding_curve is a same-index boolean Series (True =
    holding gold that day), used by compute_hybrid_cagr() to build the
    "미보유기간 채권투자 가정" hybrid equity curve. sell_noise_log is a list of
    dicts, one per completed/released noise-filter episode: {d0_date,
    d0_price, check_date (the resolving trading day, once known),
    check_price (None until resolved), elapsed_trading_days (None under
    method ②), band_pct (the method-① confirmation threshold that applied at
    resolution, None under method ②), occurrence_count (signals within the
    episode, D0 included), outcome ("sold_on_drop" | "released_no_drop" |
    "unresolved_at_window_end"), sold_date (None unless sold)}.
    """
    dates = signals.index
    gold = signals["gold"]
    green_count = signals["green_count"]
    ratio = signals["gold_silver_ratio"]
    gold_sma_long = signals["gold_sma_long"]
    reentry_trigger = compute_reentry_trigger(signals, long_trend_buffer_pct)
    new_high_trigger = signals["gold_new_52w_high"]

    holding = False
    entry_date = None
    entry_price = None
    entry_reason = None
    equity_at_entry = None  # strategy equity value at the moment this position was opened
    running_equity = 1.0
    pending_buy_date = None
    pending_buy_reason = None
    pending_sell_date = None
    pending_sell_reason = None
    last_reentry_fire_date = None  # rolling cooldown clock for the reentry trigger only
    # {"d0_date", "d0_price", "occurrence_dates", "use_daily_band",
    # "elapsed_trading_days" (method ① running counter), "check_date"
    # (method ② fixed checkpoint)} while an episode is open, else None.
    sell_noise_state = None
    sell_noise_log: list[dict] = []
    trades: list[dict] = []
    equity_values = []
    holding_values = []

    for dt in dates:
        gc = int(green_count.loc[dt])
        r = float(ratio.loc[dt])
        price = float(gold.loc[dt])
        raw_reentry_trigger = use_reentry_trigger and bool(reentry_trigger.loc[dt])

        if not holding:
            # The reentry trigger fires today only if its underlying condition
            # holds AND (the frequency limit is off, or no prior fire, or the
            # cooldown has elapsed since the last time it fired).
            reentry_ready = raw_reentry_trigger and (
                not use_reentry_freq_limit
                or last_reentry_fire_date is None
                or (dt - last_reentry_fire_date).days >= reentry_freq_limit_days
            )
            # No cooldown of its own (unlike the reentry trigger above) —
            # fires every time it's true and we're not already holding.
            new_high_ready = use_new_high_trigger and bool(new_high_trigger.loc[dt])

            entry_reason_today = None
            if reentry_ready or new_high_ready or r >= buy_ratio:
                # Ratio/reentry/new-high buys are immediate: no delay, and
                # this preempts any still-pending green_count order.
                entry_reason_today = _buy_reason(
                    gc,
                    r,
                    buy_ratio,
                    buy_green_count,
                    include_reentry=reentry_ready,
                    long_trend_buffer_pct=long_trend_buffer_pct,
                    include_new_high=new_high_ready,
                )
                pending_buy_date = None
                pending_buy_reason = None
                if reentry_ready:
                    last_reentry_fire_date = dt
            else:
                if pending_buy_date is None:
                    if gc >= buy_green_count:
                        pending_buy_date = dt + timedelta(days=entry_delay_days)
                        pending_buy_reason = _buy_reason(gc, r, buy_ratio, buy_green_count)
                if pending_buy_date is not None and dt >= pending_buy_date:
                    entry_reason_today = pending_buy_reason
                    pending_buy_date = None
                    pending_buy_reason = None

            if entry_reason_today is not None:
                holding = True
                entry_date = dt
                entry_price = price
                entry_reason = entry_reason_today
                equity_at_entry = running_equity
                sell_noise_state = None  # defensive: a fresh position starts with no open episode
        else:
            exit_reason_today = None
            # Minimum holding period: no sell trigger (immediate ratio or
            # delayed green_count) is even evaluated until this elapses.
            # `.days` on a Timestamp difference is a fixed calendar-day count
            # (date2 - date1), unaffected by weekends/holidays even though
            # `dates` itself only contains trading days.
            if (dt - entry_date).days >= min_holding_days:
                if r <= sell_ratio:
                    # Ratio sells are immediate: no delay, and this preempts any
                    # still-pending green_count order.
                    exit_reason_today = _sell_reason(gc, r, sell_ratio, sell_green_count)
                    pending_sell_date = None
                    pending_sell_reason = None
                else:
                    if pending_sell_date is None:
                        if gc <= sell_green_count:
                            pending_sell_date = dt + timedelta(days=exit_delay_days)
                            pending_sell_reason = _sell_reason(gc, r, sell_ratio, sell_green_count)
                    if pending_sell_date is not None and dt >= pending_sell_date:
                        exit_reason_today = pending_sell_reason
                        pending_sell_date = None
                        pending_sell_reason = None

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
                    # sell_noise_band_pct's derivation above), but the first
                    # SELL_NOISE_BAND_MIN_CHECK_TRADING_DAYS-1 days are never
                    # even checked (too short a window for a drop to mean
                    # anything beyond random-walk noise).
                    elapsed_trading_days = sell_noise_state["elapsed_trading_days"] + 1
                    sell_noise_state["elapsed_trading_days"] = elapsed_trading_days
                    if elapsed_trading_days >= SELL_NOISE_BAND_MIN_CHECK_TRADING_DAYS:
                        band_pct = sell_noise_band_pct(elapsed_trading_days)
                        price_dropped = price <= d0_price * (1.0 - band_pct)
                    else:
                        band_pct = None
                        price_dropped = False
                    if price_dropped:
                        resolved = True
                        sold = True
                    elif elapsed_trading_days >= SELL_NOISE_BAND_MAX_TRADING_DAYS:
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
                        base_reason = _sell_reason(gc, r, sell_ratio, sell_green_count) or "관찰모드 종료"
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
                sma_long_today = gold_sma_long.loc[dt]
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
                fee_factor = _fee_decay((dt - entry_date).days, gold_holding_fee_annual_pct)
                running_equity = equity_at_entry * (exit_price / entry_price) * fee_factor
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
            fee_factor = _fee_decay((dt - entry_date).days, gold_holding_fee_annual_pct)
            equity_values.append(equity_at_entry * (price / entry_price) * fee_factor)
        else:
            equity_values.append(running_equity)
        holding_values.append(holding)

    if holding:
        last_dt = dates[-1]
        last_price = float(gold.loc[last_dt])
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

    equity_curve = pd.Series(equity_values, index=dates, name="strategy_equity")
    # Buy & Hold holds continuously from the first date, so the fee compounds
    # over each row's elapsed calendar days since the very start (unlike the
    # strategy curve, which resets its clock at each entry_date).
    elapsed_since_start = (dates - dates[0]).days.to_numpy()
    bh_fee_decay = (1.0 - gold_holding_fee_annual_pct / 100.0) ** (elapsed_since_start / 365.25)
    bh_equity_curve = ((gold / gold.iloc[0]) * bh_fee_decay).rename("bh_equity")
    holding_curve = pd.Series(holding_values, index=dates, name="holding")
    return trades, equity_curve, bh_equity_curve, holding_curve, sell_noise_log


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
    gold_holding_fee_annual_pct: float = 0.0,
) -> dict:
    """The full-period CAGR variant that fills non-holding days with an
    assumed bond return instead of leaving them flat: holding days compound at
    gold's actual day-over-day return (net of gold_holding_fee_annual_pct, if
    any), non-holding days compound at `bond_annual_yield` annualized over the
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

    The returned "hybrid_equity_curve" (same index as holding_curve, starting
    at 1.0 on the first date) is the day-by-day equity behind hybrid_cagr/
    hybrid_total_return — built by the same loop, so a chart plotting it is
    guaranteed to agree with those two scalars exactly (no separate
    recomputation to drift out of sync).
    """
    dates = holding_curve.index
    hybrid_equity = 1.0
    equity_values = [1.0]
    non_holding_days = 0
    total_days = 0
    for i in range(1, len(dates)):
        elapsed_days = (dates[i] - dates[i - 1]).days
        total_days += elapsed_days
        if bool(holding_curve.iloc[i - 1]):
            factor = float(gold.iloc[i] / gold.iloc[i - 1]) * _fee_decay(
                elapsed_days, gold_holding_fee_annual_pct
            )
        else:
            factor = (1.0 + bond_annual_yield) ** (elapsed_days / 365.25)
            non_holding_days += elapsed_days
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

    A year the strategy spent entirely in cash naturally comes out to 0%, since
    the equity curve doesn't move during cash periods — no special-casing needed.
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


def prepare_signals(
    as_of: date | None = None,
    years: int = BACKTEST_YEARS,
    gold_price_basis: str = config.GOLD_PRICE_BASIS_DEFAULT,
) -> pd.DataFrame:
    """The network-bound half of the pipeline: fetch + compute signals + trim
    to the backtest window. Independent of the buy/sell delay settings, so
    callers can cache this and re-run `simulate()` cheaply when only the
    delay changes."""
    raw = fetch_raw_data(as_of, years=years, gold_price_basis=gold_price_basis)
    signals = compute_signals(raw)
    return trim_to_backtest_window(signals, as_of, years=years)


def simulate(
    signals: pd.DataFrame,
    entry_delay_days: int = 0,
    exit_delay_days: int = 0,
    use_reentry_trigger: bool = True,
    use_reentry_freq_limit: bool = True,
    reentry_freq_limit_days: int = DEFAULT_REENTRY_FREQ_LIMIT_DAYS,
    long_trend_buffer_pct: float = DEFAULT_LONG_TREND_BUFFER_PCT,
    use_new_high_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER,
    buy_ratio: float = BUY_RATIO,
    sell_ratio: float = SELL_RATIO,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
    bond_annual_yield: float = DEFAULT_BOND_ANNUAL_YIELD,
    use_sell_noise_filter: bool = True,
    sell_noise_filter_buffer_pct: float = DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT,
    use_daily_band_confirmation: bool = DEFAULT_SELL_NOISE_USE_DAILY_BAND,
    sell_noise_filter_drop_pct: float = DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
    gold_holding_fee_annual_pct: float = 0.0,
) -> dict:
    """The pure-computation half: run the trade state machine over already-
    prepared signals and derive trades/equity curves/metrics/yearly returns."""
    trades, equity_curve, bh_equity_curve, holding_curve, sell_noise_log = run_backtest(
        signals,
        entry_delay_days=entry_delay_days,
        exit_delay_days=exit_delay_days,
        use_reentry_trigger=use_reentry_trigger,
        use_reentry_freq_limit=use_reentry_freq_limit,
        reentry_freq_limit_days=reentry_freq_limit_days,
        long_trend_buffer_pct=long_trend_buffer_pct,
        use_new_high_trigger=use_new_high_trigger,
        buy_ratio=buy_ratio,
        sell_ratio=sell_ratio,
        buy_green_count=buy_green_count,
        sell_green_count=sell_green_count,
        min_holding_days=min_holding_days,
        use_sell_noise_filter=use_sell_noise_filter,
        sell_noise_filter_buffer_pct=sell_noise_filter_buffer_pct,
        use_daily_band_confirmation=use_daily_band_confirmation,
        sell_noise_filter_drop_pct=sell_noise_filter_drop_pct,
        gold_holding_fee_annual_pct=gold_holding_fee_annual_pct,
    )
    metrics_out = compute_metrics(trades, equity_curve, bh_equity_curve)
    hybrid = compute_hybrid_cagr(
        holding_curve, signals["gold"], bond_annual_yield, gold_holding_fee_annual_pct
    )
    hybrid_equity_curve = hybrid.pop("hybrid_equity_curve")
    metrics_out.update(hybrid)
    yearly = yearly_returns(equity_curve, bh_equity_curve)
    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "bh_equity_curve": bh_equity_curve,
        "holding_curve": holding_curve,
        "hybrid_equity_curve": hybrid_equity_curve,
        "metrics": metrics_out,
        "yearly_returns": yearly,
        "sell_noise_log": sell_noise_log,
    }


def run(
    as_of: date | None = None,
    years: int = BACKTEST_YEARS,
    entry_delay_days: int = 0,
    exit_delay_days: int = 0,
    use_reentry_trigger: bool = True,
    use_reentry_freq_limit: bool = True,
    reentry_freq_limit_days: int = DEFAULT_REENTRY_FREQ_LIMIT_DAYS,
    long_trend_buffer_pct: float = DEFAULT_LONG_TREND_BUFFER_PCT,
    use_new_high_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER,
    buy_ratio: float = BUY_RATIO,
    sell_ratio: float = SELL_RATIO,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
    bond_annual_yield: float = DEFAULT_BOND_ANNUAL_YIELD,
    gold_price_basis: str = config.GOLD_PRICE_BASIS_DEFAULT,
    use_sell_noise_filter: bool = True,
    sell_noise_filter_buffer_pct: float = DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT,
    use_daily_band_confirmation: bool = DEFAULT_SELL_NOISE_USE_DAILY_BAND,
    sell_noise_filter_drop_pct: float = DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
    gold_holding_fee_annual_pct: float = 0.0,
) -> dict:
    signals = prepare_signals(as_of, years=years, gold_price_basis=gold_price_basis)
    result = simulate(
        signals,
        entry_delay_days=entry_delay_days,
        exit_delay_days=exit_delay_days,
        use_reentry_trigger=use_reentry_trigger,
        use_reentry_freq_limit=use_reentry_freq_limit,
        reentry_freq_limit_days=reentry_freq_limit_days,
        long_trend_buffer_pct=long_trend_buffer_pct,
        use_new_high_trigger=use_new_high_trigger,
        buy_ratio=buy_ratio,
        sell_ratio=sell_ratio,
        buy_green_count=buy_green_count,
        sell_green_count=sell_green_count,
        min_holding_days=min_holding_days,
        bond_annual_yield=bond_annual_yield,
        use_sell_noise_filter=use_sell_noise_filter,
        sell_noise_filter_buffer_pct=sell_noise_filter_buffer_pct,
        use_daily_band_confirmation=use_daily_band_confirmation,
        sell_noise_filter_drop_pct=sell_noise_filter_drop_pct,
        gold_holding_fee_annual_pct=gold_holding_fee_annual_pct,
    )
    result["signals"] = signals
    return result

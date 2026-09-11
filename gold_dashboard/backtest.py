"""Signal-based backtest: real-rate/DXY MA breakout signals plus a gold/silver-ratio
threshold, compared against a same-period Buy & Hold benchmark.

Buy (while flat): green_count >= BUY_GREEN_COUNT OR gold/silver ratio >= buy_ratio
  OR the reentry trigger — both of:
    ① long-term trend filter (necessary condition): gold close is at least
      long_trend_buffer_pct% above its LONG_TREND_WINDOW-day SMA, AND that SMA
      itself is higher than it was LONG_TREND_SLOPE_LOOKBACK trading days ago
      (the 200-day SMA must itself be trending up, not just be under price).
    ② short-term re-breakout trigger: gold closes back above its
      SHORT_REENTRY_WINDOW-day SMA today, having been at/below it yesterday.
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

MA_WINDOWS = [60, 30, 5]

BACKTEST_YEARS = ts.YEARS  # default analysis period; the 유효성 검증 page lets the
# user override this per-session (3-10 years, see MIN/MAX_BACKTEST_YEARS below)
# without affecting the main dashboard's fixed-window charts.
MIN_BACKTEST_YEARS = 3
MAX_BACKTEST_YEARS = 10
# Extra calendar days of history fetched before the analysis start. Sized for
# the reentry trigger's 200-day long-term SMA (needs ~200 trading days ≈ 280
# calendar days of warm-up), not just the 60-day SMA used elsewhere — deliberately
# independent of timeseries.BUFFER_DAYS, which only needs to cover the latter
# and stays smaller for the main dashboard's per-indicator chart fetches.
BUFFER_DAYS = 400

BUY_GREEN_COUNT = 6
# Shared with the main dashboard's chart shading/reference lines (config.py)
# so both can never drift apart.
BUY_RATIO = config.DEFAULT_GS_RATIO_BUY_THRESHOLD
SELL_GREEN_COUNT = 0
SELL_RATIO = config.DEFAULT_GS_RATIO_SELL_THRESHOLD
# Not a day-trading strategy by design: default to holding at least a month
# before any sell trigger is even evaluated (still user-adjustable).
DEFAULT_MIN_HOLDING_DAYS = 30

# Reentry trigger (replaces the previous "fresh gold record high" trigger):
# necessary condition is a long-term uptrend filter on gold's LONG_TREND_WINDOW
# -day SMA (see compute_reentry_trigger: a % buffer above it, and the SMA
# itself sloping up over LONG_TREND_SLOPE_LOOKBACK trading days); the trigger
# itself fires the day gold closes back above its SHORT_REENTRY_WINDOW-day SMA,
# having been at/below it the previous day (a short-term re-breakout).
LONG_TREND_WINDOW = 200
SHORT_REENTRY_WINDOW = 20
# How far above its own 200-day SMA gold's close must be (as a %) for the
# long-term trend filter to hold. User-adjustable per run.
DEFAULT_LONG_TREND_BUFFER_PCT = 5.0
# How many trading days back the 200-day SMA's slope is measured over (today's
# SMA must exceed the SMA from this many trading days ago).
LONG_TREND_SLOPE_LOOKBACK = 20
# Default cap on how often the reentry trigger alone (not other buy triggers)
# may fire — at most once per this many calendar days. User-togglable per run.
DEFAULT_REENTRY_FREQ_LIMIT_DAYS = 30

# Default assumed annual yield for the "미보유기간 채권투자 가정" hybrid CAGR
# below. Adjustable per-run via simulate()'s bond_annual_yield argument.
DEFAULT_BOND_ANNUAL_YIELD = 0.10


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
    df["gold_short_ma_crossover_up"] = above_short & ~above_short.shift(1).fillna(False)
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
       - gold close is at least `long_trend_buffer_pct`% above its 200-day SMA
         (not just barely above it).
       - that SMA is itself higher than it was LONG_TREND_SLOPE_LOOKBACK
         trading days ago (the 200-day SMA must be sloping up, i.e. gold is in
         a genuine uptrend, not just a flat/declining SMA that price happens
         to sit above).
    ② short-term re-breakout: gold_short_ma_crossover_up (see compute_signals).

    Fires only on days both ① and ② hold.
    """
    long_sma = df["gold_sma_long"]
    above_buffer = (df["gold"] > long_sma * (1.0 + long_trend_buffer_pct / 100.0)).fillna(False)
    long_sma_rising = (long_sma > long_sma.shift(LONG_TREND_SLOPE_LOOKBACK)).fillna(False)
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
) -> str:
    reasons = []
    if gc >= buy_green_count:
        reasons.append(f"green_count≥{buy_green_count}")
    if r >= buy_ratio:
        reasons.append(f"금/은비율≥{buy_ratio:g}")
    if include_reentry:
        reasons.append(f"재진입(200일선+{long_trend_buffer_pct:g}%·우상향, 20일선 상향돌파)")
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
    buy_ratio: float = BUY_RATIO,
    sell_ratio: float = SELL_RATIO,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
) -> tuple[list[dict], pd.Series, pd.Series, pd.Series]:
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
    - **gold/silver ratio** (buy: `>= buy_ratio`, sell: `<= sell_ratio`) and the
      **reentry trigger** (see compute_reentry_trigger: gold sufficiently above
      a rising long-term SMA AND a fresh short-term SMA re-breakout today) are
      both *immediate*: they always fill the same day, ignoring the delay
      settings, and preempt any green_count order still pending.

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

    Returns (trades, equity_curve, bh_equity_curve, holding_curve). Both equity
    curves start at 1.0 on the first date. The strategy curve is flat (1.0x,
    i.e. 0% return) while in cash and compounds only through held periods; the
    Buy & Hold curve is always invested from that same first date.
    holding_curve is a same-index boolean Series (True = holding gold that
    day), used by compute_hybrid_cagr() to build the "미보유기간 채권투자
    가정" hybrid equity curve.
    """
    dates = signals.index
    gold = signals["gold"]
    green_count = signals["green_count"]
    ratio = signals["gold_silver_ratio"]
    reentry_trigger = compute_reentry_trigger(signals, long_trend_buffer_pct)

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

            entry_reason_today = None
            if reentry_ready or r >= buy_ratio:
                # Ratio/reentry buys are immediate: no delay, and this
                # preempts any still-pending green_count order.
                entry_reason_today = _buy_reason(
                    gc,
                    r,
                    buy_ratio,
                    buy_green_count,
                    include_reentry=reentry_ready,
                    long_trend_buffer_pct=long_trend_buffer_pct,
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

            if exit_reason_today is not None:
                exit_price = price
                running_equity = equity_at_entry * (exit_price / entry_price)
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

        equity_values.append(equity_at_entry * (price / entry_price) if holding else running_equity)
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

    equity_curve = pd.Series(equity_values, index=dates, name="strategy_equity")
    bh_equity_curve = (gold / gold.iloc[0]).rename("bh_equity")
    holding_curve = pd.Series(holding_values, index=dates, name="holding")
    return trades, equity_curve, bh_equity_curve, holding_curve


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
    holding_curve: pd.Series, gold: pd.Series, bond_annual_yield: float
) -> dict:
    """The full-period CAGR variant that fills non-holding days with an
    assumed bond return instead of leaving them flat: holding days compound at
    gold's actual day-over-day return, non-holding days compound at
    `bond_annual_yield` annualized over the elapsed calendar days since the
    previous row. This directly complements compute_metrics()'s
    "strategy_cagr" (which is invested-days-only) with a whole-period figure,
    so the two can be compared side by side.

    Each step from day i-1 to day i is classified by whether the position was
    already held going INTO that step (holding_curve.iloc[i-1]), not whether
    it ends the step held — matching run_backtest()'s own "buy/sell at that
    day's close" convention, where the entry day itself earns no gold return
    (bought at today's close, so today's price move isn't captured) and the
    exit day earns the full gold return (held all day, sold at today's
    close). Using the current day's flag instead would double-count the
    entry day's price move that the strategy never actually captured.
    """
    dates = holding_curve.index
    hybrid_equity = 1.0
    non_holding_days = 0
    total_days = 0
    for i in range(1, len(dates)):
        elapsed_days = (dates[i] - dates[i - 1]).days
        total_days += elapsed_days
        if bool(holding_curve.iloc[i - 1]):
            factor = float(gold.iloc[i] / gold.iloc[i - 1])
        else:
            factor = (1.0 + bond_annual_yield) ** (elapsed_days / 365.25)
            non_holding_days += elapsed_days
        hybrid_equity *= factor

    hybrid_total_return = hybrid_equity - 1.0
    hybrid_cagr = hybrid_equity ** (365.25 / total_days) - 1.0 if total_days > 0 else None
    non_holding_fraction = non_holding_days / total_days if total_days > 0 else None

    return {
        "bond_annual_yield": bond_annual_yield,
        "hybrid_total_return": hybrid_total_return,
        "hybrid_cagr": hybrid_cagr,
        "non_holding_days": non_holding_days,
        "non_holding_fraction": non_holding_fraction,
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
    buy_ratio: float = BUY_RATIO,
    sell_ratio: float = SELL_RATIO,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
    bond_annual_yield: float = DEFAULT_BOND_ANNUAL_YIELD,
) -> dict:
    """The pure-computation half: run the trade state machine over already-
    prepared signals and derive trades/equity curves/metrics/yearly returns."""
    trades, equity_curve, bh_equity_curve, holding_curve = run_backtest(
        signals,
        entry_delay_days=entry_delay_days,
        exit_delay_days=exit_delay_days,
        use_reentry_trigger=use_reentry_trigger,
        use_reentry_freq_limit=use_reentry_freq_limit,
        reentry_freq_limit_days=reentry_freq_limit_days,
        long_trend_buffer_pct=long_trend_buffer_pct,
        buy_ratio=buy_ratio,
        sell_ratio=sell_ratio,
        buy_green_count=buy_green_count,
        sell_green_count=sell_green_count,
        min_holding_days=min_holding_days,
    )
    metrics_out = compute_metrics(trades, equity_curve, bh_equity_curve)
    metrics_out.update(compute_hybrid_cagr(holding_curve, signals["gold"], bond_annual_yield))
    yearly = yearly_returns(equity_curve, bh_equity_curve)
    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "bh_equity_curve": bh_equity_curve,
        "holding_curve": holding_curve,
        "metrics": metrics_out,
        "yearly_returns": yearly,
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
    buy_ratio: float = BUY_RATIO,
    sell_ratio: float = SELL_RATIO,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
    bond_annual_yield: float = DEFAULT_BOND_ANNUAL_YIELD,
    gold_price_basis: str = config.GOLD_PRICE_BASIS_DEFAULT,
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
        buy_ratio=buy_ratio,
        sell_ratio=sell_ratio,
        buy_green_count=buy_green_count,
        sell_green_count=sell_green_count,
        min_holding_days=min_holding_days,
        bond_annual_yield=bond_annual_yield,
    )
    result["signals"] = signals
    return result

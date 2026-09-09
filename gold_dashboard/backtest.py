"""Signal-based backtest: real-rate/DXY MA breakout signals plus a gold/silver-ratio
threshold, compared against a same-period Buy & Hold benchmark.

Buy (while flat): green_count >= BUY_GREEN_COUNT OR gold/silver ratio >= buy_ratio
  (OR, optionally, a fresh gold record high)
Sell (while holding): green_count == SELL_GREEN_COUNT OR gold/silver ratio <= sell_ratio

All fills happen at the signal day's own close. The green_count trigger can be
delayed by a configurable number of days; the ratio (and new-high) triggers are
always immediate — see run_backtest() for the full timing rules.
"""

from datetime import date, timedelta

import pandas as pd

from . import data_sources as ds
from . import metrics
from .timeutil import today_kst

DXY_TICKERS = ["DX-Y.NYB", "^DXY", "DX=F"]
MA_WINDOWS = [60, 30, 5]

BACKTEST_YEARS = 7
BUFFER_DAYS = 90  # extra calendar days of history fetched before the analysis start,
# so the 60-day SMA already has a full window on day 1 of the backtest.

BUY_GREEN_COUNT = 5
BUY_RATIO = 90
SELL_GREEN_COUNT = 0
SELL_RATIO = 40


def fetch_raw_data(as_of: date | None = None) -> pd.DataFrame:
    """Fetch real_rate/dxy/gold/silver as one date-aligned, forward-filled frame.

    Covers BACKTEST_YEARS + BUFFER_DAYS of history ending at `as_of` (default
    today). Different markets close on different days (rates vs. commodities),
    so the four series are joined on the union of their dates and gaps are
    forward-filled from the prior available value.
    """
    end_date = as_of or today_kst()
    fetch_start = end_date - timedelta(days=BACKTEST_YEARS * 365 + BUFFER_DAYS)
    yf_end = end_date + timedelta(days=1)  # yfinance's `end` is exclusive

    real_rate = ds.fetch_fred_series("DFII10", end=end_date)
    real_rate = real_rate[real_rate.index >= pd.Timestamp(fetch_start)]
    dxy = ds.fetch_yfinance_close(DXY_TICKERS, start=fetch_start, end=yf_end)
    gold = ds.fetch_yfinance_close("GC=F", start=fetch_start, end=yf_end)
    silver = ds.fetch_yfinance_close("SI=F", start=fetch_start, end=yf_end)

    df = pd.concat(
        [
            real_rate.rename("real_rate"),
            dxy.rename("dxy"),
            gold.rename("gold"),
            silver.rename("silver"),
        ],
        axis=1,
        join="outer",
    ).sort_index()
    df = df[df.index <= pd.Timestamp(end_date)]
    df = df.ffill()
    df = df.dropna()  # drop the leading stretch before all four series have started
    return df


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Adds SMA-based gold-friendly flags for real_rate/dxy, green_count (0-6),
    the raw gold/silver ratio (no MA needed for the ratio itself), and a
    gold_new_high flag (today's close exceeds every prior close seen so far
    in the fetched history, i.e. a fresh record high)."""
    df = df.copy()
    gf_cols = []
    for col in ("real_rate", "dxy"):
        for window in MA_WINDOWS:
            sma = metrics.compute_sma(df[col], window)
            gf_col = f"{col}_gf_{window}"
            # Both are inverse-correlation indicators: below its own MA is gold-friendly
            # (same rule as app.py's build_table highlighting).
            df[gf_col] = (df[col] < sma).fillna(False)
            gf_cols.append(gf_col)
    df["green_count"] = df[gf_cols].sum(axis=1).astype(int)
    df["gold_silver_ratio"] = df["gold"] / df["silver"]
    prior_high = df["gold"].shift(1).cummax()
    df["gold_new_high"] = (df["gold"] > prior_high).fillna(False)
    return df


def trim_to_backtest_window(df: pd.DataFrame, as_of: date | None = None) -> pd.DataFrame:
    end_date = as_of or today_kst()
    start_date = end_date - timedelta(days=BACKTEST_YEARS * 365)
    trimmed = df[df.index >= pd.Timestamp(start_date)]
    if trimmed.empty:
        raise RuntimeError("no data available in the requested backtest window")
    return trimmed


def _buy_reason(
    gc: int, r: float, buy_ratio: float, buy_green_count: int, include_new_high: bool = False
) -> str:
    reasons = []
    if gc >= buy_green_count:
        reasons.append(f"green_count≥{buy_green_count}")
    if r >= buy_ratio:
        reasons.append(f"금/은비율≥{buy_ratio:g}")
    if include_new_high:
        reasons.append("신고가 갱신")
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
    use_new_high_buy: bool = False,
    buy_ratio: float = BUY_RATIO,
    sell_ratio: float = SELL_RATIO,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
) -> tuple[list[dict], pd.Series, pd.Series]:
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
    - **gold/silver ratio** (buy: `>= buy_ratio`, sell: `<= sell_ratio`) and, if
      `use_new_high_buy` is on, a **fresh record high in gold** (see
      compute_signals' gold_new_high) are both *immediate*: they always fill
      the same day, ignoring the delay settings, and preempt any green_count
      order still pending.

    `min_holding_days`: once a position is opened, every sell trigger (both the
    immediate ratio sell and the delayed green_count sell) is ignored entirely
    until at least this many calendar days have passed since entry — a minimum
    holding period for a longer-horizon strategy, not day-trading. No pending
    sell order can even be scheduled during this window; sell evaluation
    resumes normally (delay settings included) once it has elapsed.

    Each trade records the reason(s) that triggered its order, as of the day
    the signal fired — for a delayed green_count order this is the day it was
    scheduled, not necessarily still true by execution day.

    Returns (trades, equity_curve, bh_equity_curve). Both equity curves start
    at 1.0 on the first date. The strategy curve is flat (1.0x, i.e. 0% return)
    while in cash and compounds only through held periods; the Buy & Hold curve
    is always invested from that same first date.
    """
    dates = signals.index
    gold = signals["gold"]
    green_count = signals["green_count"]
    ratio = signals["gold_silver_ratio"]
    new_high = signals["gold_new_high"]

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
    trades: list[dict] = []
    equity_values = []

    for dt in dates:
        gc = int(green_count.loc[dt])
        r = float(ratio.loc[dt])
        price = float(gold.loc[dt])
        is_new_high = bool(new_high.loc[dt])

        if not holding:
            entry_reason_today = None
            if (use_new_high_buy and is_new_high) or r >= buy_ratio:
                # Ratio/new-high buys are immediate: no delay, and this
                # preempts any still-pending green_count order.
                entry_reason_today = _buy_reason(
                    gc,
                    r,
                    buy_ratio,
                    buy_green_count,
                    include_new_high=(use_new_high_buy and is_new_high),
                )
                pending_buy_date = None
                pending_buy_reason = None
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
    return trades, equity_curve, bh_equity_curve


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


def prepare_signals(as_of: date | None = None) -> pd.DataFrame:
    """The network-bound half of the pipeline: fetch + compute signals + trim
    to the backtest window. Independent of the buy/sell delay settings, so
    callers can cache this and re-run `simulate()` cheaply when only the
    delay changes."""
    raw = fetch_raw_data(as_of)
    signals = compute_signals(raw)
    return trim_to_backtest_window(signals, as_of)


def simulate(
    signals: pd.DataFrame,
    entry_delay_days: int = 0,
    exit_delay_days: int = 0,
    use_new_high_buy: bool = False,
    buy_ratio: float = BUY_RATIO,
    sell_ratio: float = SELL_RATIO,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
) -> dict:
    """The pure-computation half: run the trade state machine over already-
    prepared signals and derive trades/equity curves/metrics/yearly returns."""
    trades, equity_curve, bh_equity_curve = run_backtest(
        signals,
        entry_delay_days=entry_delay_days,
        exit_delay_days=exit_delay_days,
        use_new_high_buy=use_new_high_buy,
        buy_ratio=buy_ratio,
        sell_ratio=sell_ratio,
        buy_green_count=buy_green_count,
        sell_green_count=sell_green_count,
        min_holding_days=min_holding_days,
    )
    metrics_out = compute_metrics(trades, equity_curve, bh_equity_curve)
    yearly = yearly_returns(equity_curve, bh_equity_curve)
    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "bh_equity_curve": bh_equity_curve,
        "metrics": metrics_out,
        "yearly_returns": yearly,
    }


def run(
    as_of: date | None = None,
    entry_delay_days: int = 0,
    exit_delay_days: int = 0,
    use_new_high_buy: bool = False,
    buy_ratio: float = BUY_RATIO,
    sell_ratio: float = SELL_RATIO,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
) -> dict:
    signals = prepare_signals(as_of)
    result = simulate(
        signals,
        entry_delay_days=entry_delay_days,
        exit_delay_days=exit_delay_days,
        use_new_high_buy=use_new_high_buy,
        buy_ratio=buy_ratio,
        sell_ratio=sell_ratio,
        buy_green_count=buy_green_count,
        sell_green_count=sell_green_count,
        min_holding_days=min_holding_days,
    )
    result["signals"] = signals
    return result

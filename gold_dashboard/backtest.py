"""Signal-based backtest: real-rate/DXY MA breakout signals plus a gold/silver-ratio
threshold, compared against a same-period Buy & Hold benchmark.

Buy (while flat): green_count >= BUY_GREEN_COUNT OR gold/silver ratio >= BUY_RATIO
Sell (while holding): green_count == SELL_GREEN_COUNT OR gold/silver ratio <= SELL_RATIO
Fills happen at the signal day's own close.
"""

from datetime import date, timedelta

import pandas as pd

from . import data_sources as ds
from . import metrics

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
    end_date = as_of or date.today()
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
    and the raw gold/silver ratio (no MA needed for the ratio itself)."""
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
    return df


def trim_to_backtest_window(df: pd.DataFrame, as_of: date | None = None) -> pd.DataFrame:
    end_date = as_of or date.today()
    start_date = end_date - timedelta(days=BACKTEST_YEARS * 365)
    trimmed = df[df.index >= pd.Timestamp(start_date)]
    if trimmed.empty:
        raise RuntimeError("no data available in the requested backtest window")
    return trimmed


def run_backtest(signals: pd.DataFrame) -> tuple[list[dict], pd.Series, pd.Series]:
    """Walks the signal frame day by day applying the buy/sell rules.

    Returns (trades, equity_curve, bh_equity_curve). Both equity curves start
    at 1.0 on the first date. The strategy curve is flat (1.0x, i.e. 0% return)
    while in cash and compounds only through held periods; the Buy & Hold curve
    is always invested from that same first date.
    """
    dates = signals.index
    gold = signals["gold"]
    green_count = signals["green_count"]
    ratio = signals["gold_silver_ratio"]

    holding = False
    entry_date = None
    entry_price = None
    equity_at_entry = None  # strategy equity value at the moment this position was opened
    running_equity = 1.0
    trades: list[dict] = []
    equity_values = []

    for dt in dates:
        gc = int(green_count.loc[dt])
        r = float(ratio.loc[dt])
        price = float(gold.loc[dt])

        if not holding:
            if gc >= BUY_GREEN_COUNT or r >= BUY_RATIO:
                holding = True
                entry_date = dt
                entry_price = price
                equity_at_entry = running_equity
        else:
            if gc == SELL_GREEN_COUNT or r <= SELL_RATIO:
                exit_price = price
                running_equity = equity_at_entry * (exit_price / entry_price)
                trades.append(
                    {
                        "entry_date": entry_date,
                        "entry_price": entry_price,
                        "exit_date": dt,
                        "exit_price": exit_price,
                        "hold_days": (dt - entry_date).days,
                        "period_return": exit_price / entry_price - 1.0,
                        "open": False,
                    }
                )
                holding = False
                entry_date = None
                entry_price = None
                equity_at_entry = None

        equity_values.append(equity_at_entry * (price / entry_price) if holding else running_equity)

    if holding:
        last_dt = dates[-1]
        last_price = float(gold.loc[last_dt])
        trades.append(
            {
                "entry_date": entry_date,
                "entry_price": entry_price,
                "exit_date": None,
                "exit_price": last_price,
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
    """Calendar-year realized returns for both curves (first/last years are partial).

    A year the strategy spent entirely in cash naturally comes out to 0%, since
    the equity curve doesn't move during cash periods — no special-casing needed.
    """
    years = sorted(set(equity_curve.index.year))
    rows = []
    prev_strategy = 1.0
    prev_bh = 1.0
    for year in years:
        year_dates = equity_curve.index[equity_curve.index.year == year]
        last_date = year_dates[-1]
        year_end_strategy = float(equity_curve.loc[last_date])
        year_end_bh = float(bh_equity_curve.loc[last_date])
        rows.append(
            {
                "year": year,
                "strategy_return": year_end_strategy / prev_strategy - 1.0,
                "bh_return": year_end_bh / prev_bh - 1.0,
            }
        )
        prev_strategy = year_end_strategy
        prev_bh = year_end_bh
    return pd.DataFrame(rows)


def run(as_of: date | None = None) -> dict:
    raw = fetch_raw_data(as_of)
    signals = compute_signals(raw)
    signals = trim_to_backtest_window(signals, as_of)
    trades, equity_curve, bh_equity_curve = run_backtest(signals)
    metrics_out = compute_metrics(trades, equity_curve, bh_equity_curve)
    yearly = yearly_returns(equity_curve, bh_equity_curve)
    return {
        "signals": signals,
        "trades": trades,
        "equity_curve": equity_curve,
        "bh_equity_curve": bh_equity_curve,
        "metrics": metrics_out,
        "yearly_returns": yearly,
    }

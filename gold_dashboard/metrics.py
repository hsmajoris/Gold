"""Moving-average and breakout-streak computations."""

import pandas as pd


def compute_sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def breakout_streak(price: pd.Series, ma: pd.Series) -> int:
    """Number of consecutive most-recent days where price stayed above the MA.

    Resets to 0 as soon as price closes back at or below the MA.
    """
    aligned = pd.concat([price, ma], axis=1, keys=["price", "ma"]).dropna()
    if aligned.empty:
        return 0
    above = aligned["price"] > aligned["ma"]
    streak = 0
    for is_above in reversed(above.tolist()):
        if is_above:
            streak += 1
        else:
            break
    return streak

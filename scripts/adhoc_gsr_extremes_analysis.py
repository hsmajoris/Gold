"""One-off analysis (not part of the dashboard app): for each 5-year bucket
from 2006 to now, find the 5 trading days with the highest gold/silver ratio
and the 5 with the lowest, simulate buying gold at each and holding for
3 months / 12 months, and compare the average annualized return of the
"ratio high" group vs the "ratio low" group.

Meant to be run once via a temporary workflow_dispatch (this sandbox can't
reach Yahoo Finance directly, but the GitHub Actions runner already does for
the daily data-refresh job) and deleted afterward — not a permanent script.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from gold_dashboard import timeseries

BUCKET_START_YEAR = 2006
BUCKET_SIZE_YEARS = 5
TOP_N = 5


def month_offset(d: pd.Timestamp, months: int) -> pd.Timestamp:
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    # Clamp the day to the target month's length (e.g. Jan 31 + 1 month -> Feb 28/29).
    day = d.day
    next_month_first = pd.Timestamp(year=year + (month // 12), month=(month % 12) + 1, day=1)
    last_day_of_month = (next_month_first - timedelta(days=1)).day
    return pd.Timestamp(year=year, month=month, day=min(day, last_day_of_month))


def forward_price(series: pd.Series, entry_date: pd.Timestamp, months: int):
    target = month_offset(entry_date, months)
    pos = series.index.searchsorted(target)
    if pos >= len(series):
        return None, None
    return float(series.iloc[pos]), series.index[pos]


def annualize(simple_return: float, days_elapsed: int) -> float:
    return (1.0 + simple_return) ** (365.25 / days_elapsed) - 1.0


def main() -> None:
    today = date.today()
    years_back = today.year - BUCKET_START_YEAR + 1
    gold = timeseries.fetch_raw_series("gold", as_of=today, years=years_back, buffer_days=5)
    silver = timeseries.fetch_raw_series("silver", as_of=today, years=years_back, buffer_days=5)

    combined = pd.concat([gold.rename("gold"), silver.rename("silver")], axis=1).dropna()
    combined = combined[combined.index >= pd.Timestamp(f"{BUCKET_START_YEAR}-01-01")]
    ratio = (combined["gold"] / combined["silver"]).rename("ratio")

    print(f"data range: {combined.index.min().date()} .. {combined.index.max().date()} "
          f"({len(combined)} trading days)")
    print()

    last_date = combined.index.max()
    bucket_ids = sorted({(d.year - BUCKET_START_YEAR) // BUCKET_SIZE_YEARS for d in combined.index})

    overall_rows = []

    for bid in bucket_ids:
        start_year = BUCKET_START_YEAR + bid * BUCKET_SIZE_YEARS
        end_year = start_year + BUCKET_SIZE_YEARS - 1
        label = f"{start_year}-{end_year}"
        bucket_mask = (combined.index.year >= start_year) & (combined.index.year <= end_year)
        bucket_ratio = ratio[bucket_mask]
        if bucket_ratio.empty:
            continue

        # Only consider entry dates where BOTH the 3-month and 12-month forward
        # price actually exist in the fetched data (excludes the most recent
        # ~12 months, which can't have a valid 12-month-later observation yet).
        eligible_dates = []
        for d in bucket_ratio.index:
            if month_offset(d, 12) <= last_date:
                eligible_dates.append(d)
        eligible_ratio = bucket_ratio.loc[eligible_dates]
        if eligible_ratio.empty:
            print(f"=== {label}: no eligible dates (too recent for a full 12-month forward window) ===\n")
            continue

        high_dates = eligible_ratio.nlargest(TOP_N).index
        low_dates = eligible_ratio.nsmallest(TOP_N).index

        print(f"=== {label} (n eligible={len(eligible_ratio)}) ===")
        for group_label, dates in (("HIGH ratio (top 5)", high_dates), ("LOW ratio (bottom 5)", low_dates)):
            print(f"  -- {group_label} --")
            rows = []
            for d in sorted(dates):
                entry_price = float(combined.loc[d, "gold"])
                r = float(ratio.loc[d])
                p3, d3 = forward_price(combined["gold"], d, 3)
                p12, d12 = forward_price(combined["gold"], d, 12)
                ret3 = p3 / entry_price - 1.0 if p3 else None
                ret12 = p12 / entry_price - 1.0 if p12 else None
                ann3 = annualize(ret3, (d3 - d).days) if ret3 is not None else None
                ann12 = annualize(ret12, (d12 - d).days) if ret12 is not None else None
                rows.append({
                    "date": d.date(), "ratio": r, "gold_price": entry_price,
                    "ret3": ret3, "ann3": ann3, "ret12": ret12, "ann12": ann12,
                })
                print(f"    {d.date()}  ratio={r:6.2f}  gold=${entry_price:9,.2f}  "
                      f"3m: {ret3*100 if ret3 is not None else float('nan'):+6.2f}% "
                      f"(ann {ann3*100 if ann3 is not None else float('nan'):+7.2f}%)   "
                      f"12m: {ret12*100 if ret12 is not None else float('nan'):+6.2f}% "
                      f"(ann {ann12*100 if ann12 is not None else float('nan'):+7.2f}%)")
            avg_ann3 = sum(r["ann3"] for r in rows if r["ann3"] is not None) / len(rows)
            avg_ann12 = sum(r["ann12"] for r in rows if r["ann12"] is not None) / len(rows)
            avg_ratio = sum(r["ratio"] for r in rows) / len(rows)
            print(f"    -> avg ratio={avg_ratio:.2f}  avg 3m annualized={avg_ann3*100:+.2f}%  "
                  f"avg 12m annualized={avg_ann12*100:+.2f}%")
            overall_rows.append({
                "bucket": label, "group": group_label, "avg_ratio": avg_ratio,
                "avg_ann3": avg_ann3, "avg_ann12": avg_ann12,
            })
        print()

    print("=== SUMMARY TABLE ===")
    print(f"{'bucket':<10} {'group':<20} {'avg_ratio':>10} {'avg_ann_3m':>12} {'avg_ann_12m':>13}")
    for row in overall_rows:
        print(f"{row['bucket']:<10} {row['group']:<20} {row['avg_ratio']:>10.2f} "
              f"{row['avg_ann3']*100:>11.2f}% {row['avg_ann12']*100:>12.2f}%")


if __name__ == "__main__":
    main()

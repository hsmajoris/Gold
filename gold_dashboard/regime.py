"""Single source of truth for the market-regime date-range table and the
signal-strategy participation-rate calculation derived from it.

Both the 유효성 검증(백테스트) page's 국면 음영/참여율 카드 and the main
dashboard's 핵심 요약 callout read from this module instead of each keeping
its own copy — so if the signal strategy's logic/parameters change, or the
manually-curated regime date ranges below are revised, or new trading days
of data arrive, every number derived from them (cards and summary text
alike) updates together the next time the page runs. Nothing here is ever
hand-typed into a report string.
"""

import pandas as pd

from . import backtest, config

# ---- 수동으로 확정된 시장 국면 구간표 (차트를 보고 직접 지정한 값 — 어떤
# 공식으로 도출된 게 아님). "대세"(장기) 구간은 "일반" 구간과 겹칠 수 있고, 그
# 경우 5단계 참여율 계산에서는 대세 쪽 라벨을 우선 적용한다. 이 네 리스트에 안
# 걸리는 나머지 기간은 전부 보합장으로 취급. 백테스트 페이지 상단의 "국면 표시"
# 라디오는 이 표를 "장기 국면"(REGIME_SECULAR_UP/DOWN만) 세트와 "일반 국면"
# (REGIME_UPTREND/DOWNTREND만) 세트로 나눠서 보여주는 것일 뿐, 서로 독립된
# 2개의 3단계(상승/보합/하락) 구분이다 — 아래 5단계 참여율처럼 우선순위를
# 매기지 않는다.
REGIME_UPTREND = [
    ("2014-06-05", "2014-08-08"), ("2014-11-07", "2015-01-21"),
    ("2015-12-03", "2016-02-26"), ("2016-04-19", "2016-07-06"),
    ("2016-12-16", "2017-04-20"), ("2017-07-14", "2017-09-08"),
    ("2017-12-13", "2018-02-06"), ("2018-09-28", "2019-08-13"),
    ("2019-11-12", "2020-07-28"), ("2021-11-04", "2022-03-09"),
    ("2023-03-13", "2023-04-07"), ("2024-03-04", "2024-10-23"),
    ("2024-11-15", "2025-02-14"), ("2025-08-20", "2025-10-15"),
    ("2025-10-28", "2026-01-29"),
]
REGIME_DOWNTREND = [
    ("2014-03-24", "2014-06-05"), ("2014-08-08", "2014-11-07"),
    ("2015-01-21", "2015-04-27"), ("2015-08-24", "2015-11-30"),
    ("2016-07-06", "2016-12-16"), ("2017-04-20", "2017-05-16"),
    ("2017-09-08", "2017-12-13"), ("2018-06-15", "2018-09-28"),
    ("2020-07-28", "2020-11-30"), ("2021-01-06", "2021-03-05"),
    ("2025-02-14", "2025-02-27"), ("2025-10-15", "2025-10-28"),
    ("2026-01-29", "2026-07-30"),
]
REGIME_SECULAR_UP = [
    ("2015-11-30", "2016-07-06"), ("2019-11-12", "2020-07-28"),
    ("2024-03-04", "2026-01-29"),
]
REGIME_SECULAR_DOWN = [
    ("2015-01-21", "2015-11-30"), ("2020-07-28", "2020-11-30"),
    ("2026-01-29", "2026-07-30"),
]

REGIME_LABELS = ["대세상승장", "상승장", "보합장", "하락장", "대세하락장"]

# 참여율 카드/요약 문구가 함께 보여주는 세 가지 신호강도(매수 green_count/매도
# green_count) 조합 — green_count는 0~6이므로 6/0이 가장 보수적(둘 다 극단),
# 4/2가 가장 공격적이다.
THRESHOLD_PAIRS = [(6, 0), (5, 1), (4, 2)]


def _regime_ts_ranges(pairs):
    return [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in pairs]


def _in_any_range(d, ranges):
    return any(a <= d <= b for a, b in ranges)


def classify_regime_5way(dates_index):
    """Per-day label in {대세상승장, 상승장, 보합장, 하락장, 대세하락장} for every
    date in `dates_index`, giving REGIME_SECULAR_* priority over REGIME_UPTREND/
    DOWNTREND wherever they overlap; anything covered by none of the four
    ranges is 보합장."""
    up, down = _regime_ts_ranges(REGIME_UPTREND), _regime_ts_ranges(REGIME_DOWNTREND)
    sec_up, sec_down = _regime_ts_ranges(REGIME_SECULAR_UP), _regime_ts_ranges(REGIME_SECULAR_DOWN)
    labels = []
    for d in dates_index:
        d = pd.Timestamp(d)
        if _in_any_range(d, sec_up):
            labels.append("대세상승장")
        elif _in_any_range(d, sec_down):
            labels.append("대세하락장")
        elif _in_any_range(d, up):
            labels.append("상승장")
        elif _in_any_range(d, down):
            labels.append("하락장")
        else:
            labels.append("보합장")
    return labels


def _clip_ranges(pairs, lo, hi):
    out = []
    for a, b in pairs:
        a, b = pd.Timestamp(a), pd.Timestamp(b)
        s, e = max(a, lo), min(b, hi)
        if s <= e:
            out.append((s, e))
    return out


def regime_shading_shapes(up_pairs, down_pairs, lo, hi):
    """{x0,x1,kind} dicts (kind='up'|'down') for the given pair of range
    lists, clipped to [lo, hi] — the shading-radio's A/B set, not the 5-way
    participation labels. The 백테스트 page's JS turns these into full Plotly
    shape dicts (color, y-domain fraction) since the color choice/z-order
    belong to rendering, not to this data-prep step."""
    shapes = [{"x0": a.isoformat(), "x1": b.isoformat(), "kind": "up"}
              for a, b in _clip_ranges(up_pairs, lo, hi)]
    shapes += [{"x0": a.isoformat(), "x1": b.isoformat(), "kind": "down"}
               for a, b in _clip_ranges(down_pairs, lo, hi)]
    return shapes


def default_backtest_kwargs(gold_price_basis: str = config.GOLD_PRICE_BASIS_DEFAULT) -> dict:
    """The same effective defaults the 백테스트 page's sliders start a fresh
    session on (see pages/1_백테스트.py's own DEFAULTS dict) expressed as
    backtest.simulate() kwargs — every value below is read from a
    backtest.DEFAULT_* module constant (never a number typed here), so both
    that page and this module automatically stay in lockstep with whatever
    backtest.py's own defaults currently are. `buy_green_count`/
    `sell_green_count` are deliberately excluded — compute_threshold_holding
    below fills those in per THRESHOLD_PAIRS entry."""
    return dict(
        use_reentry_trigger=False,
        use_reentry_freq_limit=True,
        reentry_freq_limit_days=backtest.DEFAULT_REENTRY_FREQ_LIMIT_DAYS,
        long_trend_buffer_pct=backtest.DEFAULT_LONG_TREND_BUFFER_PCT,
        use_new_high_trigger=backtest.DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER,
        use_new_low_trigger=backtest.DEFAULT_USE_FIFTY_TWO_WEEK_LOW_TRIGGER,
        min_holding_days=backtest.DEFAULT_MIN_HOLDING_DAYS,
        bond_annual_yield=backtest.DEFAULT_BOND_ANNUAL_YIELD,
        use_sell_noise_filter=True,
        use_daily_band_confirmation=backtest.DEFAULT_SELL_NOISE_USE_DAILY_BAND,
        sell_noise_filter_drop_pct=backtest.DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
        # 참여율 계산은 holding_curve(보유 여부)만 쓰므로 수수료 값 자체는 결과에
        # 영향이 없지만, simulate()의 새 파라미터 이름과는 맞춰둬야 한다.
        buy_fee_pct=(
            backtest.DEFAULT_BUY_FEE_PCT if gold_price_basis == config.GOLD_PRICE_BASIS_KRX else 0.0
        ),
        sell_fee_pct=(
            backtest.DEFAULT_SELL_FEE_PCT if gold_price_basis == config.GOLD_PRICE_BASIS_KRX else 0.0
        ),
        daily_holding_fee_pct=(
            backtest.DEFAULT_DAILY_HOLDING_FEE_PCT
            if gold_price_basis == config.GOLD_PRICE_BASIS_KRX
            else 0.0
        ),
    )


def compute_threshold_holding(signals_df: pd.DataFrame, **backtest_kwargs) -> dict[str, pd.Series]:
    """Run backtest.simulate() once per (buy,sell) green_count pair in
    THRESHOLD_PAIRS, holding every other parameter in `backtest_kwargs`
    fixed, and return {"6/0": holding_curve, "5/1": ..., "4/2": ...}."""
    holding_by_threshold = {}
    for buy_gc, sell_gc in THRESHOLD_PAIRS:
        result = backtest.simulate(
            signals_df, buy_green_count=buy_gc, sell_green_count=sell_gc, **backtest_kwargs
        )
        holding_by_threshold[f"{buy_gc}/{sell_gc}"] = result["holding_curve"]
    return holding_by_threshold


def participation_rates(dates, held_by_threshold: dict[str, pd.Series]) -> dict[str, dict[str, float]]:
    """{threshold: {regime_label: rate_pct}} — of the days in `dates` that
    fall in each of the 5 REGIME_LABELS, the % on which the given threshold's
    signal was holding, over the FULL supplied date range (never filtered to
    a chart's current zoom — see pages/1_백테스트.py for the zoom-filtered
    version of this same calculation)."""
    labels = classify_regime_5way(dates)
    out = {}
    for key, holding_curve in held_by_threshold.items():
        held = holding_curve.reindex(dates).fillna(False).astype(int).to_numpy()
        sums = dict.fromkeys(REGIME_LABELS, 0)
        counts = dict.fromkeys(REGIME_LABELS, 0)
        for lbl, h in zip(labels, held):
            counts[lbl] += 1
            sums[lbl] += h
        out[key] = {
            lbl: (sums[lbl] / counts[lbl] * 100.0 if counts[lbl] else None)
            for lbl in REGIME_LABELS
        }
    return out


def participation_rate_range(
    rates: dict[str, dict[str, float]], regime_label: str
) -> tuple[float, float]:
    """min/max of one regime's participation rate across every threshold in
    `rates` (as returned by participation_rates) — this is exactly the
    "[상승장_참여율_최소]~[상승장_참여율_최대]%" the dashboard's 핵심 요약 text
    binds to, so a change to the regime table or the strategy's parameters
    flows straight through to that text with no manual edit."""
    values = [r[regime_label] for r in rates.values() if r[regime_label] is not None]
    return (min(values), max(values))

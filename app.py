"""Streamlit dashboard: gold price correlation table.

For today's date, reads the pre-computed data/latest.json (refreshed daily at
07:00 KST by the GitHub Actions workflow in
.github/workflows/update_dashboard_data.yml) so the page loads instantly.
For any other selected date, computes the table live as of that date
(requires network access and FRED_API_KEY).
"""

import json
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from gold_dashboard import config, signals, timeseries
from gold_dashboard.timeutil import today_kst

DATA_PATH = Path(__file__).resolve().parent / "data" / "latest.json"
EARLIEST_DATE = date(1990, 1, 1)

# dataviz reference palette: the indicator gets a sequential blue ramp (darkest =
# its own daily close, progressively lighter for the 7/30/90-calendar-day SMAs
# so shorter windows read closer to the raw series), gold price gets categorical
# slot 2 (orange) so it never reads as "one more shade of the same family" on
# its own (right-hand) axis, and reference threshold lines use a neutral gray.
CHART_INDICATOR_COLOR = "#256abf"
CHART_SMA_COLORS = {7: "#5598e7", 30: "#86b6ef", 90: "#b7d3f6"}
CHART_GOLD_COLOR = "#eb6834"
CHART_THRESHOLD_COLOR = "#8a8a86"
# One consistent shading treatment for "buy signal active" across every indicator
# (identity is already carried by line color; the shade means the same thing everywhere).
CHART_SIGNAL_SHADE_COLOR = "#e34948"
CHART_SIGNAL_SHADE_OPACITY = 0.16
# Fixed, not user-configurable here — deliberately independent of the 유효성
# 검증 (backtest) page's own adjustable analysis period, so changing that
# page's setting never affects these charts.
CHART_YEARS = 10


def _boolean_series_to_ranges(flag: pd.Series) -> list[tuple]:
    """Contiguous [start, end] date ranges where `flag` is True (inclusive of
    both ends). Used to turn a per-day gold-friendly boolean series into
    background shading bands (Altair's equivalent of plotly's add_vrect —
    a rect with only x/x2 encoded spans the chart's full height)."""
    if flag.empty:
        return []
    idx = flag.index
    values = flag.to_numpy()
    ranges = []
    start = None
    for i, is_true in enumerate(values):
        if is_true and start is None:
            start = idx[i]
        elif not is_true and start is not None:
            ranges.append((start, idx[i - 1]))
            start = None
    if start is not None:
        ranges.append((start, idx[-1]))
    return ranges


@st.cache_data(ttl=3600, show_spinner="데이터를 불러오는 중입니다...")
def load_data(selected_date_iso: str, is_today: bool):
    if is_today and DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))

    from gold_dashboard.build_table import build

    as_of = None if is_today else date.fromisoformat(selected_date_iso)
    return build(as_of=as_of)


@st.cache_data(ttl=86400, show_spinner=f"{CHART_YEARS}년치 시계열 데이터를 불러오는 중입니다...")
def load_chart_data(indicator_key: str, as_of_iso: str) -> dict:
    return timeseries.build_indicator_chart_data(
        indicator_key, as_of=date.fromisoformat(as_of_iso), years=CHART_YEARS
    )


def render_indicator_chart(indicator_key: str, label: str, as_of_iso: str) -> None:
    try:
        chart_data = load_chart_data(indicator_key, as_of_iso)
    except Exception as exc:
        st.error(f"{label} 시계열을 불러오지 못했습니다: {exc}")
        return

    meta = config.INDICATOR_META[indicator_key]
    unit_suffix = f" ({meta['unit']})" if meta["unit"] else ""
    indicator_series_name = f"{label} 종가{unit_suffix}"
    gold_series_name = "금(GC=F) 가격 ($)"

    left_rows = [
        {"date": d, "value": v, "series": indicator_series_name}
        for d, v in chart_data["indicator"].items()
    ]
    if chart_data["kind"] == "ma":
        window_labels = {7: "7일 이동평균", 30: "30일 이동평균", 90: "90일 이동평균"}
        for window in (7, 30, 90):
            sma_series = chart_data["smas"][window]
            left_rows.extend(
                {"date": d, "value": v, "series": window_labels[window]}
                for d, v in sma_series.items()
            )
        left_domain = [indicator_series_name, "7일 이동평균", "30일 이동평균", "90일 이동평균"]
        left_range = [CHART_INDICATOR_COLOR, CHART_SMA_COLORS[7], CHART_SMA_COLORS[30], CHART_SMA_COLORS[90]]
    else:
        left_domain = [indicator_series_name]
        left_range = [CHART_INDICATOR_COLOR]

    combined_domain = left_domain + [gold_series_name]
    combined_range = left_range + [CHART_GOLD_COLOR]
    color_scale = alt.Scale(domain=combined_domain, range=combined_range)

    left_df = pd.DataFrame(left_rows)
    left_chart = (
        alt.Chart(left_df)
        .mark_line(strokeWidth=2)
        .encode(
            x=alt.X("date:T", axis=alt.Axis(title=None, format="%Y", tickCount="year")),
            y=alt.Y(
                "value:Q",
                title=indicator_series_name,
                axis=alt.Axis(titleColor=CHART_INDICATOR_COLOR),
            ),
            color=alt.Color("series:N", scale=color_scale, legend=alt.Legend(title=None)),
            strokeDash=alt.StrokeDash(
                "series:N",
                scale=alt.Scale(domain=left_domain, range=[[]] + [[4, 2]] * (len(left_domain) - 1)),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("date:T", title="날짜"),
                alt.Tooltip("series:N", title="시리즈"),
                alt.Tooltip("value:Q", title="값", format=".2f"),
            ],
        )
    )

    # Buy-signal-active shading: only for indicators that actually feed a real
    # buy/sell trigger. real_rate/dxy use the green_count condition (all 3 MA
    # windows simultaneously gold-friendly, a stricter single-indicator view of
    # the same comparison green_count sums); gold/silver ratio uses the same
    # >= threshold as its own immediate-buy trigger (unrelated to any moving
    # average). WTI and VIX are reference-only — never used in any buy/sell
    # trigger — so they get no shading at all, regardless of what their own
    # chart might otherwise suggest. Both real cases call gold_dashboard/signals.py
    # — the same module build_table.py's highlighting and backtest.py's
    # green_count/ratio triggers use — so the shading can never silently
    # diverge from the actual signal definitions.
    if chart_data["kind"] == "ma":
        signal_flag = (
            signals.all_windows_gold_friendly_for(
                indicator_key, chart_data["indicator"], chart_data["smas"]
            )
            if indicator_key in config.GREEN_COUNT_SIGNAL_INDICATORS
            else None
        )
    else:
        signal_flag = signals.ratio_threshold_active(
            chart_data["indicator"], config.DEFAULT_GS_RATIO_BUY_THRESHOLD, "ge"
        )

    shade_ranges = _boolean_series_to_ranges(signal_flag) if signal_flag is not None else []
    layers = []
    if shade_ranges:
        shade_df = pd.DataFrame(
            {
                "start": [r[0] for r in shade_ranges],
                "end": [r[1] + timedelta(days=1) for r in shade_ranges],
            }
        )
        layers.append(
            alt.Chart(shade_df)
            .mark_rect(color=CHART_SIGNAL_SHADE_COLOR, opacity=CHART_SIGNAL_SHADE_OPACITY)
            .encode(x="start:T", x2="end:T")
        )

    layers.append(left_chart)
    if chart_data["kind"] == "ratio":
        threshold_df = pd.DataFrame(
            {
                "y": [80, config.DEFAULT_GS_RATIO_SELL_THRESHOLD],
                "label": ["학술적 관행 임계값 80", f"매도신호 임계값 {config.DEFAULT_GS_RATIO_SELL_THRESHOLD:g}"],
            }
        )
        layers.append(
            alt.Chart(threshold_df)
            .mark_rule(strokeDash=[4, 4], strokeWidth=1.5, color=CHART_THRESHOLD_COLOR)
            .encode(y="y:Q", tooltip=[alt.Tooltip("label:N", title="기준선")])
        )

    gold_df = pd.DataFrame(
        {"date": d, "value": v, "series": gold_series_name} for d, v in chart_data["gold"].items()
    )
    gold_chart = (
        alt.Chart(gold_df)
        .mark_line(strokeWidth=2)
        .encode(
            x=alt.X("date:T", axis=alt.Axis(title=None, format="%Y", tickCount="year")),
            y=alt.Y(
                "value:Q",
                title=gold_series_name,
                axis=alt.Axis(orient="right", titleColor=CHART_GOLD_COLOR),
            ),
            color=alt.Color("series:N", scale=color_scale, legend=alt.Legend(title=None)),
            tooltip=[
                alt.Tooltip("date:T", title="날짜"),
                alt.Tooltip("value:Q", title="금 가격", format="$.2f"),
            ],
        )
    )

    combined_chart = (
        alt.layer(alt.layer(*layers), gold_chart)
        .resolve_scale(y="independent")
        .properties(height=650, title=f"{label} vs 금 가격 — 최근 {CHART_YEARS}년")
    )
    st.altair_chart(combined_chart, use_container_width=True)

    if chart_data["kind"] == "ratio":
        st.caption(
            f"🔵 {label}(왼쪽 축) · 🟠 금 가격(오른쪽 축, $) · 회색 점선 = 기준선 "
            f"(학술적 관행값 80, 유효성 검증 페이지의 매도신호 임계값 기본값 "
            f"{config.DEFAULT_GS_RATIO_SELL_THRESHOLD:g}) — 절대적 기준은 아님"
        )
        st.caption(
            f"🟥 음영 구간 = 해당 지표 기준 매수신호 활성 구간 "
            f"(금/은비율 ≥ {config.DEFAULT_GS_RATIO_BUY_THRESHOLD:g}, 백테스트 매수 임계값의 기본값 기준)"
        )
    else:
        st.caption(
            f"🔵 진한 파랑 = {label} 종가, 옅어질수록 5→30→60일 이동평균(왼쪽 축) · "
            "🟠 금 가격(오른쪽 축, $)"
        )
        if indicator_key in config.GREEN_COUNT_SIGNAL_INDICATORS:
            st.caption(
                "🟥 음영 구간 = 해당 지표 기준 매수신호 활성 구간 (7·30·90일 이평선 3개 모두 동시에 "
                "만족하는 날). 실제 매매 신호의 green_count는 이 조건을 실질금리·달러인덱스 두 지표에서 "
                "합산하므로, 이 지표 하나만으로 3개를 모두 만족하지 못해도 다른 지표 쪽에서 green_count≥5가 "
                "채워져 매수가 발생할 수 있습니다."
            )
        else:
            st.caption(
                f"{label}는 실제 매수·매도 신호에 사용되지 않는 참고용 지표라 음영 표시가 없습니다."
            )


_KEY_TAKEAWAYS_PERIODS = [
    # (기간, 실질금리 R² %, 달러인덱스 R² %, 우세 팩터)
    ("2005~2006", 75, 64, "둘 다 강함"),
    ("2007~2008", 79, 51, "둘 다 강함"),
    ("2009~2010", 35, 53, "달러"),
    ("2011~2012", 60, 8, "실질금리"),
    ("2013~2014", 60, 49, "둘 다 강함"),
    ("2015~2016", 58, 0, "실질금리"),
    ("2017~2018", 3, 75, "달러"),
    ("2019~2020", 82, 5, "실질금리"),
    ("2021~2022", 10, 24, "⚠️ 둘 다 약함"),
    ("2023~2024", 5, 59, "달러"),
    ("2025~2026", 11, 66, "달러"),
]


def _render_key_takeaways() -> None:
    """Fixed "핵심 요약" callout pinned above the correlation table, plus a
    collapsed-by-default expander with the period-by-period R² backing it up.
    Static content (not derived from data/latest.json) — see World Gold
    Council / Erb & Harvey / RBC Wealth Management sourcing in the caption."""
    st.markdown(
        """
<div style="background-color:#fff8e1;border-left:6px solid #f5a623;
border-radius:8px;padding:16px 20px;margin-bottom:4px">
<div style="font-size:17px;font-weight:600;margin-bottom:8px">💡 핵심 요약</div>
<p style="margin:0 0 10px 0;line-height:1.6">
실질금리는 정책 개입기(QE·팬데믹)에, 달러인덱스는 위기 연쇄기·최근 구간에 강하게 작동합니다.
2005년 이후 11개 구간 중 10개 구간에서 최소 한 팩터는 강한 상관성을 보였습니다 —
둘을 같이 보면 대부분의 시기를 커버할 수 있습니다.
</p>
<p style="margin:0 0 12px 0;line-height:1.6">
다만 최근에는 각국 중앙은행의 금 수요에 의해 실질금리 및 달러인덱스의 상관성이 유효하지 않음을
보임 — 2021년 이후 실질금리는 계속 약하게 나타나고 있습니다. 이는 중국·폴란드·인도·터키 등
신흥국 중앙은행(미국 연준 아님)이 주도하는 매입으로, 2022년 러시아 외환보유고 동결을 계기로
촉발된 탈달러화(de-dollarization) 흐름과 맞물려 있습니다.
</p>
<div style="background-color:#fdecea;border-left:4px solid #d32f2f;
border-radius:6px;padding:10px 14px;font-weight:600;color:#611a15;line-height:1.6">
⚠️ 본 대시보드는 역사적 상관성이 확인된 실시간 수치를 기반으로 하기 때문에, 각국 중앙은행의
매입 정도는 반영하지 못하는 한계가 있습니다. 이는 별도 확인이 꼭 필요합니다.
<span style="font-weight:400">(중앙은행 매입은 분기 단위로만 발표되어 실시간 반영 불가)</span>
</div>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.expander("📊 구간별 상관성 근거 보기", expanded=False):

        def header_cell(text: str) -> str:
            return (
                f'<th style="padding:8px 12px;border:1px solid #ddd;background:#f5f5f5;'
                f'text-align:left;white-space:nowrap">{text}</th>'
            )

        def r2_cell(value: int) -> str:
            text = f"{value}%"
            if value >= 50:
                text = f"<strong>{text}</strong>"
            return f'<td style="padding:8px 12px;border:1px solid #ddd">{text}</td>'

        def factor_cell(text: str) -> str:
            return f'<td style="padding:8px 12px;border:1px solid #ddd">{text}</td>'

        header_row = (
            header_cell("구간") + header_cell("실질금리 R²") + header_cell("달러인덱스 R²") + header_cell("우세 팩터")
        )
        rows_html = [f"<tr>{header_row}</tr>"]
        for period, real_rate_r2, dxy_r2, factor in _KEY_TAKEAWAYS_PERIODS:
            rows_html.append(
                f"<tr>{header_cell(period)}{r2_cell(real_rate_r2)}{r2_cell(dxy_r2)}{factor_cell(factor)}</tr>"
            )
        table_html = (
            '<table style="border-collapse:collapse;width:100%;font-size:14px">' + "".join(rows_html) + "</table>"
        )
        st.markdown(table_html, unsafe_allow_html=True)
        st.caption(
            "2년 단위 기준 · 출처: Erb&Harvey(2013,2024), RBC Wealth Management(2025), "
            "World Gold Council · 원자료: FRED(REAINTRATREARAT10Y, TWEXBGSMTH)"
        )

        # A nested st.expander() isn't allowed inside this outer one (Streamlit
        # raises on expander-in-expander), so this is a button-driven toggle
        # instead — collapsed by default, flips a session_state flag on click.
        st.session_state.setdefault("dash_show_calc_method", False)
        if st.button("📐 계산 방법 보기", key="dash_calc_method_btn"):
            st.session_state["dash_show_calc_method"] = not st.session_state["dash_show_calc_method"]
        if st.session_state["dash_show_calc_method"]:
            st.markdown(
                "- **데이터**: 실질금리(10년물 TIPS 실질수익률, FRED `REAINTRATREARAT10Y`), "
                "달러인덱스(`TWEXBGSMTH`), 금 가격(월간) — 모두 월별 종가 기준\n"
                "- R²는 각 2년 구간 내 월별 데이터로 피어슨 상관계수(r)를 구한 뒤 제곱한 값 "
                "(R² = r²)\n"
                "- \"실질금리 R²\"는 실질금리 레벨과 금값 레벨의 상관관계, \"달러인덱스 R²\"는 "
                "달러인덱스 레벨과 금값 레벨의 상관관계를 각각 계산\n"
                "- R²가 50% 이상이면 해당 팩터를 \"강함\"으로, 두 팩터 모두 50% 미만이면 "
                "\"둘 다 약함\"으로 표시\n"
                "- 표본 수(n)는 구간당 약 24개월(2006년 이전 달러인덱스는 데이터 시작 시점 "
                "제약으로 n이 더 적음)"
            )


def render_dashboard() -> None:
    st.title("금(Gold) 상관관계 대시보드")
    _render_key_takeaways()

    # Shared with the 유효성 검증 (backtest) page via config.GOLD_PRICE_BASIS_STATE_KEY
    # — but NOT via that key's own widget binding: st.navigation resets a
    # widget's session_state entry back to its default the moment that exact
    # widget isn't instantiated in a run (i.e. the instant you navigate away
    # from the page that renders it), so a `key=` shared across two different
    # pages' widgets does NOT survive navigation between them (verified
    # directly against this Streamlit version). The fix is to keep the shared
    # choice in that plain session_state entry (which does survive navigation)
    # and seed each page's own, page-local widget from it via `index=`,
    # writing the widget's result straight back after every rerun.
    _gold_basis_options = [config.GOLD_PRICE_BASIS_INTL, config.GOLD_PRICE_BASIS_KRX]
    st.session_state.setdefault(config.GOLD_PRICE_BASIS_STATE_KEY, config.GOLD_PRICE_BASIS_DEFAULT)
    gold_price_basis = st.radio(
        "금 가격 기준",
        options=_gold_basis_options,
        format_func=lambda v: config.GOLD_PRICE_BASIS_LABELS[v],
        index=_gold_basis_options.index(st.session_state[config.GOLD_PRICE_BASIS_STATE_KEY]),
        key="_gold_price_basis_widget_dashboard",
        horizontal=True,
        help="유효성 검증(백테스트) 페이지 전체가 이 기준으로 계산됩니다. 이 대시보드 페이지의 "
        "표·그래프 자체는 이 설정과 무관하게 항상 국제 시세 기준입니다.",
    )
    st.session_state[config.GOLD_PRICE_BASIS_STATE_KEY] = gold_price_basis
    if gold_price_basis == config.GOLD_PRICE_BASIS_KRX:
        st.caption(
            "ℹ️ 아래 표의 실질금리·달러인덱스·금/은비율·WTI·VIX는 국제 시세 기준 참고 지표이며 "
            "KRX 금현물과 직접 대응되지 않습니다. 이 설정은 유효성 검증 페이지의 백테스트에만 "
            "적용됩니다."
        )

    today = today_kst()
    date_col, refresh_col = st.columns([4, 1])
    with date_col:
        selected_date = st.date_input(
            "기준일 선택",
            value=today,
            min_value=EARLIEST_DATE,
            max_value=today,
            help="이 날짜(또는 그 이전 최근 거래일)의 종가를 기준으로 표를 계산합니다.",
        )
    with refresh_col:
        st.write("")  # vertical alignment spacer next to the date input
        st.write("")
        force_live = st.button("새로고침", use_container_width=True)

    is_today = selected_date == today
    if force_live:
        st.cache_data.clear()

    try:
        data = load_data(selected_date.isoformat(), is_today)
    except Exception as exc:
        st.error(f"데이터를 불러오지 못했습니다: {exc}")
        st.stop()

    close_row_label = "전일종가" if is_today else "종가"

    if is_today:
        st.caption(
            f"기준일(전일 미국장 마감 종가): **{data['as_of']}**  ·  생성시각(KST): {data['generated_at']}"
        )
        st.caption(
            "⚠️ 실시간 시세가 아닙니다. 원자재·금리 데이터는 대부분 일봉(전일 확정 종가) 기준이며, "
            "이 표는 매일 아침 7시(KST)에 자동 갱신됩니다."
        )
    else:
        st.caption(
            f"선택한 기준일: **{selected_date}** → 실제 반영된 거래일: **{data['as_of']}** "
            "(주말·휴장일이면 직전 거래일 종가가 표시됩니다)"
        )
        st.caption("ℹ️ 과거 기준일은 매일 자동 갱신되는 캐시가 아니라 그때그때 실시간으로 계산됩니다.")
    st.caption(
        "🟢 옅은 녹색 배경 = 실제 매수·매도 신호에 쓰이는 지표에서, 그 신호가 현재 금값에 "
        "우호적인 방향인 셀입니다. 역방향 지표(실질금리·달러인덱스)는 종가가 이평선 아래일 때 "
        "초록색으로 표시되며(green_count에 사용), 금/은비율은 이평선과 무관하게 종가가 "
        f"{config.DEFAULT_GS_RATIO_BUY_THRESHOLD:g} 이상일 때만 세 이평선 행 모두 초록색으로 "
        "표시됩니다(백테스트의 매수 임계값과 동일). WTI·VIX는 참고용 지표라 실제 매수·매도 "
        "신호에 쓰이지 않으므로 이평선을 돌파해도 녹색으로 표시되지 않습니다. "
        "셀에 보이는 '상향 돌파/이평선 아래' 문구는 하이라이트 색과 무관한, 종가와 이평선의 기술적 위치입니다. "
        "각 셀 하단의 작은 글씨는 그 상향 돌파가 며칠째 지속 중인지를 나타내는 보조 정보입니다."
    )

    indicator_order = data["indicator_order"]
    indicators = data["indicators"]

    def header_cell(text: str, tooltip: str | None = None) -> str:
        title_attr = f' title="{tooltip}"' if tooltip else ""
        return (
            f'<th style="padding:8px 12px;border:1px solid #ddd;background:#f5f5f5;'
            f'text-align:left;white-space:nowrap"{title_attr}>{text}</th>'
        )

    def data_cell(text: str, highlight: bool = False) -> str:
        bg = "background-color: rgba(76,175,80,0.28);" if highlight else ""
        return f'<td style="padding:8px 12px;border:1px solid #ddd;{bg}">{text}</td>'

    def ma_cell(sma: dict) -> str:
        """MA row cell: the MA value vs. close (primary) with the breakout-streak
        day count shown only as a small supplementary badge, not the headline.
        Highlighting reflects whether the signal is gold-friendly given the
        indicator's correlation direction, not simply "close above its own MA"."""
        bg = "background-color: rgba(76,175,80,0.28);" if sma["gold_friendly"] else ""
        badge = (
            f'<div style="font-size:11px;color:#5a5a5a;margin-top:2px">{sma["streak_display"]}</div>'
            if sma["streak_display"]
            else ""
        )
        return f'<td style="padding:8px 12px;border:1px solid #ddd;{bg}">{sma["display"]}{badge}</td>'

    rows_html = []

    header_row = header_cell("구성") + "".join(
        header_cell(indicators[k]["label"], tooltip=indicators[k]["source"]) for k in indicator_order
    )
    rows_html.append(f"<tr>{header_row}</tr>")

    for row_name in data["row_order"]:
        cells = "".join(data_cell(data["static_rows"][row_name][k]) for k in indicator_order)
        rows_html.append(f"<tr>{header_cell(row_name)}{cells}</tr>")

    for window in data["ma_windows"]:
        cells = "".join(ma_cell(indicators[k]["sma"][str(window)]) for k in indicator_order)
        rows_html.append(f"<tr>{header_cell(f'{window}일선')}{cells}</tr>")

    close_cells = "".join(data_cell(indicators[k]["prev_close"]["display"]) for k in indicator_order)
    rows_html.append(f"<tr>{header_cell(close_row_label)}{close_cells}</tr>")

    table_html = (
        '<table style="border-collapse:collapse;width:100%;font-size:14px">'
        + "".join(rows_html)
        + "</table>"
    )
    st.markdown(table_html, unsafe_allow_html=True)

    st.markdown("#### 지표별 시계열 그래프")
    st.caption(
        f"버튼을 누른 지표만 그 시점에 최근 {CHART_YEARS}년치 데이터를 받아와 그립니다 — 누르기 "
        "전에는 어떤 지표도 미리 계산하지 않습니다."
    )

    chart_cols = st.columns(len(indicator_order))
    for col, key in zip(chart_cols, indicator_order):
        state_key = f"show_chart_{key}"
        st.session_state.setdefault(state_key, False)
        with col:
            button_label = (
                f"📉 {indicators[key]['label']} 그래프 숨기기"
                if st.session_state[state_key]
                else f"📈 {indicators[key]['label']} 그래프 보기"
            )
            if st.button(button_label, key=f"btn_{state_key}", use_container_width=True):
                st.session_state[state_key] = not st.session_state[state_key]
                st.rerun()

    for key in indicator_order:
        if st.session_state.get(f"show_chart_{key}", False):
            render_indicator_chart(key, indicators[key]["label"], today.isoformat())

    st.markdown("#### 지표별 참고 출처")
    for k in indicator_order:
        st.caption(f"**{indicators[k]['label']}** — {data['footnotes'][k]}")


def main() -> None:
    # Centralized here (not per-page) because st.set_page_config may only be
    # called once per app run, and st.navigation below replaces the classic
    # pages/-folder auto-discovery that used to let each page set its own.
    st.set_page_config(page_title="금(Gold) 상관관계 대시보드", layout="wide")

    # url_path values are pinned to the exact slugs Streamlit's classic
    # pages/-folder discovery used to auto-derive from these filenames (root
    # "" for app.py, "백테스트" for pages/1_백테스트.py) so existing shared
    # links keep resolving even though the sidebar labels below now differ
    # from the filenames.
    pages = st.navigation(
        [
            st.Page(render_dashboard, title="대시보드", url_path="", default=True),
            st.Page("pages/1_백테스트.py", title="유효성 검증", url_path="백테스트"),
        ]
    )
    pages.run()


main()

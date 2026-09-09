"""Backtest page: real-rate/DXY MA breakout signal + gold/silver-ratio threshold
strategy vs. a same-period Buy & Hold benchmark."""

from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

from gold_dashboard import backtest

st.set_page_config(page_title="백테스트 — 금(Gold) 상관관계 대시보드", layout="wide")

# dataviz reference palette: strategy/benchmark use categorical slots 2-3 (orange/aqua)
# so they stay visually distinct from the buy/sell markers below, which reuse the
# conventional blue=buy / red=sell pair (slots 1 and 8).
STRATEGY_COLOR = "#eb6834"
BH_COLOR = "#1baf7a"
BUY_COLOR = "#2a78d6"
SELL_COLOR = "#e34948"
STRATEGY_LABEL = "신호전략"
BH_LABEL = "Buy & Hold"

st.title("신호 기반 매매 전략 백테스트")
st.caption(
    "실질금리·달러인덱스의 이동평균 돌파 신호와 금/은비율 임계값을 결합한 매수·매도 규칙을, "
    "동일 시작일의 Buy & Hold와 비교합니다."
)

with st.expander("전략 규칙 보기"):
    st.markdown(
        f"""
- **매수** (미보유 상태일 때만): `green_count ≥ {backtest.BUY_GREEN_COUNT}`
  (실질금리·달러인덱스 × 5/30/60일 이평선, 총 6개 셀 중 금값에 우호적인 셀 수) — 아래 매수
  지연일수만큼 기다린 뒤 체결. **또는** 금/은비율 ≥ (아래에서 설정한 값) — 지연 없이 **당일
  즉시 매수**. **또는** (아래 체크박스를 켠 경우) **금 신고가 갱신** — 이것도 지연 없이 당일
  즉시 매수. 셋 중 아무 조건이나 먼저 만족하면 매수합니다
- **매도** (보유 상태일 때만): `green_count = {backtest.SELL_GREEN_COUNT}` — 아래 매도
  지연일수만큼 기다린 뒤 체결. **또는** 금/은비율 ≤ (아래에서 설정한 값) — 지연 없이 **당일
  즉시 매도**
- 지연이 설정된 green_count 신호는 그 시점 이후 첫 거래일 **종가**로 체결됩니다(지연 기간 중
  조건 재확인 없이 그대로 체결 — 지연일수 0이면 신호 당일 종가에 즉시 체결). 금/은비율과
  신고가 갱신 신호는 지연 설정과 무관하게 항상 신호 당일 종가에 체결되며, 아직 대기 중인
  green_count 지연 주문이 있어도 먼저 체결됩니다
- 분석 기간: 오늘 기준 최근 **{backtest.BACKTEST_YEARS}년** (이동평균 계산용으로 그 이전
  {backtest.BUFFER_DAYS}캘린더일치 데이터를 추가로 사용)
        """
    )

st.subheader("매수·매도 조건 설정")
st.caption(
    "금/은비율 임계값과 신고가 갱신 조건은 지연 없이 항상 신호 당일 즉시 체결됩니다. "
    "지연일수는 green_count 신호에만 적용됩니다."
)
ratio_col1, ratio_col2, ratio_col3 = st.columns(3)
with ratio_col1:
    buy_ratio = st.number_input(
        "매수 금/은비율 임계값 (이상, 즉시 매수)",
        min_value=1.0, max_value=200.0, value=float(backtest.BUY_RATIO), step=1.0,
        help="금/은비율이 이 값 이상이면 그날 즉시 매수합니다 (지연 미적용).",
    )
with ratio_col2:
    sell_ratio = st.number_input(
        "매도 금/은비율 임계값 (이하, 즉시 매도)",
        min_value=1.0, max_value=200.0, value=float(backtest.SELL_RATIO), step=1.0,
        help="금/은비율이 이 값 이하이면 그날 즉시 매도합니다 (지연 미적용).",
    )
with ratio_col3:
    use_new_high_buy = st.checkbox(
        "신고가 갱신 시 매수 조건 추가",
        value=False,
        help="켜면 금(GC=F) 종가가 분석 기간 내 최고가를 새로 경신하는 날 그 즉시 매수합니다 "
        "(지연 미적용, green_count/금은비율 조건과는 OR로 결합).",
    )
if sell_ratio >= buy_ratio:
    st.warning("매도 임계값이 매수 임계값보다 크거나 같습니다. 매수 즉시 매도 조건도 함께 만족해 거의 바로 청산될 수 있습니다.")

delay_col1, delay_col2 = st.columns(2)
with delay_col1:
    entry_delay_days = st.number_input(
        "매수 지연일수 (일, green_count 신호에만 적용)", min_value=0, max_value=180, value=0, step=1,
        help="예: 30을 입력하면 'green_count 신호 발생 1개월 후 매수'를 시뮬레이션합니다. "
        "금/은비율·신고가 갱신 매수에는 적용되지 않습니다.",
    )
    st.caption(f"≈ {entry_delay_days / 30:.1f}개월 후 매수 (금/은비율·신고가 갱신 매수는 항상 즉시 체결)")
with delay_col2:
    exit_delay_days = st.number_input(
        "매도 지연일수 (일, green_count 신호에만 적용)", min_value=0, max_value=180, value=0, step=1,
        help="예: 30을 입력하면 'green_count 신호 발생 1개월 후 매도'를 시뮬레이션합니다. "
        "금/은비율 매도에는 적용되지 않습니다.",
    )
    st.caption(f"≈ {exit_delay_days / 30:.1f}개월 후 매도 (금/은비율 매도는 항상 즉시 체결)")

refresh_clicked = st.button("데이터 새로고침 (오늘 기준으로 다시 수집)")


@st.cache_data(ttl=3600, show_spinner="7년치 데이터를 내려받는 중입니다...")
def load_signals(as_of_iso: str) -> pd.DataFrame:
    return backtest.prepare_signals(as_of=date.fromisoformat(as_of_iso))


if refresh_clicked:
    st.cache_data.clear()

try:
    signals = load_signals(date.today().isoformat())
    result = backtest.simulate(
        signals,
        entry_delay_days=int(entry_delay_days),
        exit_delay_days=int(exit_delay_days),
        use_new_high_buy=use_new_high_buy,
        buy_ratio=float(buy_ratio),
        sell_ratio=float(sell_ratio),
    )
except Exception as exc:
    st.error(f"백테스트를 실행하지 못했습니다: {exc}")
    st.stop()

m = result["metrics"]
equity = result["equity_curve"]
bh_equity = result["bh_equity_curve"]
yearly = result["yearly_returns"]
trades = result["trades"]

start_date = equity.index[0].date()
end_date = equity.index[-1].date()
st.caption(
    f"분석 기간: **{start_date} ~ {end_date}** "
    "(신호전략과 Buy & Hold 모두 이 기간의 첫날에 시작 — 동일 시작일 비교)"
)

# ---- 1. 누적수익률 라인차트 (+ 매수/매도 시점 마커) ----
st.subheader("누적수익률")
cum_df = pd.DataFrame(
    {
        "date": equity.index,
        STRATEGY_LABEL: equity.values - 1.0,
        BH_LABEL: bh_equity.reindex(equity.index).values - 1.0,
    }
).melt("date", var_name="series", value_name="return")

line_chart = (
    alt.Chart(cum_df)
    .mark_line(strokeWidth=2)
    .encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("return:Q", title="누적수익률", axis=alt.Axis(format="%")),
        color=alt.Color(
            "series:N",
            title=None,
            scale=alt.Scale(domain=[STRATEGY_LABEL, BH_LABEL], range=[STRATEGY_COLOR, BH_COLOR]),
        ),
        tooltip=[
            alt.Tooltip("date:T", title="날짜"),
            alt.Tooltip("series:N", title="전략"),
            alt.Tooltip("return:Q", title="누적수익률", format=".1%"),
        ],
    )
)

marker_rows = []
for t in trades:
    marker_rows.append(
        {
            "date": t["entry_date"],
            "구분": "매수",
            "return": float(equity.loc[t["entry_date"]]) - 1.0,
            "가격": round(t["entry_price"], 2),
            "사유": t["entry_reason"] or "-",
        }
    )
    if not t["open"]:
        marker_rows.append(
            {
                "date": t["exit_date"],
                "구분": "매도",
                "return": float(equity.loc[t["exit_date"]]) - 1.0,
                "가격": round(t["exit_price"], 2),
                "사유": t["exit_reason"] or "-",
            }
        )
marker_df = pd.DataFrame(marker_rows)

if not marker_df.empty:
    markers = (
        alt.Chart(marker_df)
        .mark_point(size=90, filled=True, opacity=0.9)
        .encode(
            x="date:T",
            y="return:Q",
            color=alt.Color(
                "구분:N", title=None, scale=alt.Scale(domain=["매수", "매도"], range=[BUY_COLOR, SELL_COLOR])
            ),
            shape=alt.Shape(
                "구분:N", scale=alt.Scale(domain=["매수", "매도"], range=["triangle-up", "triangle-down"])
            ),
            tooltip=[
                alt.Tooltip("date:T", title="날짜"),
                alt.Tooltip("구분:N", title="구분"),
                alt.Tooltip("가격:Q", title="체결가"),
                alt.Tooltip("return:Q", title="당시 누적수익률", format=".1%"),
                alt.Tooltip("사유:N", title="사유"),
            ],
        )
    )
    combined_chart = alt.layer(line_chart, markers).resolve_scale(color="independent", shape="independent")
else:
    combined_chart = line_chart

st.altair_chart(combined_chart.properties(height=380).interactive(), use_container_width=True)
st.caption("▲ 파란색 = 매수 시점, ▼ 빨간색 = 매도 시점 (거래 내역 표 참고)")

# ---- 2. 연환산수익률(CAGR) 비교 ----
st.subheader("연환산수익률(CAGR) 비교")
cagr_labels = [f"{STRATEGY_LABEL} (순수투자기간)", f"{BH_LABEL} (전체기간)"]
cagr_df = pd.DataFrame(
    {"series": cagr_labels, "cagr": [m["strategy_cagr"] or 0.0, m["bh_cagr"] or 0.0]}
)
cagr_chart = (
    alt.Chart(cagr_df)
    .mark_bar(size=70)
    .encode(
        x=alt.X("series:N", title=None, sort=None),
        y=alt.Y("cagr:Q", title="CAGR", axis=alt.Axis(format="%")),
        color=alt.Color(
            "series:N",
            legend=None,
            scale=alt.Scale(domain=cagr_labels, range=[STRATEGY_COLOR, BH_COLOR]),
        ),
        tooltip=[alt.Tooltip("series:N", title="전략"), alt.Tooltip("cagr:Q", title="CAGR", format=".2%")],
    )
    .properties(height=300)
)
st.altair_chart(cagr_chart, use_container_width=True)
if m["strategy_cagr"] is None:
    st.caption("ℹ️ 신호전략이 이 기간 동안 한 번도 매수 신호를 내지 않아 CAGR을 계산할 수 없습니다.")

# ---- 3. 연도별 연환산수익률 막대그래프 ----
st.subheader("연도별 연환산수익률")
yearly_long = yearly.melt(
    id_vars=["year", "days_span"],
    value_vars=["strategy_return_annualized", "bh_return_annualized"],
    var_name="series",
    value_name="return",
)
yearly_long["series"] = yearly_long["series"].map(
    {"strategy_return_annualized": STRATEGY_LABEL, "bh_return_annualized": BH_LABEL}
)
# raw (non-annualized) realized return for the tooltip, aligned to the same rows
raw_map = {}
for _, row in yearly.iterrows():
    raw_map[(row["year"], STRATEGY_LABEL)] = row["strategy_return"]
    raw_map[(row["year"], BH_LABEL)] = row["bh_return"]
yearly_long["raw_return"] = [raw_map[(y, s)] for y, s in zip(yearly_long["year"], yearly_long["series"])]

bar_chart = (
    alt.Chart(yearly_long)
    .mark_bar()
    .encode(
        x=alt.X("year:O", title=None),
        xOffset=alt.XOffset("series:N", sort=[STRATEGY_LABEL, BH_LABEL]),
        y=alt.Y("return:Q", title="연환산수익률", axis=alt.Axis(format="%")),
        color=alt.Color(
            "series:N",
            title=None,
            scale=alt.Scale(domain=[STRATEGY_LABEL, BH_LABEL], range=[STRATEGY_COLOR, BH_COLOR]),
        ),
        tooltip=[
            alt.Tooltip("year:O", title="연도"),
            alt.Tooltip("series:N", title="전략"),
            alt.Tooltip("return:Q", title="연환산수익률", format=".1%"),
            alt.Tooltip("raw_return:Q", title="해당 연도 실제 수익률", format=".1%"),
            alt.Tooltip("days_span:Q", title="해당 연도 일수"),
        ],
    )
    .properties(height=340)
)
st.altair_chart(bar_chart, use_container_width=True)
st.caption(
    f"{int(yearly['year'].iloc[0])}년과 {int(yearly['year'].iloc[-1])}년은 분석 기간에 걸친 "
    "부분연도이며, 그 부분 기간의 실제 수익률을 연 단위로 환산한 값입니다(마우스오버 시 실제 "
    "수익률 확인 가능). 신호전략이 그 해 내내 현금(미보유) 상태였다면 0%로 표시됩니다."
)

# ---- 4. 거래 내역 표 ----
st.subheader("거래 내역")
if trades:
    trade_rows = [
        {
            "매수일": t["entry_date"].date(),
            "매수가": round(t["entry_price"], 2),
            "매수 사유": t["entry_reason"] or "-",
            "매도일": t["exit_date"].date() if t["exit_date"] is not None else "미청산(보유 중)",
            "매도가": round(t["exit_price"], 2),
            "매도 사유": t["exit_reason"] or ("미청산" if t["open"] else "-"),
            "보유일수": t["hold_days"],
            "구간수익률": f"{t['period_return']:.2%}" + (" (평가)" if t["open"] else ""),
        }
        for t in trades
    ]
    st.dataframe(pd.DataFrame(trade_rows), use_container_width=True, hide_index=True)
    st.caption(
        "매수 사유/매도 사유는 신호가 처음 발생한 날 기준입니다. green_count/금은비율 매수는 "
        "매수 지연일수만큼 지난 뒤 체결되어 매수일이 신호 발생일과 다를 수 있지만, 신고가 갱신 "
        "매수는 항상 매수일 = 신호 발생일입니다."
    )
else:
    st.caption("이 기간 동안 매수 신호가 발생하지 않아 거래 내역이 없습니다.")

# ---- 5. 요약 지표 ----
st.subheader("요약 지표")
col1, col2, col3 = st.columns(3)
col1.metric("총 거래 횟수", f"{m['closed_trade_count']}회")
col1.metric("승률", f"{m['win_rate']:.1%}" if m["win_rate"] is not None else "-")
col2.metric(f"누적수익률 ({STRATEGY_LABEL})", f"{m['strategy_total_return']:.1%}")
col2.metric(f"누적수익률 ({BH_LABEL})", f"{m['bh_total_return']:.1%}")
col3.metric(
    f"CAGR ({STRATEGY_LABEL}, 순수투자기간)",
    f"{m['strategy_cagr']:.1%}" if m["strategy_cagr"] is not None else "-",
)
col3.metric(f"CAGR ({BH_LABEL}, 전체기간)", f"{m['bh_cagr']:.1%}")
st.metric("최대 낙폭 (MDD, 신호전략)", f"{m['max_drawdown']:.1%}")

if m["has_open_position"]:
    st.info(
        "현재 포지션을 보유 중입니다. 마지막 거래는 미청산 상태이며, 위 거래 내역·수익률에 "
        "표시된 값은 오늘 종가 기준 평가손익입니다."
    )

st.caption(
    "⚠️ 본 백테스트는 과거 데이터에 기반한 시뮬레이션 결과이며 미래 성과를 보장하지 않습니다. "
    "거래비용·세금·슬리피지는 반영되어 있지 않고, 표본 기간이 짧아 과최적화(overfitting) 위험이 "
    "있습니다."
)

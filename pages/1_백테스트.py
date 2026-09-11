"""Backtest page: real-rate/DXY MA breakout signal + gold/silver-ratio threshold
strategy vs. a same-period Buy & Hold benchmark."""

from datetime import date

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from gold_dashboard import backtest, config
from gold_dashboard import timeseries
from gold_dashboard.timeutil import today_kst

# Page config (title/layout) is centralized in app.py's main(), since
# st.navigation there replaces the classic pages/-folder auto-discovery this
# file used to rely on for its own page config, and st.set_page_config can
# only be called once per app run.

# dataviz reference palette: strategy/benchmark use categorical slots 2-3 (orange/aqua)
# so they stay visually distinct from the buy/sell markers below, which reuse the
# conventional blue=buy / red=sell pair (slots 1 and 8).
STRATEGY_COLOR = "#eb6834"
BH_COLOR = "#1baf7a"
BUY_COLOR = "#2a78d6"
SELL_COLOR = "#e34948"
# Third line (신호전략 + 기대수익률 포함) gets its own categorical slot (violet)
# distinct from both STRATEGY_COLOR and BH_COLOR — never red/pink, since a
# lighter tint of this is used for the non-holding stretch and red/pink would
# read as "loss" there. NONHOLDING is that lighter tint (mixed toward white),
# used only for the dashed non-holding segments of the hybrid line.
STRATEGY_HYBRID_COLOR = "#7b5ea8"
STRATEGY_HYBRID_NONHOLDING_COLOR = "#c9bfe0"
NONHOLDING_BAND_COLOR = "#9aa0a6"  # neutral gray background shading, not red/pink
STRATEGY_LABEL = "신호전략"
BH_LABEL = "Buy & Hold"

DEFAULTS = {
    "bt_years": backtest.BACKTEST_YEARS,
    "bt_buy_green_count": backtest.BUY_GREEN_COUNT,
    "bt_sell_green_count": backtest.SELL_GREEN_COUNT,
    "bt_buy_ratio": float(backtest.BUY_RATIO),
    "bt_sell_ratio": float(backtest.SELL_RATIO),
    "bt_use_reentry_trigger": False,
    "bt_long_trend_buffer_pct": backtest.DEFAULT_LONG_TREND_BUFFER_PCT,
    "bt_use_reentry_freq_limit": True,
    "bt_reentry_freq_limit_days": backtest.DEFAULT_REENTRY_FREQ_LIMIT_DAYS,
    "bt_entry_delay_days": 0,
    "bt_exit_delay_days": 0,
    "bt_min_holding_days": backtest.DEFAULT_MIN_HOLDING_DAYS,
    "bt_bond_yield_pct": backtest.DEFAULT_BOND_ANNUAL_YIELD * 100.0,
    "bt_use_sell_noise_filter": True,
    "bt_use_daily_band_confirmation": backtest.DEFAULT_SELL_NOISE_USE_DAILY_BAND,
    "bt_sell_noise_filter_drop_pct": backtest.DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
    "bt_krx_holding_fee_pct": backtest.DEFAULT_KRX_HOLDING_FEE_ANNUAL_PCT,
}
for _key, _default in DEFAULTS.items():
    st.session_state.setdefault(_key, _default)

st.title("신호 기반 매매 전략 백테스트")
st.caption(
    "실질금리·달러인덱스의 이동평균 돌파 신호와 금/은비율 임계값을 결합한 매수·매도 규칙을, "
    "동일 시작일의 Buy & Hold와 비교합니다."
)

# Shared with the main dashboard page via config.GOLD_PRICE_BASIS_STATE_KEY —
# but NOT via that key's own widget binding: st.navigation resets a widget's
# session_state entry back to its default the instant that exact widget isn't
# instantiated in a run (i.e. the moment you navigate to a different page), so
# a `key=` shared across two pages' widgets does NOT survive navigation
# between them (verified directly against this Streamlit version). The fix is
# to keep the shared choice in that plain session_state entry (which does
# survive navigation) and seed this page's own, page-local widget from it via
# `index=`, writing the widget's result straight back after every rerun.
# Deliberately NOT part of DEFAULTS above: it's a data-source choice, not a
# backtest tuning parameter, so "기본값으로 초기화" leaves it untouched.
_gold_basis_options = [config.GOLD_PRICE_BASIS_INTL, config.GOLD_PRICE_BASIS_KRX]
st.session_state.setdefault(config.GOLD_PRICE_BASIS_STATE_KEY, config.GOLD_PRICE_BASIS_DEFAULT)
gold_price_basis = st.radio(
    "금 가격 기준",
    options=_gold_basis_options,
    format_func=lambda v: config.GOLD_PRICE_BASIS_LABELS[v],
    index=_gold_basis_options.index(st.session_state[config.GOLD_PRICE_BASIS_STATE_KEY]),
    key="_gold_price_basis_widget_backtest",
    horizontal=True,
    help="이 페이지 전체(이동평균·장기추세 필터·매수매도 신호·백테스트·요약지표·그래프)가 이 "
    "기준으로 다시 계산됩니다. 대시보드 페이지와 상태를 공유하므로 여기서 바꾸면 그쪽에도 "
    "반영됩니다.",
)
st.session_state[config.GOLD_PRICE_BASIS_STATE_KEY] = gold_price_basis
st.caption(
    "② KRX 금현물은 환율을 곱해 환산한 값이 아니라, KRX 금현물시장(04020000, \"금 99.99_1kg\") "
    "실제 국내 시세(KRW/g)를 그대로 사용합니다(출처: Naver 증권). 금/은비율은 이 선택과 무관하게 "
    "항상 국제 금·은 시세(GC=F/SI=F, USD/oz) 기준으로 계산됩니다 — 대시보드 표의 금/은비율 행과 "
    "동일합니다. ② 선택 시 최초 데이터 수집에 1분 내외 걸릴 수 있습니다(이후 캐시되어 즉시 표시)."
)

with st.expander("전략 규칙 보기"):
    st.markdown(
        """
- **매수** (미보유 상태일 때만): 아래 "매수 조건" 카드의 `green_count ≥ 임계값` — 고급 설정의
  매수 지연일수만큼 기다린 뒤 체결. **또는** `금/은비율 ≥ 임계값` — 지연 없이 **당일 즉시 매수**.
  **또는** (고급 설정의 **단기 재진입 로직 사용**이 켜져 있을 때만) **재진입 조건**:
  (① 장기추세 필터, 필요조건 — 아래 두 가지를 모두 만족해야 함) 금 종가가 365일(역일) 이동평균보다
  "장기추세 필터 버퍼" %(기본 {buffer_pct:g}%) 이상 높고, 동시에 365일 이동평균 자체가
  30일(역일) 전보다 높아야(우상향) 하며, 그리고 (② 단기 재돌파 트리거) 금 종가가 30일(역일)
  이동평균을 아래에서 위로 상향 돌파한 날 — 이것도 지연 없이 당일 즉시 매수. 아무 조건이나
  먼저 만족하면 매수합니다
- **매도** (보유 상태일 때만): "매도 조건" 카드의 `green_count ≤ 임계값` — 고급 설정의 매도
  지연일수만큼 기다린 뒤 체결. **또는** `금/은비율 ≤ 임계값` — 지연 없이 **당일 즉시 매도**
- 지연이 설정된 green_count 신호는 그 시점 이후 첫 거래일 **종가**로 체결됩니다(지연 기간 중
  조건 재확인 없이 그대로 체결 — 지연일수 0이면 신호 당일 종가에 즉시 체결). 금/은비율과
  재진입 신호는 지연 설정과 무관하게 항상 신호 당일 종가에 체결되며, 아직 대기 중인
  green_count 지연 주문이 있어도 먼저 체결됩니다
- 고급 설정의 **단기 재진입 로직 사용** (기본값 OFF)을 켜야 재진입 조건(①②)이 적용됩니다 —
  꺼져 있으면(기본값) 이 로직 도입 이전의 기준(green_count·금/은비율만)으로 동작합니다. 켜져
  있을 때만 그 아래 **단기 재진입 빈도 제한** (기본값 ON, 이 상위 설정이 꺼져 있으면 비활성화)이
  작동합니다 — 켜두면 재진입 조건 중 ② 단기 재돌파 트리거로 인한 매수만 최근 "단기 재진입
  빈도 제한 일수"(기본 {freq_limit_days}일, 역일 기준) 내 최대 1회로 제한되고(① 장기추세
  필터는 이 제한과 무관하게 항상 필요조건), 꺼두면 ①② 조건만으로 제한 없이 자유롭게
  재진입합니다. 이 제한은 green_count·금/은비율 매수 조건에는 영향을 주지 않습니다
- 고급 설정의 **최소 보유일수**(역일/달력일 기준, 주말·공휴일 관계없이 매수일로부터의 날짜
  차이로 계산)를 설정하면, 매수 후 그 일수가 지나기 전까지는 매도 조건(green_count와
  금/은비율 즉시 매도 모두)을 아예 확인하지 않습니다 — 단기 매매가 아니라 최소 보유 기간을
  두는 전략을 시뮬레이션할 때 사용
- 고급 설정의 **상승추세 중 매도신호 노이즈 필터** (기본값 ON, 매수 쪽 재진입 로직과는 완전히
  별개)는 매도신호가 실제로 체결되기 직전(green_count 지연 주문의 체결일, 또는 금/은비율
  즉시 매도일)에 개입합니다. 그날(D0) 종가가 365일(역일) 이동평균보다 5% 이상 높을 때만 작동하며
  (미만이면 이 필터 없이 항상 그대로 즉시 매도), 작동하면 D0의 매도신호는 무시하고 관찰을
  시작합니다. 관찰 중 추가로 뜨는 매도신호는 매도 여부에 전혀 영향을 주지 않으며 참고용
  기록으로만 남습니다. 매도 실행 여부를 확인하는 방식은 **매도 확인 - 매일 갱신 2시그마 밴드**
  설정에 따라 둘 중 하나입니다:
  - **켜짐(기본값)**: D0 이후 1~7거래일은 하락폭과 무관하게 **무조건 보류**합니다(며칠 사이의
    등락은 랜덤워크 노이즈일 확률이 커서, 그 정도 기간만으로는 진짜 하락인지 판단하지 않음).
    8거래일차부터 21거래일차(약 3주)까지는 매일, 그날까지 경과한 거래일수의 제곱근에 비례해
    넓어지는 확인 밴드를 계산합니다 — `그날의 밴드(%) = 2 × 일간표준편차 × √(경과 거래일수)`,
    일간표준편차는 30년 금 가격 기준 월간 변동성(4.9%)을 √21(한 달 거래일수)로 나눠 환산(예:
    8거래일차 -6.05%, 10거래일차 -6.76%, 14거래일차 -8.00%, 21거래일차 -9.80%). 종가가 D0
    종가 대비 그날의 밴드만큼(또는 그 이상) 하락한 **첫날** 즉시 매도합니다. 21거래일 동안
    한 번도 도달하지 못하면 관찰을 종료하고 D0의 신호는 없었던 것으로 처리합니다(다음
    매도신호부터 처음부터 다시 시작 — 대세 상승장 중 며칠 새 노이즈나 완만한 조정 때문에
    일찍 매도되는 것을 방지하되, 8일차 이후로는 하락 속도가 빠를수록 더 일찍 확인되도록 설계).
  - **꺼짐**: D0+7일(역일 기준) 고정 시점의 종가만을 D0 종가와 비교합니다 — "매도 확인
    하락률"(기본 5%) 이상 낮으면 그날 매도, 그만큼 낮지 않으면 관찰모드를 해제하고 D0의 신호는
    없었던 것으로 처리합니다
- 고급 설정의 **KRX 금현물 보유 수수료 (연, %)** (기본값 0.15%, ② KRX 금현물 선택 시에만
  적용)는 보유 중인 기간의 경과 일수에 비례해 연복리로 수익률에서 차감되며, Buy & Hold와
  신호전략 보유 기간 모두 동일하게 적용됩니다(① 국제 금 시세에는 적용되지 않음)
- 분석 기간: 아래에서 설정한 오늘 기준 최근 **{years}년** (3~10년 조정 가능, 이동평균 계산용으로
  그 이전 {buffer}캘린더일치 데이터를 추가로 사용)
        """.format(
            years=int(st.session_state["bt_years"]),
            buffer=backtest.BUFFER_DAYS,
            buffer_pct=float(st.session_state["bt_long_trend_buffer_pct"]),
            freq_limit_days=int(st.session_state["bt_reentry_freq_limit_days"]),
        )
    )

header_col, reset_col = st.columns([5, 1])
with header_col:
    st.subheader("분석 기간 · 매수·매도 조건")
with reset_col:
    st.write("")
    if st.button("↺ 기본값으로 초기화", use_container_width=True):
        for _key, _default in DEFAULTS.items():
            st.session_state[_key] = _default
        st.rerun()

# Independent of the main dashboard: this key (bt_years) is only ever read or
# written on this page, and the main dashboard's per-indicator charts always
# fetch a fixed CHART_YEARS window regardless of what's set here. Must be
# instantiated after the reset button above (Streamlit forbids writing to a
# widget's session_state key once that widget has been instantiated in the
# same script run).
years = st.number_input(
    "분석 기간 (최근 N년)",
    min_value=backtest.MIN_BACKTEST_YEARS,
    max_value=backtest.MAX_BACKTEST_YEARS,
    step=1,
    key="bt_years",
    help="이 페이지의 백테스트 결과(거래 내역·승률·CAGR·아래 그래프)에만 영향을 줍니다 — "
    "메인 대시보드의 지표별 그래프는 이 값과 무관하게 항상 고정된 기간으로 표시됩니다.",
)

if gold_price_basis == config.GOLD_PRICE_BASIS_KRX and timeseries.gold_window_would_clamp_to_krx(
    today_kst(), int(years), backtest.BUFFER_DAYS
):
    st.info(
        f"KRX 금현물시장은 {config.KRX_GOLD_EARLIEST_DATE} 이후 데이터만 존재합니다. "
        f"분석 시작일이 자동으로 {config.KRX_GOLD_EARLIEST_DATE}로 조정됩니다(이동평균 "
        "계산용 사전 데이터가 짧아지는 만큼, 분석 기간 첫 구간의 365일선/장기추세 필터 "
        "신뢰도가 낮을 수 있습니다)."
    )

buy_card, sell_card = st.columns(2)
with buy_card:
    with st.container(border=True):
        st.markdown("#### 🔵 매수 조건")
        buy_green_count = st.number_input(
            "green_count 임계값 (이상)",
            min_value=0, max_value=6, step=1, key="bt_buy_green_count",
            help="실질금리·달러인덱스 × 7/30/90일(역일) 이평선, 총 6개 셀 중 금값에 우호적인 셀 수가 "
            "이 값 이상이면 매수 신호 (고급 설정의 매수 지연일수만큼 기다린 뒤 체결).",
        )
        buy_ratio = st.number_input(
            "금/은비율 임계값 (이상)",
            min_value=1.0, max_value=200.0, step=1.0, key="bt_buy_ratio",
            help="금/은비율이 이 값 이상이면 그날 즉시 매수합니다 (지연 미적용).",
        )
with sell_card:
    with st.container(border=True):
        st.markdown("#### 🔴 매도 조건")
        sell_green_count = st.number_input(
            "green_count 임계값 (이하)",
            min_value=0, max_value=6, step=1, key="bt_sell_green_count",
            help="금값에 우호적인 셀 수가 이 값 이하로 떨어지면 매도 신호 (고급 설정의 매도 "
            "지연일수만큼 기다린 뒤 체결).",
        )
        sell_ratio = st.number_input(
            "금/은비율 임계값 (이하)",
            min_value=1.0, max_value=200.0, step=1.0, key="bt_sell_ratio",
            help="금/은비율이 이 값 이하이면 그날 즉시 매도합니다 (지연 미적용).",
        )

if sell_ratio >= buy_ratio:
    st.warning(
        "매도 임계값이 매수 임계값보다 크거나 같습니다. 매수 즉시 매도 조건도 함께 만족해 "
        "거의 바로 청산될 수 있습니다."
    )

with st.expander("⚙️ 고급 설정 (지연일수 · 최소 보유일수 · 단기 재진입 로직 — 기본값 그대로 둬도 무방)"):
    st.caption(
        "금/은비율 임계값과 재진입 조건은 지연 없이 항상 신호 당일 즉시 체결됩니다. "
        "아래 지연일수는 green_count 신호에만 적용됩니다."
    )
    adv_buy_col, adv_sell_col = st.columns(2)
    with adv_buy_col:
        st.markdown("**매수 관련**")
        entry_delay_days = st.number_input(
            "매수 지연일수 (일, green_count 신호에만 적용)",
            min_value=0, max_value=180, step=1, key="bt_entry_delay_days",
            help="예: 30을 입력하면 'green_count 신호 발생 1개월 후 매수'를 시뮬레이션합니다. "
            "금/은비율·재진입 매수에는 적용되지 않습니다.",
        )
        st.caption(f"≈ {entry_delay_days / 30:.1f}개월 후 매수")
        use_reentry_trigger = st.checkbox(
            "단기 재진입 로직 사용",
            key="bt_use_reentry_trigger",
            help="끄면 장기추세 필터(① 365일 이동평균)와 단기 재돌파 트리거(② 30일 이동평균 "
            "상향 돌파) 조건 자체를 전혀 적용하지 않고, 이 로직 도입 이전의 재진입 기준"
            "(green_count·금/은비율만)으로 되돌아갑니다. green_count·금/은비율 매수 조건에는 "
            "영향을 주지 않습니다.",
        )
        long_trend_buffer_pct = st.number_input(
            "① 장기추세 필터 버퍼 (%)",
            min_value=0.0, max_value=30.0, step=0.5, key="bt_long_trend_buffer_pct",
            disabled=not use_reentry_trigger,
            help="위 '단기 재진입 로직 사용'이 켜져 있을 때만 작동합니다. 종가가 365일 이동평균"
            "보다 이 %만큼 이상 높아야 ① 장기추세 필터를 만족합니다(예: 5이면 365일선 대비 "
            "+5% 이상). 365일 이동평균 자체가 30일(역일) 전보다 높아야(우상향) 한다는 조건은 "
            "이 값과 무관하게 항상 함께 적용됩니다.",
        )
        use_reentry_freq_limit = st.checkbox(
            f"단기 재진입 빈도 제한 ({int(st.session_state['bt_reentry_freq_limit_days'])}일 내 1회)",
            key="bt_use_reentry_freq_limit",
            disabled=not use_reentry_trigger,
            help="위 '단기 재진입 로직 사용'이 켜져 있을 때만 작동합니다. 켜면 ② 단기 재돌파 "
            "트리거로 인한 매수는 최근 아래 일수(역일 기준) 내 최대 1회로 제한됩니다 — "
            "① 장기추세 필터는 이 제한과 무관하게 항상 필요조건으로 적용됩니다. 끄면 제한 없이 "
            "조건을 만족할 때마다 재진입합니다.",
        )
        reentry_freq_limit_days = st.number_input(
            "단기 재진입 빈도 제한 일수 (일)",
            min_value=1, max_value=365, step=1, key="bt_reentry_freq_limit_days",
            disabled=not (use_reentry_trigger and use_reentry_freq_limit),
            help="위 '단기 재진입 빈도 제한'이 켜져 있을 때만 작동합니다. ② 단기 재돌파 트리거로 "
            "인한 매수를 이 일수(역일 기준) 내 최대 1회로 제한합니다(기본 30일).",
        )

    with adv_sell_col:
        st.markdown("**매도 관련**")
        exit_delay_days = st.number_input(
            "매도 지연일수 (일, green_count 신호에만 적용)",
            min_value=0, max_value=180, step=1, key="bt_exit_delay_days",
            help="예: 30을 입력하면 'green_count 신호 발생 1개월 후 매도'를 시뮬레이션합니다. "
            "금/은비율 매도에는 적용되지 않습니다.",
        )
        st.caption(f"≈ {exit_delay_days / 30:.1f}개월 후 매도")
        min_holding_days = st.number_input(
            "매수 후 최소 보유일수 (일, 역일 기준)",
            min_value=0, max_value=1825, step=1, key="bt_min_holding_days",
            help="매수 이후 이 일수가 지나기 전까지는 매도 조건(green_count, 금/은비율 모두)을 "
            "아예 확인하지 않습니다. 단기 트레이딩이 아닌 전략에 적합합니다. 주말·공휴일과 "
            "무관하게 매수일로부터의 달력일 차이(date2 - date1)로 계산됩니다.",
        )
        st.caption(f"≈ {min_holding_days / 30:.1f}개월간 매도 조건을 무시하고 무조건 보유")
        use_sell_noise_filter = st.checkbox(
            "상승추세 중 매도신호 노이즈 필터 (365일선 +5% 이상)",
            key="bt_use_sell_noise_filter",
            help="매도신호가 발생한 날(D0) 종가가 365일 이동평균보다 "
            f"{backtest.DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT:g}% 이상 높을 때만 작동합니다(그 미만이면 "
            "이 필터와 무관하게 항상 즉시 매도). 켜두면: D0의 매도신호는 무시하고 보유를 유지하며, "
            "아래 '매도 확인 - 매일 갱신 2시그마 밴드' 설정에 따라 매일 확대되는 밴드(기본) 또는 "
            f"D0+{backtest.SELL_NOISE_FILTER_WINDOW_DAYS}일(역일 기준) 고정 시점(끄면) 방식으로 매도 "
            "여부를 확인합니다. 관찰 기간 중 추가로 뜨는 매도신호는 매도 여부에 전혀 영향을 주지 "
            "않고 참고 기록으로만 남습니다. 끄면 매도 신호가 뜨는 즉시 항상 매도합니다(이 로직 "
            "도입 이전과 동일).",
        )
        use_daily_band_confirmation = st.checkbox(
            "매도 확인 - 매일 갱신 2시그마 밴드",
            key="bt_use_daily_band_confirmation",
            disabled=not use_sell_noise_filter,
            help="켜두면(기본값): 관찰 시작(D0) 후 1~7거래일은 하락폭과 무관하게 무조건 보류합니다"
            "(며칠 새 등락은 랜덤워크 노이즈일 확률이 커서 판단하지 않음). 8~21거래일차(3주)까지는 "
            "매일, 그날까지 경과한 거래일수의 제곱근에 비례해 넓어지는 확인 밴드(√t 법칙, 2시그마 "
            "— 예: 8일차 -6.05%, 10일차 -6.76%, 14일차 -8.00%, 21일차 -9.80%)를 계산해, D0 종가 "
            "대비 그날의 밴드만큼(또는 그 이상) 하락한 첫날 즉시 매도합니다. 21거래일 동안 한 번도 "
            "밴드에 도달하지 못하면 관찰을 종료하고 D0의 신호는 없었던 것으로 처리합니다. "
            "끄면: 아래 '매도 확인 하락률'을 사용하는 기존 방식(D0+7일 고정 시점 확인)으로 "
            "동작합니다.",
        )
        sell_noise_filter_drop_pct = st.number_input(
            "매도 확인 하락률 (%, D0 대비 D0+7일 — 위 2시그마 밴드가 꺼져 있을 때만 사용)",
            min_value=0.0, max_value=50.0, step=0.5, key="bt_sell_noise_filter_drop_pct",
            disabled=not use_sell_noise_filter or use_daily_band_confirmation,
            help="위 '매도 확인 - 매일 갱신 2시그마 밴드'가 꺼져 있을 때만 작동하는 고정 방식 "
            "설정입니다. D0+7일 종가가 D0 종가보다 이 %만큼(또는 그 이상) 낮아야만 매도를 "
            "실행합니다(예: 5이면 5% 이상 하락해야 매도, 살짝만 빠진 경우는 대세 상승장으로 보고 "
            "관찰모드를 해제해 계속 보유). 0으로 두면 이전처럼 '조금이라도 낮으면 매도'와 "
            "동일해집니다.",
        )
        krx_holding_fee_pct = st.number_input(
            "KRX 금현물 보유 수수료 (연, %)",
            min_value=0.0, max_value=5.0, step=0.01, format="%.2f", key="bt_krx_holding_fee_pct",
            disabled=gold_price_basis != config.GOLD_PRICE_BASIS_KRX,
            help="② KRX 금현물 선택 시에만 적용되는 연간 보유(보관) 비용입니다(① 국제 금 시세는 "
            "실물이 아닌 참고 가격이라 적용되지 않음). 보유 중인 기간 동안 경과 일수에 비례해 "
            "연복리로 수익률에서 차감되며, Buy & Hold와 신호전략 보유 기간 모두 동일하게 "
            "적용됩니다(기본값 0.15%).",
        )

# The "기대수익률" input itself is rendered later, inside the ④ 신호전략
# (미보유기간 기대수익률 포함) summary-metric group — but its value is needed
# here, before that section, to run the simulation. Reading straight from
# session_state (rather than calling st.number_input again) works because a
# widget's session_state entry always reflects its latest value regardless of
# where in the script it's actually instantiated this run, and a given key can
# only be instantiated once per run.
bond_yield_pct = float(st.session_state["bt_bond_yield_pct"])

refresh_clicked = st.button("데이터 새로고침 (오늘 기준으로 다시 수집)")


@st.cache_data(ttl=3600, show_spinner="데이터를 내려받는 중입니다...")
def load_signals(as_of_iso: str, years: int, gold_price_basis: str) -> pd.DataFrame:
    return backtest.prepare_signals(
        as_of=date.fromisoformat(as_of_iso), years=years, gold_price_basis=gold_price_basis
    )


if refresh_clicked:
    st.cache_data.clear()


def _format_gold_price(value: float, basis: str = gold_price_basis) -> str:
    if basis == config.GOLD_PRICE_BASIS_KRX:
        return f"{value:,.0f}원"
    return f"${value:,.2f}"


# Meaningless (and not applied) unless the KRX basis is active — the input
# itself stays enabled-looking with its 0.15 default either way, but only
# actually reaches the simulation when relevant.
effective_holding_fee_pct = (
    float(krx_holding_fee_pct) if gold_price_basis == config.GOLD_PRICE_BASIS_KRX else 0.0
)

try:
    signals = load_signals(today_kst().isoformat(), int(years), gold_price_basis)
    result = backtest.simulate(
        signals,
        entry_delay_days=int(entry_delay_days),
        exit_delay_days=int(exit_delay_days),
        use_reentry_trigger=use_reentry_trigger,
        use_reentry_freq_limit=use_reentry_freq_limit,
        reentry_freq_limit_days=int(reentry_freq_limit_days),
        long_trend_buffer_pct=float(long_trend_buffer_pct),
        buy_ratio=float(buy_ratio),
        sell_ratio=float(sell_ratio),
        buy_green_count=int(buy_green_count),
        sell_green_count=int(sell_green_count),
        min_holding_days=int(min_holding_days),
        bond_annual_yield=float(bond_yield_pct) / 100.0,
        use_sell_noise_filter=use_sell_noise_filter,
        use_daily_band_confirmation=use_daily_band_confirmation,
        sell_noise_filter_drop_pct=float(sell_noise_filter_drop_pct),
        gold_holding_fee_annual_pct=effective_holding_fee_pct,
    )
except Exception as exc:
    st.error(f"백테스트를 실행하지 못했습니다: {exc}")
    st.stop()

m = result["metrics"]
equity = result["equity_curve"]
bh_equity = result["bh_equity_curve"]
holding_curve = result["holding_curve"]
hybrid_equity = result["hybrid_equity_curve"]
yearly = result["yearly_returns"]
trades = result["trades"]

start_date = equity.index[0].date()
end_date = equity.index[-1].date()
st.caption(
    f"분석 기간: **{start_date} ~ {end_date}** "
    "(신호전략과 Buy & Hold 모두 이 기간의 첫날에 시작 — 동일 시작일 비교)"
)

# ---- 1. 요약 지표 (설정 바로 아래에 배치 — 값을 바꿔가며 바로 확인) ----
# 4개 그룹으로 묶어서 표시: ① 매매 개요 / ② Buy & Hold / ③ 신호전략(보유기간만) /
# ④ 신호전략(미보유기간 기대수익률 포함, 기대수익률 입력도 이 그룹 안에 위치).
st.subheader("요약 지표")
holding_fraction = (
    1.0 - m["non_holding_fraction"] if m["non_holding_fraction"] is not None else None
)

overview_col, bh_col, strategy_held_col, strategy_hybrid_col = st.columns(4)
with overview_col:
    st.markdown("###### ① 매매 개요")
    st.metric("매매횟수", f"{m['closed_trade_count']}회")
    st.metric(
        "보유기간",
        f"{holding_fraction:.1%}" if holding_fraction is not None else "-",
        help="분석 기간 전체(캘린더일 기준) 중 신호전략이 실제로 금을 보유하고 있던 기간의 비중.",
    )
    st.metric("승률", f"{m['win_rate']:.1%}" if m["win_rate"] is not None else "-")
with bh_col:
    st.markdown(f"###### ② {BH_LABEL}")
    st.metric("누적수익률", f"{m['bh_total_return']:.1%}")
    st.metric("연환산수익률(CAGR)", f"{m['bh_cagr']:.1%}")
with strategy_held_col:
    st.markdown(f"###### ③ {STRATEGY_LABEL} (보유기간)")
    st.metric(
        "누적수익률",
        f"{m['strategy_total_return']:.1%}",
        help="보유 기간에만 투자했다고 가정한 누적수익률(미보유 기간은 반영하지 않음).",
    )
    st.metric(
        "연환산수익률(CAGR)",
        f"{m['strategy_cagr']:.1%}" if m["strategy_cagr"] is not None else "-",
        help="실제로 금을 보유했던 기간의 일수만 분모로 사용한 연환산수익률(현금 보유 기간 제외).",
    )
with strategy_hybrid_col:
    st.markdown(f"###### ④ {STRATEGY_LABEL} (미보유기간 기대수익률 포함)")
    st.metric(
        "누적수익률",
        f"{m['hybrid_total_return']:.1%}" if m["hybrid_total_return"] is not None else "-",
        help="보유 기간엔 실제 금 수익률을, 미보유 기간엔 아래 '기대수익률'을 적용해 이어 붙인 "
        "전체 분석기간 기준 누적수익률입니다.",
    )
    st.metric(
        "연환산수익률(CAGR)",
        f"{m['hybrid_cagr']:.1%}" if m["hybrid_cagr"] is not None else "-",
        help="위 누적수익률을 분석 기간 전체를 기준으로 연환산한 값입니다.",
    )
    bond_yield_pct = st.number_input(
        "기대수익률 (연, %)",
        min_value=0.0, max_value=20.0, step=0.1, key="bt_bond_yield_pct",
        help="신호가 없어 금을 보유하지 않는 기간 동안, 그 돈을 이 연이율로 운용했다고 "
        "가정합니다(예: 채권 매입). 값을 바꾸면 이 그룹의 누적수익률·CAGR이 바로 재계산됩니다.",
    )

if m["has_open_position"]:
    st.info(
        "현재 포지션을 보유 중입니다. 마지막 거래는 미청산 상태이며, 위 수익률·아래 거래 내역에 "
        "표시된 값은 오늘 종가 기준 평가손익입니다."
    )
if m["strategy_cagr"] is None:
    st.caption("ℹ️ 신호전략이 이 기간 동안 한 번도 매수 신호를 내지 않아 CAGR을 계산할 수 없습니다.")

# ---- 2. 누적수익률 라인차트 (+ 매수/매도 시점 마커) ----
st.subheader("누적수익률")
STRAT_HYBRID_LABEL = f"{STRATEGY_LABEL}(기대수익률 포함)"

# 신호전략(보유기간만)은 이 차트에서 제외 — Buy & Hold와 신호전략(기대수익률 포함) 둘만 표시.
cum_df = pd.DataFrame(
    {
        "date": equity.index,
        BH_LABEL: bh_equity.reindex(equity.index).values - 1.0,
    }
).melt("date", var_name="series", value_name="return")

# ④(기대수익률 포함) curve's holding-segment points feed into the SAME
# melted frame/color scale as BH, so both share exactly one legend. Its
# non-holding-segment points are a separate, unencoded-color layer below
# (dashed + lighter tint) so they don't add a 3rd legend entry.
hybrid_returns = hybrid_equity.reindex(equity.index).to_numpy() - 1.0
holding_bool = holding_curve.reindex(equity.index).fillna(False).to_numpy()
n_points = len(hybrid_returns)
# Segment j spans (point j, point j+1) and is a "holding" segment iff
# holding_bool[j] — matching compute_hybrid_cagr's own convention (a step is
# classified by the state going INTO it). A point belongs to the holding
# sub-line if either segment touching it is a holding segment (so the two
# sub-lines share their shared boundary point and visually connect there).
point_in_holding = np.zeros(n_points, dtype=bool)
point_in_nonholding = np.zeros(n_points, dtype=bool)
if n_points > 1:
    seg_holding = holding_bool[:-1]
    point_in_holding[:-1] |= seg_holding
    point_in_holding[1:] |= seg_holding
    point_in_nonholding[:-1] |= ~seg_holding
    point_in_nonholding[1:] |= ~seg_holding
else:
    point_in_holding[:] = holding_bool
    point_in_nonholding[:] = ~holding_bool

hybrid_holding_df = pd.DataFrame(
    {
        "date": equity.index,
        "series": STRAT_HYBRID_LABEL,
        "return": np.where(point_in_holding, hybrid_returns, np.nan),
    }
)
cum_df = pd.concat([cum_df, hybrid_holding_df], ignore_index=True)

line_chart = (
    alt.Chart(cum_df)
    .mark_line(strokeWidth=2)
    .encode(
        x=alt.X("date:T", axis=alt.Axis(title=None, format="%Y", tickCount="year")),
        y=alt.Y("return:Q", title="누적수익률", axis=alt.Axis(format="%")),
        color=alt.Color(
            "series:N",
            title=None,
            scale=alt.Scale(
                domain=[BH_LABEL, STRAT_HYBRID_LABEL],
                range=[BH_COLOR, STRATEGY_HYBRID_COLOR],
            ),
        ),
        tooltip=[
            alt.Tooltip("date:T", title="날짜"),
            alt.Tooltip("series:N", title="전략"),
            alt.Tooltip("return:Q", title="누적수익률", format=".1%"),
        ],
    )
)

# 미보유 구간: 옅은 톤 + 점선, 위 색상 스케일과 무관한 리터럴 색상이라 범례에
# 별도 항목을 만들지 않음(같은 STRAT_HYBRID_LABEL 시리즈의 연장선일 뿐).
hybrid_nonholding_df = pd.DataFrame(
    {
        "date": equity.index,
        "return": np.where(point_in_nonholding, hybrid_returns, np.nan),
        "안내": f"기대수익률 연 {bond_yield_pct:g}% 가정 적용 구간",
    }
)
hybrid_nonholding_line = (
    alt.Chart(hybrid_nonholding_df)
    .mark_line(strokeWidth=2, strokeDash=[6, 4], color=STRATEGY_HYBRID_NONHOLDING_COLOR)
    .encode(
        x="date:T",
        y="return:Q",
        tooltip=[
            alt.Tooltip("date:T", title="날짜"),
            alt.Tooltip("return:Q", title="누적수익률(기대수익률 적용)", format=".1%"),
            alt.Tooltip("안내:N", title=None),
        ],
    )
)

# 미보유 구간 배경 음영(회색) — 연속 미보유 구간을 하나의 띠로 묶어서 표시.
non_holding_bands = None
if n_points > 1:
    seg_df = pd.DataFrame(
        {"start": equity.index[:-1], "end": equity.index[1:], "holding": holding_bool[:-1]}
    )
    seg_df["run_id"] = (seg_df["holding"] != seg_df["holding"].shift()).cumsum()
    runs = seg_df.groupby("run_id").agg(
        start=("start", "first"), end=("end", "last"), holding=("holding", "first")
    )
    bands_df = runs.loc[~runs["holding"], ["start", "end"]].copy()
    if not bands_df.empty:
        bands_df["안내"] = f"기대수익률 연 {bond_yield_pct:g}% 가정 적용 구간"
        non_holding_bands = (
            alt.Chart(bands_df)
            .mark_rect(color=NONHOLDING_BAND_COLOR, opacity=0.14)
            .encode(
                x="start:T",
                x2="end:T",
                tooltip=[
                    alt.Tooltip("start:T", title="시작"),
                    alt.Tooltip("end:T", title="종료"),
                    alt.Tooltip("안내:N", title=None),
                ],
            )
        )

marker_rows = []
for t in trades:
    marker_rows.append(
        {
            "date": t["entry_date"],
            "구분": "매수",
            "return": float(hybrid_equity.loc[t["entry_date"]]) - 1.0,
            "가격": round(t["entry_price"], 2),
            "사유": t["entry_reason"] or "-",
        }
    )
    if not t["open"]:
        marker_rows.append(
            {
                "date": t["exit_date"],
                "구분": "매도",
                "return": float(hybrid_equity.loc[t["exit_date"]]) - 1.0,
                "가격": round(t["exit_price"], 2),
                "사유": t["exit_reason"] or "-",
            }
        )
marker_df = pd.DataFrame(marker_rows)

# Basis-aware price display: KRX (KRW/g) shows no decimals and no "$", intl
# (USD/oz) keeps the original "$" formatting.
_price_tooltip_format = "$,.2f" if gold_price_basis == config.GOLD_PRICE_BASIS_INTL else ",.0f"
_price_tooltip_title = "체결가" if gold_price_basis == config.GOLD_PRICE_BASIS_INTL else "체결가 (원)"

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
                alt.Tooltip("가격:Q", title=_price_tooltip_title, format=_price_tooltip_format),
                alt.Tooltip("return:Q", title="당시 누적수익률", format=".1%"),
                alt.Tooltip("사유:N", title="사유"),
            ],
        )
    )
    chart_layers = [line_chart, hybrid_nonholding_line, markers]
else:
    chart_layers = [line_chart, hybrid_nonholding_line]
if non_holding_bands is not None:
    chart_layers = [non_holding_bands] + chart_layers
combined_chart = alt.layer(*chart_layers).resolve_scale(color="independent", shape="independent")

st.altair_chart(combined_chart.properties(height=380).interactive(), use_container_width=True)
st.caption(
    "▲ 파란색 = 매수 시점, ▼ 빨간색 = 매도 시점 (거래 내역 표 참고) · "
    f"{STRAT_HYBRID_LABEL}의 점선·회색 음영 구간 = 미보유(현금) 기간에 기대수익률을 "
    "가정 적용한 부분"
)

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
            "매수가": _format_gold_price(t["entry_price"]),
            "매수 사유": t["entry_reason"] or "-",
            "매도일": t["exit_date"].date() if t["exit_date"] is not None else "미청산(보유 중)",
            "매도가": _format_gold_price(t["exit_price"]),
            "매도 사유": t["exit_reason"] or ("미청산" if t["open"] else "-"),
            "보유일수": t["hold_days"],
            "구간수익률": f"{t['period_return']:.2%}" + (" (평가)" if t["open"] else ""),
        }
        for t in trades
    ]
    st.dataframe(pd.DataFrame(trade_rows), use_container_width=True, hide_index=True)
    st.caption(
        "매수 사유/매도 사유는 신호가 처음 발생한 날 기준입니다. green_count 매수/매도는 "
        "지연일수만큼 지난 뒤 체결되어 매수·매도일이 신호 발생일과 다를 수 있지만, 금/은비율·"
        "재진입 매수는 항상 체결일 = 신호 발생일입니다."
    )
else:
    st.caption("이 기간 동안 매수 신호가 발생하지 않아 거래 내역이 없습니다.")

st.caption(
    "⚠️ 본 백테스트는 과거 데이터에 기반한 시뮬레이션 결과이며 미래 성과를 보장하지 않습니다. "
    "거래비용·세금·슬리피지는 반영되어 있지 않고, 표본 기간이 짧아 과최적화(overfitting) 위험이 "
    "있습니다. '④ 신호전략 (미보유기간 기대수익률 포함)' 그룹의 기대수익률은 사용자가 입력한 "
    "단일 연이율을 그대로 연복리 적용한 단순 가정치이며, 실제 채권 등 투자자산의 이자율 변동· "
    "재투자·신용위험은 반영되어 있지 않습니다."
)

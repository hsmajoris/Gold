"""자산군 비교 페이지: 금·반포자이(부동산)·비트코인·하이닉스·코스피를 월별로
지수화해 비교하고, 각 자산을 반포자이 실거래가 추세에 맞춰 거듭제곱 변환
(λ)·래그(개월)·제외구간을 직접 조절하며 R²(선형 적합도)를 확인할 수 있는
탐색 도구. 추가로 "반포자이가 N년 안에 k배 오르면 다른 자산은?" 시나리오
계산기를 제공한다.

이 페이지는 백테스트 페이지(가격 기준 선택·매매신호·수수료)와는 완전히
독립적이다 — 매매신호 로직을 전혀 쓰지 않고, 순수하게 자산 간 과거 수익률과
통계적 상관관계만 탐색한다."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from gold_dashboard import data_sources as ds

st.title("자산군 비교 — 금 · 반포자이 · 비트코인 · 하이닉스 · 코스피")
st.caption(
    "다섯 자산을 월별로 지수화(=100 기준)해 비교하고, 반포자이 84㎡ 실거래가 추세와 "
    "다른 네 자산 사이의 선형 적합도(R²)를 직접 λ(거듭제곱 변환)·래그(개월)·제외구간을 "
    "바꿔가며 확인할 수 있습니다. 매매신호와는 무관한 순수 통계 비교 도구입니다."
)

ASSET_TICKERS = {
    "비트코인": "BTC-KRW",
    "하이닉스": "000660.KS",
    "코스피": "^KS11",
}
FETCH_START = date(2010, 1, 1)
CONTROLLABLE_ASSETS = ["비트코인", "하이닉스", "코스피", "국내금"]
BENCHMARK = "반포자이"

# 자산별 기본 λ·래그 — 2026-11-25 그리드서치 결과(월별 집계, 반포자이84 평당가
# 추세 기준, TRADING_LOGIC.md 참고). 사용자가 아래 컨트롤에서 자유롭게 바꿀 수
# 있다.
DEFAULT_LAMBDA = {"비트코인": 0.40, "하이닉스": 0.10, "코스피": -1.3, "국내금": -0.70}
DEFAULT_LAG_MONTHS = {"비트코인": 6, "하이닉스": 12, "코스피": 11, "국내금": 2}


@st.cache_data(ttl=3600, show_spinner="자산 가격 데이터를 내려받는 중입니다...")
def load_raw_series() -> dict[str, pd.Series]:
    end = date.today() + timedelta(days=1)
    series = {
        name: ds.fetch_yfinance_close(ticker, start=FETCH_START, end=end)
        for name, ticker in ASSET_TICKERS.items()
    }
    series["국내금"] = ds.fetch_krx_gold_krw_per_gram()
    series[BENCHMARK] = ds.fetch_banpo_xi_trend_series()
    return series


def monthly_mean_frame(series_dict: dict[str, pd.Series]) -> pd.DataFrame:
    """Date-indexed daily/transaction series -> one DataFrame of monthly
    arithmetic means, reindexed onto the union of every series' own month
    range (missing months are NaN, never 0)."""
    monthly = {}
    all_months: pd.PeriodIndex | None = None
    for name, s in series_dict.items():
        m = s.copy()
        m.index = m.index.to_period("M")
        grouped = m.groupby(level=0).mean()
        monthly[name] = grouped
        months = grouped.index
        all_months = months if all_months is None else all_months.union(months)
    all_months = all_months.sort_values()
    return pd.DataFrame({name: s.reindex(all_months) for name, s in monthly.items()}, index=all_months)


def to_index100(s: pd.Series) -> pd.Series:
    first_valid = s.first_valid_index()
    if first_valid is None:
        return s
    return s / s[first_valid] * 100.0


def transform(x: np.ndarray, lam: float) -> np.ndarray:
    if abs(lam) < 1e-9:
        return np.log(x)
    return np.power(x, lam)


def inverse_transform(y: np.ndarray, lam: float) -> np.ndarray:
    if abs(lam) < 1e-9:
        return np.exp(y)
    return np.power(y, 1.0 / lam)


def fit_and_r2(monthly: pd.DataFrame, asset: str, lam: float, lag: int, excl_start, excl_end):
    """Shift `asset`'s monthly series back by `lag` months (so row t pairs
    asset[t-lag] with 반포자이[t]), apply the λ transform, fit a line against
    반포자이's own monthly mean, and return (slope, intercept, r2, n,
    source_months actually used). `excl_start`/`excl_end` (dates or None) drop
    any row whose asset SOURCE month (post-lag) falls inside that range from
    both the fit and n — mirrors the 총괄판 엑셀의 '제외구간' 기능."""
    source_month = monthly.index - lag
    asset_vals = monthly[asset].reindex(source_month).to_numpy()
    banpo_vals = monthly[BENCHMARK].to_numpy()
    source_ts = source_month.to_timestamp()

    valid = ~np.isnan(asset_vals) & ~np.isnan(banpo_vals) & (asset_vals > 0)
    if excl_start is not None and excl_end is not None:
        excluded = (source_ts >= pd.Timestamp(excl_start)) & (source_ts <= pd.Timestamp(excl_end))
        valid &= ~excluded

    n = int(valid.sum())
    if n < 5:
        return None

    x = transform(asset_vals[valid], lam)
    y = banpo_vals[valid]
    slope, intercept = np.polyfit(x, y, 1)
    pred = intercept + slope * x
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return {
        "slope": slope, "intercept": intercept, "r2": r2, "n": n,
        "asset_min": float(np.nanmin(asset_vals[valid])), "asset_max": float(np.nanmax(asset_vals[valid])),
    }


raw_series = load_raw_series()
monthly = monthly_mean_frame(raw_series)

st.subheader("지수 비교 (각 자산 자신의 첫 유효월 = 100)")
fig = go.Figure()
colors = {"비트코인": "#5b6fb8", "반포자이": "#a13d2f", "하이닉스": "#2f8a5f", "코스피": "#8a8a8a", "국내금": "#b8860b"}
for name in [BENCHMARK, "비트코인", "하이닉스", "코스피", "국내금"]:
    idx = to_index100(monthly[name])
    fig.add_trace(go.Scatter(
        x=idx.index.to_timestamp(), y=idx.values, mode="lines", name=name,
        line=dict(color=colors.get(name)),
    ))
fig.update_layout(
    height=460, margin=dict(l=40, r=20, t=20, b=40),
    yaxis_title="지수(첫 유효월=100, 로그축)", yaxis_type="log",
    legend=dict(orientation="h", yanchor="bottom", y=1.02),
)
st.plotly_chart(fig, use_container_width=True)
st.caption(
    "자산마다 데이터가 존재하는 기간이 달라(비트코인 2014-09~, 국내금 2014-03~, 반포자이 "
    "2009-05~) 각자 자기 자신의 첫 유효월을 100으로 삼습니다 — 서로 다른 시작점을 가진 "
    "지수이므로 '지금 몇 배 차이'로는 읽을 수 있지만 '같은 시작점에서 누가 더 올랐는지'는 "
    "아래 R² 섹션의 표본 기간을 참고하세요. 값의 자릿수 차이가 커서 y축은 로그 스케일입니다."
)

st.subheader("반포자이 실거래가 추세와의 선형 적합도 (R²)")
st.caption(
    "각 자산 가격에 거듭제곱 변환(λ)을 적용한 값과 반포자이 추세 사이의 선형회귀 R²입니다. "
    "λ=1은 원값, λ=0.5는 제곱근, λ→0은 로그, λ<0은 역수 계열입니다. 래그(개월)는 그 자산의 "
    "n개월 전 가격을 같은 달의 반포자이 값과 짝짓습니다(그 자산이 n개월 선행한다는 가정)."
)

fit_results: dict[str, dict | None] = {}
cols = st.columns(len(CONTROLLABLE_ASSETS))
for col, asset in zip(cols, CONTROLLABLE_ASSETS):
    with col:
        st.markdown(f"**{asset}**")
        lam = st.number_input(
            "λ", min_value=-3.0, max_value=3.0, step=0.05,
            value=DEFAULT_LAMBDA[asset], key=f"lam_{asset}",
            help="0=로그, 1=원값, 0.5=제곱근, 1/3≈0.33=세제곱근",
        )
        lag = st.number_input(
            "래그(개월)", min_value=-24, max_value=36, step=1,
            value=DEFAULT_LAG_MONTHS[asset], key=f"lag_{asset}",
        )
        use_excl = st.checkbox("제외구간 사용", key=f"excl_on_{asset}")
        excl_start = excl_end = None
        if use_excl:
            excl_range = st.date_input(
                "제외할 기간(이 자산의 소스월 기준)",
                value=(date.today() - timedelta(days=30), date.today()),
                key=f"excl_range_{asset}",
            )
            if isinstance(excl_range, tuple) and len(excl_range) == 2:
                excl_start, excl_end = excl_range
        result = fit_and_r2(monthly, asset, float(lam), int(lag), excl_start, excl_end)
        fit_results[asset] = result
        if result is None:
            st.metric("R²", "표본 부족")
        else:
            st.metric("R²", f"{result['r2']:.3f}")
            st.caption(f"표본 {result['n']}개월")

st.divider()

st.subheader("시나리오 계산기 — 반포자이가 N년간 k배 오르면 다른 자산은?")
st.caption(
    "위에서 설정한 각 자산의 λ·래그·제외구간 그대로, 반포자이가 목표 배수에 도달했을 때 "
    "그 회귀식이 성립하려면 각 자산 가격이 얼마여야 하는지 역산합니다. 이건 예측이 아니라 "
    "'과거의 통계적 관계를 그대로 연장하면 나오는 숫자'일 뿐이며, 특히 관측 범위를 크게 "
    "벗어나는 추정은 신뢰도가 급격히 떨어집니다(아래 ⚠️ 표시 참고)."
)

scenario_cols = st.columns(3)
with scenario_cols[0]:
    fold_change = st.number_input("반포자이 목표 배수", min_value=0.1, max_value=20.0, value=2.0, step=0.1)
with scenario_cols[1]:
    horizon_years = st.number_input("기간(년)", min_value=1, max_value=30, value=5, step=1)

banpo_now = monthly[BENCHMARK].dropna().iloc[-1]
banpo_target = banpo_now * fold_change
banpo_cagr = fold_change ** (1.0 / horizon_years) - 1.0

st.markdown(
    f"현재 반포자이 추세가 **{banpo_now:,.0f}원** → {horizon_years}년 뒤 {fold_change:g}배인 "
    f"**{banpo_target:,.0f}원**이 된다고 가정합니다(연복리 {banpo_cagr:.1%})."
)

rows = []
for asset in CONTROLLABLE_ASSETS:
    result = fit_results[asset]
    asset_now = monthly[asset].dropna().iloc[-1]
    if result is None or abs(result["slope"]) < 1e-12:
        rows.append({"자산": asset, "R²": "-", "함의 가격": "-", "현재 대비": "-", "연복리(CAGR)": "-", "반포자이 대비": "-", "비고": "회귀 불가(표본 부족)"})
        continue
    x_required = (banpo_target - result["intercept"]) / result["slope"]
    if x_required <= 0:
        rows.append({"자산": asset, "R²": f"{result['r2']:.3f}", "함의 가격": "-", "현재 대비": "-", "연복리(CAGR)": "-", "반포자이 대비": "-", "비고": "회귀식상 불가능(음수)"})
        continue
    asset_required = float(inverse_transform(np.array([x_required]), float(st.session_state[f"lam_{asset}"]))[0])
    asset_fold = asset_required / asset_now
    asset_cagr = asset_fold ** (1.0 / horizon_years) - 1.0
    relative_fold = asset_fold / fold_change
    out_of_range = asset_required > result["asset_max"] * 1.5 or asset_required < result["asset_min"] * 0.67
    rows.append({
        "자산": asset,
        "R²": f"{result['r2']:.3f}",
        "함의 가격": f"{asset_required:,.0f}" + ("⚠️" if out_of_range else ""),
        "현재 대비": f"{asset_fold:.2f}배",
        "연복리(CAGR)": f"{asset_cagr:.1%}",
        "반포자이 대비": f"{relative_fold:.2f}배 " + ("오버퍼폼" if relative_fold >= 1 else "언더퍼폼"),
        "비고": "⚠️ 관측 범위를 크게 벗어남(외삽)" if out_of_range else "",
    })

st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
st.caption(
    "'반포자이 대비' 배수는 (그 자산의 함의 배수) ÷ (반포자이 목표 배수)입니다 — 1배보다 "
    "크면 반포자이보다 더 가파르게 올라야(오버퍼폼) 같은 회귀식이 성립하고, 1배보다 작으면 "
    "덜 올라도(혹은 떨어져도, 1.0 미만) 성립한다는 뜻입니다. ⚠️ 표시는 함의 가격이 그 자산의 "
    "과거 관측 범위(최소~최대)를 50% 넘게 벗어난 경우로, 회귀식의 형태가 그 구간에서도 "
    "유지된다는 보장이 없어 특히 신뢰도가 낮습니다."
)

st.caption(
    "⚠️ 전체 유의사항: 이 페이지의 모든 R²·시나리오 수치는 상관관계 탐색이며 인과관계를 "
    "뜻하지 않습니다. 표본 기간(최대 17년)에 완결된 사이클이 몇 번 되지 않아 특정 λ·래그에서 "
    "높은 R²가 나와도 과최적화(overfitting)일 가능성을 배제할 수 없습니다. 투자 판단의 "
    "근거로 사용하는 것은 권장하지 않습니다."
)

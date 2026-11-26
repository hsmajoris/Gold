"""자산군 비교 페이지: 반포자이(부동산) 실거래가 추세를 기준으로 삼아, 선택한
비교 자산(비트코인·하이닉스·코스피·국내금)을 월별로 지수화해 같은 차트에
겹쳐보고, 각 자산을 반포자이 추세에 맞춰 거듭제곱 변환(λ)·래그(개월)·제외구간을
직접 조절하며 R²(적합도)를 확인할 수 있는 탐색 도구. 회귀식은 log(반포자이) =
절편 + 기울기×transform(자산,λ) 형태(log-log에 가까운 "비율 대 비율" 모델) —
반포자이·비교 자산 둘 다 시간에 따라 복리로(비율로) 성장하는 가격이라, 반포자이
쪽도 log로 맞춰야 "과거에 둘 다 비슷한 배수로 움직였다"는 관측과 시나리오
계산기의 배수 추정이 일관된다(반포자이를 원금액 그대로 두면 큰 폭 외삽에서
배수가 왜곡된다). 추가로 "반포자이가 N년 안에 k배 오르면 다른 자산은?" 시나리오
계산기를 제공한다.

차트는 백테스트 페이지와 같은 방식(Y축 줌 자동조정 + X축 연/월 눈금 전환 +
하단 레인지슬라이더·상단 기간 버튼)으로 렌더링한다(gold_dashboard/chart_utils.
render_zoomable_chart). 이 차트는 st.iframe으로 띄우는 정적 HTML+JS라 Python
쪽으로 "지금 확대해서 보는 구간"을 자동으로 돌려받을 채널이 없으므로(진짜
양방향 커스텀 컴포넌트를 시도했으나 이 환경에서 Streamlit의 componentReady/
render 핸드셰이크가 안정적으로 동작하지 않아 포기했다), 같은 효과는 아래
"특정 구간만의 R²" 섹션의 수동 기간 선택(date_input)으로 제공한다 — 차트에서
보고 있는 기간을 그대로 거기 입력하면 된다.

이 페이지는 백테스트 페이지(가격 기준 선택·매매신호·수수료)와는 완전히
독립적이다 — 매매신호 로직을 전혀 쓰지 않고, 순수하게 자산 간 과거 수익률과
통계적 상관관계만 탐색한다."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from gold_dashboard import config
from gold_dashboard import data_sources as ds
from gold_dashboard.chart_utils import render_zoomable_chart

st.title("자산군 비교 — 반포자이 기준 + 선택 자산 비교")
st.caption(
    "기본으로는 반포자이 84㎡ 실거래가(점=개별 체결가)만 보여줍니다. "
    "아래에서 비교하고 싶은 자산을 선택하면 그 자산이 월별 지수로 차트에 추가되고, λ(거듭제곱"
    " 변환)·래그(개월)를 조절하며 반포자이 추세와의 선형 적합도(R²)도 확인할 수 있습니다. "
    "매매신호와는 무관한 순수 통계 비교 도구입니다. 날짜는 실제 계약일 기준입니다(백테스트 "
    "⑧ 기준은 실거래 신고기한 지연을 반영해 계약일+45일로 미뤄서 쓰지만, 이 페이지는 매매 "
    "신호를 만들지 않으므로 그 지연 없이 원래 계약일을 그대로 씁니다)."
)

ASSET_TICKERS = {
    "하이닉스": "000660.KS",
    "코스피": "^KS11",
}
FETCH_START = date(2010, 1, 1)
CONTROLLABLE_ASSETS = ["비트코인", "하이닉스", "코스피", "국내금"]
BENCHMARK = "반포자이"
COLORS = {"비트코인": "#5b6fb8", "반포자이": "#a13d2f", "하이닉스": "#2f8a5f", "코스피": "#8a8a8a", "국내금": "#b8860b"}

# 자산별 기본 λ·래그. 회귀식은 log(반포자이) = 절편 + 기울기×transform(자산,λ)
# 형태(반포자이·자산 둘 다 "비율로" 성장하는 자산이라, 반포자이 쪽도 log를 써야
# 두 자산의 과거 등락 '배수'가 시나리오 계산기에서도 일관되게 나온다 — 예를
# 들어 반포자이 쪽만 원금액 그대로 두면, 과거엔 금값도 반포자이도 비슷하게
# 4배 올랐는데 "반포자이 2배 → 금 9.87배" 같은 왜곡된 외삽이 나왔었다).
# λ는 0 이상(로그~거듭제곱)만 허용한다 — 음수 λ는 회귀식을 역으로 풀 때(시나리오
# 계산기) x^λ(λ<0)의 부호 반전 때문에 "회귀식상 도달 불가능(음수)" 같은 해석하기
# 어려운 결과를 만들기 쉽다. 아래 값은 2026-11-26 그리드서치 결과(월별 집계,
# 이 log-log 모델·래그는 과거 그대로 고정한 채 λ∈[0,3]만 재탐색, TRADING_LOGIC.md
# 참고) — R²: 비트코인 0.948, 하이닉스 0.914, 코스피 0.665, 국내금 0.825.
# 아래 컨트롤에서 자유롭게 바꿀 수 있다.
DEFAULT_LAMBDA = {"비트코인": 0.25, "하이닉스": 0.0, "코스피": 0.0, "국내금": 0.0}
DEFAULT_LAG_MONTHS = {"비트코인": 6, "하이닉스": 12, "코스피": 11, "국내금": 2}


@st.cache_data(ttl=3600, show_spinner="자산 가격 데이터를 내려받는 중입니다...")
def load_raw_series() -> dict[str, pd.Series]:
    end = date.today() + timedelta(days=1)
    series = {
        name: ds.fetch_yfinance_close(ticker, start=FETCH_START, end=end)
        for name, ticker in ASSET_TICKERS.items()
    }
    series["국내금"] = ds.fetch_krx_gold_krw_per_gram()
    # yfinance의 BTC-KRW는 2014-09-17 이전 데이터가 없어, 비트코인의 실제 거래
    # 시작 시점(2010년 중반)부터 비교하려면 별도 함수가 필요하다 — 아래 참고.
    series["비트코인"] = ds.fetch_bitcoin_full_history_krw()
    return series


@st.cache_data(ttl=3600, show_spinner=False)
def load_banpo_raw_and_trend() -> pd.DataFrame:
    # 캐시 파일(data/banpo_xi_trend.json)은 신고기한 지연을 더한 "확인 가능일"
    # (계약일+45일, config.BANPO_REPORTING_LAG_DAYS — look-ahead bias 방지용,
    # 백테스트 ⑧ 기준이 실제 매매 신호를 만드는 데 꼭 필요한 보수적 지연)로
    # 인덱싱돼 있다. 이 페이지는 매매신호를 전혀 쓰지 않는 순수 통계 비교
    # 도구라 그 지연이 굳이 필요하지 않고, (엑셀로 내보낸 비교 자료와 동일한
    # 기준으로 비교하기 위해) 실제 계약일로 되돌려서 쓴다 — 이 되돌림은 이
    # 페이지 안에서만 적용되고, 캐시 파일 자체나 백테스트 페이지에는 영향이
    # 없다(fetch_banpo_xi_raw_and_trend가 돌려주는 DataFrame의 복사본만 바꿈).
    df = ds.fetch_banpo_xi_raw_and_trend().copy()
    df.index = df.index - pd.Timedelta(days=config.BANPO_REPORTING_LAG_DAYS)
    return df


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


def transform(x, lam: float):
    if abs(lam) < 1e-9:
        return np.log(x)
    return np.power(x, lam)


def inverse_transform(y, lam: float):
    if abs(lam) < 1e-9:
        return np.exp(y)
    return np.power(y, 1.0 / lam)


def fit_and_r2(
    monthly: pd.DataFrame, banpo_monthly: pd.Series, asset: str, lam: float, lag: int,
    excl_start, excl_end, window_start=None, window_end=None,
):
    """Shift `asset`'s monthly series back by `lag` months (so row t pairs
    asset[t-lag] with 반포자이[t]), apply the λ transform to the asset side,
    and fit a line of log(반포자이) on that transformed asset value — both
    반포자이와 비교 자산 모두 가격이 시간에 따라 "비율로"(복리로) 성장하는
    자산이라, 반포자이 쪽도 log로 맞춰야 두 자산의 과거 등락 '배수'가 시나리오
    계산기에서 일관되게 나온다(반포자이만 원금액 그대로 두면 큰 폭 외삽에서
    기울기가 금액 단위로 고정돼 버려, 배수로는 말이 안 되는 결과가 나온다).
    Returns a dict of fit stats, or None if too few valid months remain.
    `excl_start`/`excl_end` (dates or None) drop any row whose asset SOURCE
    month (post-lag) falls inside that range. `window_start`/`window_end`
    (dates or None) do the opposite — KEEP only rows whose 반포자이(nominal,
    t) month falls inside that range, i.e. the chart's current visible
    x-range (both 반포자이's own trace and the lag-shifted asset trace sit at
    that same t position on screen — see the chart-trace loop below — so
    this matches "only use what's currently zoomed into on screen")."""
    source_month = monthly.index - lag
    asset_vals = monthly[asset].reindex(source_month).to_numpy()
    banpo_vals = banpo_monthly.reindex(monthly.index).to_numpy()
    source_ts = source_month.to_timestamp()
    nominal_ts = monthly.index.to_timestamp()

    valid = ~np.isnan(asset_vals) & ~np.isnan(banpo_vals) & (asset_vals > 0) & (banpo_vals > 0)
    if excl_start is not None and excl_end is not None:
        excluded = (source_ts >= pd.Timestamp(excl_start)) & (source_ts <= pd.Timestamp(excl_end))
        valid &= ~excluded
    if window_start is not None and window_end is not None:
        within = (nominal_ts >= pd.Timestamp(window_start)) & (nominal_ts <= pd.Timestamp(window_end))
        valid &= within

    n = int(valid.sum())
    if n < 5:
        return None

    x = transform(asset_vals[valid], lam)
    y = np.log(banpo_vals[valid])
    slope, intercept = np.polyfit(x, y, 1)
    pred = intercept + slope * x
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    x_mean = float(x.mean())
    sxx = float(np.sum((x - x_mean) ** 2))
    resid_se = float(np.sqrt(ss_res / (n - 2))) if n > 2 else float("nan")
    return {
        "slope": slope, "intercept": intercept, "r2": r2, "n": n,
        "asset_min": float(np.nanmin(asset_vals[valid])), "asset_max": float(np.nanmax(asset_vals[valid])),
        "x_mean": x_mean, "sxx": sxx, "resid_se": resid_se,
    }


raw_series = load_raw_series()
banpo_raw_trend = load_banpo_raw_and_trend()
monthly = monthly_mean_frame(raw_series)
banpo_monthly = banpo_raw_trend["trend"].copy()
banpo_monthly.index = banpo_monthly.index.to_period("M")
banpo_monthly = banpo_monthly.groupby(level=0).mean()

st.subheader("비교 자산 선택")
selected_assets = st.multiselect(
    "차트에 추가할 자산",
    options=CONTROLLABLE_ASSETS,
    default=[],
    help="아무것도 선택하지 않으면 반포자이 실거래가만 보입니다.",
)

fit_results: dict[str, dict | None] = {}
display_mode: dict[str, str] = {}

if selected_assets:
    st.caption(
        "선택한 자산마다 λ(거듭제곱 변환)·래그(개월)·제외구간을 조절할 수 있습니다. 차트에는 "
        "그 자산의 '원 데이터'(변환 없는 원래 값 그대로) 또는 'λ 변환 데이터'(설정한 λ로 "
        "거듭제곱·로그 변환한 값) 중 고른 쪽이 각자 자신의 축(오른쪽 보조축)에 원래 단위 "
        "그대로 표시되고, 아래 R²도 그 선택을 그대로 따라갑니다 — '원 데이터'를 고르면 변환 "
        "없는 원값 기준 R², 'λ 변환 데이터'를 고르면 설정한 λ를 반영한 R²로 바뀌어, 토글만으로 "
        "λ 변환이 적합도를 얼마나 개선하는지 바로 비교할 수 있습니다(래그·제외구간은 두 경우 "
        "모두 동일하게 반영됩니다). λ는 0 이상의 값만 쓸 수 있습니다(음수 λ는 아래 시나리오 "
        "계산기에서 회귀식을 역으로 풀 때 해석하기 어려운 결과를 만들기 쉬워 제외했습니다). "
        "아래 시나리오 계산기는 이 토글과 무관하게 항상 λ 입력값을 그대로 씁니다."
    )
    cols = st.columns(len(selected_assets))
    for col, asset in zip(cols, selected_assets):
        with col:
            st.markdown(f"**{asset}**")
            lam = st.number_input(
                "λ", min_value=0.0, max_value=3.0, step=0.05,
                value=DEFAULT_LAMBDA[asset], key=f"lam_{asset}",
                help="0=로그, 1=원값, 0.5=제곱근, 1/3≈0.33=세제곱근 (0 이상만 가능)",
            )
            lag = st.number_input(
                "래그(개월)", min_value=-24, max_value=36, step=1,
                value=DEFAULT_LAG_MONTHS[asset], key=f"lag_{asset}",
            )
            display_mode[asset] = st.radio(
                "차트 표시", ["원 데이터", "λ 변환 데이터"],
                key=f"disp_{asset}", horizontal=False,
            )
            use_excl = st.checkbox("제외구간 사용", key=f"excl_on_{asset}")
            excl_start = excl_end = None
            if use_excl:
                excl_range = st.date_input(
                    "제외할 기간(소스월 기준)",
                    value=(date.today() - timedelta(days=30), date.today()),
                    key=f"excl_range_{asset}",
                )
                if isinstance(excl_range, tuple) and len(excl_range) == 2:
                    excl_start, excl_end = excl_range
            # 시나리오 계산기(아래)는 항상 "지금 입력한 λ" 기준으로 계산하므로,
            # 여기 fit_results는 차트 표시 토글과 무관하게 그 λ로 고정해 둔다.
            result = fit_and_r2(monthly, banpo_monthly, asset, float(lam), int(lag), excl_start, excl_end)
            fit_results[asset] = result
            # 화면에 보여줄 R²만 토글을 따라간다 — '원 데이터' 선택 시 λ=1(변환 없음)
            # 기준으로 다시 계산해, 토글만으로 "λ 변환이 적합도를 실제로 개선하는지"를
            # 바로 비교할 수 있게 한다.
            if display_mode[asset] == "λ 변환 데이터":
                display_result = result
            else:
                display_result = fit_and_r2(monthly, banpo_monthly, asset, 1.0, int(lag), excl_start, excl_end)
            if display_result is None:
                st.metric("R²", "표본 부족")
            else:
                st.metric("R²", f"{display_result['r2']:.3f}")
                st.caption(f"표본 {display_result['n']}개월")

st.subheader("지수 비교 차트")
show_regression_line = False
if selected_assets:
    show_regression_line = st.checkbox(
        "반포자이 회귀분석선 표시 (선택한 자산의 회귀식으로 역산한 반포자이 예측값)",
        value=False, key="show_regression_line",
        help="각 선택 자산의 λ·래그로 적합된 회귀식(기울기·절편)에 그 자산의 실제 값을 "
             "넣었을 때 나오는 '반포자이 예측값'을 반포자이와 같은 축에 점선으로 겹쳐 "
             "보여줍니다. 실제 반포자이 실거래 점들과 가까울수록 그 회귀식의 적합도(R²)가 "
             "높다는 뜻입니다.",
    )
fig = go.Figure()

banpo_base = banpo_raw_trend["trend"].dropna()
banpo_base_value = banpo_base.iloc[0] if not banpo_base.empty else None
if banpo_base_value:
    raw_idx = banpo_raw_trend["raw"] / banpo_base_value * 100.0
    fig.add_trace(go.Scatter(
        x=raw_idx.index, y=raw_idx.values, mode="markers", name="반포자이 실거래(개별)",
        marker=dict(size=4, color=COLORS[BENCHMARK], opacity=0.45),
        hovertemplate="%{x|%Y-%m-%d}<br>지수 %{y:.1f}<extra>반포자이 실거래</extra>",
    ))

# 비교 자산은 이제 지수(=100)로 정규화하지 않고 원래 단위(또는 λ 변환값)를
# 그대로 보여주므로, 여러 자산을 동시에 선택하면 서로 스케일이 완전히 달라
# 한 보조축을 같이 쓸 수 없다 — 선택한 자산마다 독립된 오른쪽 축(y2,y3,...)을
# 하나씩 배정하고, 두 번째 축부터는 anchor="free"로 플롯 영역 바깥쪽에
# 차례로 겹치지 않게 늘어놓는다(x축 도메인을 그만큼 왼쪽으로 줄여 자리를 만듦).
MARGIN_STEP = 0.08
n_assets = len(selected_assets)
right_edge = max(0.3, 1.0 - MARGIN_STEP * max(n_assets - 1, 0))
extra_axis_layout: dict[str, dict] = {}
overlap_candidates = [banpo_base.index.min()] if not banpo_base.empty else []

for i, asset in enumerate(selected_assets):
    lam = float(st.session_state[f"lam_{asset}"])
    lag = int(st.session_state[f"lag_{asset}"])
    if display_mode.get(asset) == "λ 변환 데이터":
        values = transform(monthly[asset].to_numpy(), lam)
        label = f"{asset}(λ={lam:g} 변환)"
        axis_title = f"{asset}(λ={lam:g} 변환값)"
    else:
        values = monthly[asset].to_numpy()
        label = asset
        axis_title = f"{asset}(원래 값)"
    series = pd.Series(values, index=monthly[asset].index)
    # R² 계산(fit_and_r2)은 asset[t-lag]를 반포자이[t]와 짝짓는다 — 그 "예측
    # 대상 시점(t)"에 맞춰 그리려면 asset 값을 실제 관측 시점보다 lag개월
    # 뒤로 밀어서 표시해야 한다. 즉 이 차트의 x축 위치는 그 자산의 실제
    # 관측 시점이 아니라, "그 값이 반포자이에 반영됐다고 가정하는 시점"이다.
    display_x = (series.index + lag).to_timestamp()
    if lag != 0:
        label += f", {lag:+d}개월 래그"

    first_valid_period = monthly[asset].dropna().index.min()
    if pd.notna(first_valid_period):
        overlap_candidates.append((first_valid_period + lag).to_timestamp())

    axis_num = i + 2
    trace_axis = f"y{axis_num}"
    layout_axis_key = f"yaxis{axis_num}"
    fig.add_trace(go.Scatter(
        x=display_x, y=series.values, mode="lines", name=label,
        line=dict(color=COLORS.get(asset)),
        yaxis=trace_axis,
        hovertemplate="%{x|%Y-%m}<br>%{y:,.3g}<extra>" + label + "</extra>",
    ))
    axis_entry = dict(
        title=dict(text=axis_title, font=dict(color=COLORS.get(asset))),
        overlaying="y", side="right", showgrid=False,
        tickfont=dict(color=COLORS.get(asset)),
    )
    if i >= 1:
        axis_entry["anchor"] = "free"
        axis_entry["position"] = min(1.0, right_edge + MARGIN_STEP * i)
    extra_axis_layout[layout_axis_key] = axis_entry

    if show_regression_line and fit_results.get(asset) is not None and banpo_base_value:
        # 회귀식(fit_and_r2가 구한 slope/intercept)에 이 자산의 실제 값(source_month
        # 기준, 반포자이[t]와 짝지어진 그 x)을 넣어 "반포자이 예측값"을 구하고, 반포자이
        # 자신의 다른 트레이스와 비교할 수 있도록 같은 지수(=100) 기준·주축에 그린다.
        res = fit_results[asset]
        source_month = monthly.index - lag
        asset_vals = monthly[asset].reindex(source_month).to_numpy()
        x_transformed = transform(asset_vals, lam)
        predicted_log = res["intercept"] + res["slope"] * x_transformed
        predicted_raw = np.exp(predicted_log)
        predicted_idx = predicted_raw / banpo_base_value * 100.0
        pred_series = pd.Series(predicted_idx, index=monthly.index)
        reg_label = f"반포자이 회귀선(vs {asset})"
        fig.add_trace(go.Scatter(
            x=pred_series.index.to_timestamp(), y=pred_series.values, mode="lines",
            name=reg_label,
            line=dict(color=COLORS.get(asset), dash="dot", width=1.5),
            hovertemplate="%{x|%Y-%m}<br>지수 %{y:.1f}<extra>" + reg_label + "</extra>",
        ))

_first_jan1 = pd.Timestamp(year=max(banpo_raw_trend.index.min().year, 2005), month=1, day=1)
xaxis_kwargs = dict(
    tickformat="%Y", dtick="M12", tick0=_first_jan1.isoformat(),
    rangeslider=dict(visible=True),
    rangeselector=dict(buttons=[
        dict(count=3, label="3년", step="year", stepmode="backward"),
        dict(count=5, label="5년", step="year", stepmode="backward"),
        dict(count=10, label="10년", step="year", stepmode="backward"),
        dict(step="all", label="전체"),
    ]),
)
if n_assets >= 2:
    xaxis_kwargs["domain"] = [0.0, right_edge]
if overlap_candidates:
    # 선택한 자산(들)과 반포자이가 "둘 다" 값을 가진 가장 이른 날짜를 차트의
    # 기본(최초) 보이는 구간의 시작일로 잡는다 — 그래야 처음 열었을 때부터
    # 모든 선이 공백 없이 보인다. "전체" 버튼을 누르면 이 제한 없이 데이터
    # 전체 기간을 볼 수 있다(아래 범위는 기본 보기에만 적용, 데이터 자체를
    # 자르지 않는다).
    overlap_start = max(overlap_candidates)
    all_x = [x for tr in fig.data for x in pd.to_datetime(tr.x)] if fig.data else []
    data_max = max(all_x) if all_x else None
    if data_max is not None and overlap_start < data_max:
        xaxis_kwargs["range"] = [overlap_start.isoformat(), data_max.isoformat()]
fig.update_xaxes(**xaxis_kwargs)
layout_kwargs = dict(
    height=460, margin=dict(t=60, b=10, r=10 + 60 * max(n_assets - 1, 0)), hovermode="closest",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    yaxis=dict(title="반포자이 지수(첫 유효월=100)"),
)
layout_kwargs.update(extra_axis_layout)
fig.update_layout(**layout_kwargs)
render_zoomable_chart(fig, height=460)
st.caption(
    "하단 슬라이더로 구간을 드래그해 확대, 상단 버튼으로 빠른 기간 이동이 가능합니다 — "
    "확대하면 모든 Y축이 각자 화면에 보이는 구간의 데이터에 맞춰 독립적으로 다시 "
    "스케일됩니다. 반포자이(왼쪽 축, 첫 유효월=100 기준 지수)와 비교 자산은 변동폭"
    "(스케일)이 크게 달라 같은 축을 쓰면 한쪽이 거의 평평해 보이므로, 비교 자산마다 "
    "오른쪽에 자신만의 축을 따로 둡니다(둘 이상 선택하면 축이 여러 개 나란히 붙습니다). "
    "그 축에는 '원 데이터'를 골랐으면 그 자산의 원래 값(원·포인트 등 단위 그대로), "
    "'λ 변환 데이터'를 골랐으면 설정한 λ로 거듭제곱(또는 λ=0이면 로그) 변환한 값이 "
    "그대로 표시됩니다 — 더 이상 100 기준으로 지수화하지 않으므로, 자산별 축의 눈금"
    "(숫자)이 서로 달라도 정상입니다. 래그(개월)를 설정한 자산은 그 선 자체를 래그만큼 "
    "뒤로(미래 방향으로) 밀어서 그립니다 — 반포자이가 그 자산을 뒤늦게 따라가는 경우가 "
    "많아, 예를 들어 래그=6이면 '그 자산의 어떤 시점 값'을 '그로부터 6개월 뒤'의 x축 "
    "위치에 표시해 반포자이의 같은 시점 움직임과 나란히 비교할 수 있게 합니다(R² 계산과 "
    "동일한 대응 관계). 즉 이 선의 x축 위치는 실제 관측 시점이 아니라 '반포자이에 "
    "반영됐다고 가정하는 시점'입니다. 차트를 처음 열면 선택한 자산(들)과 반포자이가 "
    "모두 값을 가진 가장 이른 시점부터 보여주며, 더 이전 기간까지 보려면 '전체' 버튼을 "
    "누르세요."
)

if selected_assets:
    st.subheader("특정 구간만의 R²")
    st.caption(
        "위 R²는 항상 전체 기간 기준입니다. 차트에서 지금 확대해서 보고 있는 기간(또는 "
        "임의의 구간)만으로 R²를 다시 보고 싶으면 아래에 그 기간을 직접 입력하세요 "
        "(λ·래그·제외구간은 위 설정을 그대로 쓰고, 반포자이 기준 시점으로 구간을 "
        "판단합니다)."
    )
    default_start = monthly.index.min().to_timestamp().date()
    default_end = monthly.index.max().to_timestamp().date()
    window_range = st.date_input(
        "구간", value=(default_start, default_end),
        min_value=default_start, max_value=default_end, key="r2_window_range",
    )
    if isinstance(window_range, tuple) and len(window_range) == 2:
        w_start, w_end = window_range
        cols_window = st.columns(len(selected_assets))
        for col, asset in zip(cols_window, selected_assets):
            with col:
                st.markdown(f"**{asset}**")
                lam = float(st.session_state[f"lam_{asset}"])
                lag = int(st.session_state[f"lag_{asset}"])
                effective_lam = lam if st.session_state.get(f"disp_{asset}") == "λ 변환 데이터" else 1.0
                w_excl_start = w_excl_end = None
                if st.session_state.get(f"excl_on_{asset}"):
                    rng = st.session_state.get(f"excl_range_{asset}")
                    if isinstance(rng, tuple) and len(rng) == 2:
                        w_excl_start, w_excl_end = rng
                window_result = fit_and_r2(
                    monthly, banpo_monthly, asset, effective_lam, lag,
                    w_excl_start, w_excl_end, w_start, w_end,
                )
                if window_result is None:
                    st.metric("R² (이 구간)", "표본 부족")
                else:
                    st.metric("R² (이 구간)", f"{window_result['r2']:.3f}")
                    st.caption(f"표본 {window_result['n']}개월")

st.divider()

st.subheader("시나리오 계산기 — 반포자이가 N년간 k배 오르면 다른 자산은?")
st.caption(
    "선택하지 않은 자산은 기본 λ·래그 값을 사용합니다. λ(거듭제곱 변환)·래그로 R²가 높게 "
    "맞춰졌다는 건, 그 변환·래그를 적용한 자산 값이 반포자이와 거의 같은 '비율'로 움직여 "
    "왔다는 뜻입니다. 그래서 '오늘의 실제 자산 가격'을 기준점으로 놓고, 반포자이가 목표 "
    "배수에 도달할 때 그 변환된 값이 과거와 같은 비율 관계(회귀 기울기)로 얼마나 더 움직여야 "
    "하는지를 구한 뒤, 거듭제곱을 반대로(예: 세제곱근을 변환으로 썼다면 다시 세제곱) 적용해 "
    "자산의 실제 가격으로 되돌립니다 — 예측이 아니라 '과거의 비율 관계를 오늘 가격에서부터 "
    "그대로 연장하면 나오는 숫자'일 뿐이며, 특히 관측 범위를 크게 벗어나는 추정은 신뢰도가 "
    "급격히 떨어집니다(아래 ⚠️ 표시 참고)."
)

scenario_cols = st.columns(3)
with scenario_cols[0]:
    fold_change = st.number_input("반포자이 목표 배수", min_value=0.1, max_value=20.0, value=2.0, step=0.1)
with scenario_cols[1]:
    horizon_years = st.number_input("기간(년)", min_value=1, max_value=30, value=5, step=1)
with scenario_cols[2]:
    investment_amount_eok = st.number_input(
        "다른 자산에 넣을 내 투자금액(억원)", min_value=0.0, value=1.0, step=0.01, format="%.2f",
        help="단위는 억원이며 소수점 입력이 가능합니다. 예: 0.1 입력 시 1,000만원. "
             "반포자이가 아니라 비교 자산 쪽에 지금 이 금액을 투자한다고 가정합니다.",
    )
    investment_amount = investment_amount_eok * 1e8

banpo_now = banpo_monthly.dropna().iloc[-1]
banpo_target = banpo_now * fold_change
banpo_cagr = fold_change ** (1.0 / horizon_years) - 1.0

st.markdown(
    f"현재 반포자이 추세가 **{banpo_now:,.0f}원** → {horizon_years}년 뒤 {fold_change:g}배인 "
    f"**{banpo_target:,.0f}원**이 된다고 가정합니다(연복리 {banpo_cagr:.1%})."
)

Z_95 = 1.96  # 정규근사 95% 구간(표본이 보통 100개월 이상이라 t분포와 거의 같음)


def invert_x_with_interval(
    result: dict, asset_now: float, lam: float, banpo_now: float, banpo_target: float, z: float = Z_95,
):
    """R²가 높다는 건 transform(자산,λ)(래그 반영)이 log(반포자이)와 거의 선형
    으로 같이 움직여 왔다는 뜻이다 — 그래서 "반포자이가 목표 배수가 되면, 그
    자산의 변환된 값도 같은 비율 관계(회귀 기울기)만큼 더 움직였을 것"이라고
    보고, '오늘의 실제 자산 가격'(x_now = transform(asset_now,λ))을 기준점으로
    놓은 뒤, 반포자이의 log 변화량(log(banpo_target)-log(banpo_now))을 회귀
    기울기로 나눠 자산의 transform값이 거기서 얼마나 더 움직여야 하는지 구한다
    (x_point = x_now + Δlog(반포자이)/slope). 회귀식을 절편까지 포함해 통째로
    역으로 푸는 대신 이렇게 "오늘 가격에서 시작한 상대적 이동"으로 계산하는
    이유: 절편을 쓰면 "오늘의 실제 자산 가격이 과거 표본 대비 이미 얼마나
    앞서/뒤처져 있는지"까지 함께 반영돼, 최근 단기간 급등한 자산에서 "목표
    배수보다 오히려 하락해야 한다" 같은 반직관적인 결과가 나오기 쉽다(예:
    최근 AI 랠리로 급등한 하이닉스·코스피). 잔차 산포·표본 크기·(x의 관측
    평균에서 얼마나 떨어져 있는지=외삽 정도)를 반영한 근사 예측구간은 기존과
    같은 공식(margin = z·s·sqrt(1+1/n+(x-x̄)²/Sxx))을 이 점추정 x에서 평가해서
    구한다. (x_point, x_lo, x_hi) 반환 — x_lo/x_hi는 음수(=이 λ 변환에서 도달
    불가능한 영역)일 수 있으며 호출부에서 그 경우를 따로 처리해야 한다."""
    slope = result["slope"]
    x_now = float(transform(np.array([asset_now]), lam)[0])
    y_now = np.log(banpo_now)
    y_target = np.log(banpo_target)
    x_point = x_now + (y_target - y_now) / slope
    se, n, x_mean, sxx = result["resid_se"], result["n"], result["x_mean"], result["sxx"]
    if not np.isfinite(se) or sxx <= 0:
        return x_point, None, None
    margin_y = z * se * np.sqrt(1 + 1.0 / n + (x_point - x_mean) ** 2 / sxx)
    y_lo, y_hi = y_target - margin_y, y_target + margin_y
    x_a = x_now + (y_lo - y_now) / slope
    x_b = x_now + (y_hi - y_now) / slope
    return x_point, min(x_a, x_b), max(x_a, x_b)  # slope<0이면 뒤집히므로 min/max로 정렬


rows = []
for asset in CONTROLLABLE_ASSETS:
    effective_lam = float(st.session_state.get(f"lam_{asset}", DEFAULT_LAMBDA[asset]))
    effective_lag = int(st.session_state.get(f"lag_{asset}", DEFAULT_LAG_MONTHS[asset]))
    result = fit_results.get(asset) or fit_and_r2(monthly, banpo_monthly, asset, effective_lam, effective_lag, None, None)
    asset_now_series = monthly[asset].dropna()
    asset_now = asset_now_series.iloc[-1] if not asset_now_series.empty else None
    if result is None or asset_now is None or abs(result["slope"]) < 1e-12:
        rows.append({
            "자산": asset, "R²": "-", "함의 가격": "-", "현재 대비": "-", "연복리(CAGR)": "-",
            "반포자이 대비": "-", "투자금액 → 예상액(억원, 95% 구간)": "-", "비고": "회귀 불가(표본 부족)",
        })
        continue
    x_required, x_lo, x_hi = invert_x_with_interval(result, asset_now, effective_lam, banpo_now, banpo_target)
    if x_required <= 0:
        rows.append({
            "자산": asset, "R²": f"{result['r2']:.3f}", "함의 가격": "-", "현재 대비": "-",
            "연복리(CAGR)": "-", "반포자이 대비": "-", "투자금액 → 예상액(억원, 95% 구간)": "-",
            "비고": "회귀식상 불가능(음수)",
        })
        continue
    asset_required = float(inverse_transform(np.array([x_required]), effective_lam)[0])
    asset_fold = asset_required / asset_now
    asset_cagr = asset_fold ** (1.0 / horizon_years) - 1.0
    relative_fold = asset_fold / fold_change
    out_of_range = asset_required > result["asset_max"] * 1.5 or asset_required < result["asset_min"] * 0.67

    # x_lo/x_hi(또는 둘 다 음수) → 95% 구간이 그 λ 변환에서 아예 도달 불가능한
    # 쪽은 "N/A(불가능)"로 표시 — 점추정 자체가 불가능한 경우(위에서 continue)와
    # 같은 종류의 음수-불가 상황이, 구간의 한쪽 끝에서만 생길 수도 있다.
    if x_lo is None:
        invest_label = "구간 추정 불가(표본 부족 또는 변동 없음)"
    else:
        def _invest_value(x_val):
            if x_val <= 0:
                return None
            req = float(inverse_transform(np.array([x_val]), effective_lam)[0])
            return investment_amount * (req / asset_now)

        invest_lo, invest_hi = _invest_value(x_lo), _invest_value(x_hi)
        if invest_lo is None and invest_hi is None:
            invest_label = "구간 전체가 불가능(λ 변환상 음수)"
        else:
            lo_text = f"{invest_lo / 1e8:,.2f}억원" if invest_lo is not None else "0억원 미만(불가능)"
            hi_text = f"{invest_hi / 1e8:,.2f}억원" if invest_hi is not None else "상한 없음"
            invest_label = f"{lo_text} ~ {hi_text}"

    rows.append({
        "자산": asset,
        "R²": f"{result['r2']:.3f}",
        "함의 가격": f"{asset_required:,.0f}" + ("⚠️" if out_of_range else ""),
        "현재 대비": f"{asset_fold:.2f}배",
        "연복리(CAGR)": f"{asset_cagr:.1%}",
        "반포자이 대비": f"{relative_fold:.2f}배 " + ("오버퍼폼" if relative_fold >= 1 else "언더퍼폼"),
        "투자금액 → 예상액(억원, 95% 구간)": invest_label,
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
    "'투자금액 → 예상액(억원, 95% 구간)'은 입력한 투자금액을 지금 이 자산에 넣었을 때, 반포자이가 "
    "목표 배수에 도달하는 시점 기준으로 얼마가 될지를 95% 예측구간으로 보여줍니다 — 점추정(함의 "
    "가격)만 보여주면 실제로는 그 회귀식 주변에 흩어져 있던 과거 달(월)들의 산포를 숨기게 되므로, "
    "잔차 표준오차·표본 수·현재 추정치가 관측 데이터 평균에서 얼마나 떨어져 있는지(외삽 정도)를 "
    "반영한 표준적인 예측구간 공식으로 범위를 계산합니다. 외삽이 심할수록(관측 범위를 크게 벗어날"
    "수록) 구간이 급격히 넓어지며, '상한 없음'·'불가능'은 95% 구간의 그 끝이 이 λ 변환에서 "
    "수학적으로 도달할 수 없는 영역(§5.8의 '회귀식상 불가능'과 같은 이유)에 해당한다는 뜻입니다."
)

st.caption(
    "⚠️ 전체 유의사항: 이 페이지의 모든 R²·시나리오 수치는 상관관계 탐색이며 인과관계를 "
    "뜻하지 않습니다. 표본 기간(최대 17년)에 완결된 사이클이 몇 번 되지 않아 특정 λ·래그에서 "
    "높은 R²가 나와도 과최적화(overfitting)일 가능성을 배제할 수 없습니다. 투자 판단의 "
    "근거로 사용하는 것은 권장하지 않습니다."
)

"""Streamlit dashboard: gold price correlation table.

For today's date, reads the pre-computed data/latest.json (refreshed daily at
07:00 KST by the GitHub Actions workflow in
.github/workflows/update_dashboard_data.yml) so the page loads instantly.
For any other selected date, computes the table live as of that date
(requires network access and FRED_API_KEY).
"""

import json
from datetime import date
from pathlib import Path

import streamlit as st

DATA_PATH = Path(__file__).resolve().parent / "data" / "latest.json"
EARLIEST_DATE = date(1990, 1, 1)

st.set_page_config(page_title="금(Gold) 상관관계 대시보드", layout="wide")


@st.cache_data(ttl=3600, show_spinner="데이터를 불러오는 중입니다...")
def load_data(selected_date_iso: str, is_today: bool):
    if is_today and DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))

    from gold_dashboard.build_table import build

    as_of = None if is_today else date.fromisoformat(selected_date_iso)
    return build(as_of=as_of)


st.title("금(Gold) 상관관계 대시보드")

today = date.today()
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
    "🟢 옅은 녹색 배경 = 그 신호가 현재 금값에 우호적인 방향인 셀입니다. "
    "정방향 지표(WTI·VIX)는 종가가 이평선 위일 때, 역방향 지표(실질금리·달러인덱스)는 "
    "종가가 이평선 아래일 때 초록색으로 표시되며, 금/은비율은 이평선 상향 돌파 여부를 그대로 표시합니다. "
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

st.markdown("#### 지표별 참고 출처")
for k in indicator_order:
    st.caption(f"**{indicators[k]['label']}** — {data['footnotes'][k]}")

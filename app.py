"""Streamlit dashboard: gold price correlation table.

Reads the pre-computed data/latest.json (refreshed daily at 07:00 KST by the
GitHub Actions workflow in .github/workflows/update_dashboard_data.yml) so the
page loads instantly. Falls back to a live fetch if that file is missing, and
offers a manual "실시간 재계산" button for local testing.
"""

import json
from pathlib import Path

import streamlit as st

DATA_PATH = Path(__file__).resolve().parent / "data" / "latest.json"

st.set_page_config(page_title="금(Gold) 상관관계 대시보드", layout="wide")


@st.cache_data(ttl=3600)
def load_data(force_live: bool):
    if not force_live and DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    from gold_dashboard.build_table import build

    return build()


st.title("금(Gold) 상관관계 대시보드")

_, refresh_col = st.columns([4, 1])
with refresh_col:
    force_live = st.button("실시간 재계산", use_container_width=True)

if force_live:
    st.cache_data.clear()

data = load_data(force_live)

st.caption(
    f"기준일(전일 미국장 마감 종가): **{data['as_of']}**  ·  생성시각(KST): {data['generated_at']}"
)
st.caption(
    "⚠️ 실시간 시세가 아닙니다. 원자재·금리 데이터는 대부분 일봉(전일 확정 종가) 기준이며, "
    "이 표는 매일 아침 7시(KST)에 자동 갱신됩니다."
)
st.caption("🟢 옅은 녹색 배경 = 해당 이동평균선을 상향 돌파한 후 유지 중인 셀")

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


rows_html = []

header_row = header_cell("구성") + "".join(
    header_cell(indicators[k]["label"], tooltip=indicators[k]["source"]) for k in indicator_order
)
rows_html.append(f"<tr>{header_row}</tr>")

for row_name in data["row_order"]:
    cells = "".join(data_cell(data["static_rows"][row_name][k]) for k in indicator_order)
    rows_html.append(f"<tr>{header_cell(row_name)}{cells}</tr>")

for window in data["ma_windows"]:
    cells = "".join(
        data_cell(
            indicators[k]["sma"][str(window)]["display"],
            highlight=indicators[k]["sma"][str(window)]["breakout"],
        )
        for k in indicator_order
    )
    rows_html.append(f"<tr>{header_cell(f'{window}일선 돌파지속')}{cells}</tr>")

close_cells = "".join(data_cell(indicators[k]["prev_close"]["display"]) for k in indicator_order)
rows_html.append(f"<tr>{header_cell('전일종가')}{close_cells}</tr>")

table_html = (
    '<table style="border-collapse:collapse;width:100%;font-size:14px">'
    + "".join(rows_html)
    + "</table>"
)
st.markdown(table_html, unsafe_allow_html=True)

st.markdown("#### 지표별 참고 출처")
for k in indicator_order:
    st.caption(f"**{indicators[k]['label']}** — {data['footnotes'][k]}")

"""Static configuration for the gold correlation dashboard: indicator metadata,
fixed reference text (structure/meaning/correlation direction), and footnotes."""

INDICATOR_ORDER = ["real_rate", "dxy", "gold_silver_ratio", "wti", "vix"]

MA_WINDOWS = [60, 30, 5]

# The backtest's default gold/silver-ratio buy threshold (immediate-buy trigger)
# and the main dashboard's chart-shading threshold must always agree, so both
# gold_dashboard/backtest.py and app.py import this single constant instead of
# each hardcoding their own copy. Deliberately more conservative than the
# academic 80 threshold cited in STATIC_ROWS below — that 80 is a general
# reference value from the literature, not this dashboard's trading rule.
DEFAULT_GS_RATIO_BUY_THRESHOLD = 100.0

INDICATOR_META = {
    "real_rate": {
        "label": "실질금리",
        "source": "FRED DFII10",
        "unit": "%",
        "decimals": 2,
    },
    "dxy": {
        "label": "달러인덱스",
        "source": "Yahoo Finance (DX-Y.NYB)",
        "unit": "",
        "decimals": 2,
    },
    "gold_silver_ratio": {
        "label": "금/은비율",
        "source": "yfinance GC=F / SI=F",
        "unit": "",
        "decimals": 1,
    },
    "wti": {
        "label": "WTI",
        "source": "FRED DCOILWTICO",
        "unit": "$",
        "decimals": 2,
    },
    "vix": {
        "label": "VIX",
        "source": "Yahoo Finance (^VIX)",
        "unit": "",
        "decimals": 2,
    },
}

STATIC_ROWS = {
    "구조": {
        "real_rate": "미국 10년 국채금리 − 10년 예상 물가상승률",
        "dxy": "6개국 통화 바스켓 내 달러 비율",
        "gold_silver_ratio": "금가격 / 은가격 비율",
        "wti": "WTI 가격",
        "vix": "S&P500 옵션 내재변동성지수",
    },
    "의미": {
        "real_rate": "금 보유의 기회비용을 결정하는 핵심 변수 — 실질금리 상승 시 무이자자산인 금의 상대적 매력이 감소함",
        "dxy": "달러 표시 자산인 금의 역내 구매력에 영향 — 달러 강세 시 비달러권의 실물 수요가 위축되는 경로로 작용함",
        "gold_silver_ratio": "은은 금 대비 산업재 수요 민감도가 높아 하락 국면에서 상대 변동성이 확대됨",
        "wti": "원자재발 인플레이션 기대를 매개로 금의 인플레이션 헤지 수요에 영향을 미침",
        "vix": "시장의 위험회피 심리를 나타내는 변동성 지표로, 금의 안전자산 수요와 연동되는 경향이 있음",
    },
    "상관관계 방향": {
        "real_rate": "역상관 (기회비용 가설)",
        "dxy": "역상관 (구조적 음(-)의 상관)",
        "gold_silver_ratio": (
            "임계값(80) 초과 시 하락 반전 가능성 — 기술적 신호로, 통계적 인과관계는 아님. "
            f"참고: 본 대시보드의 매수신호 임계값({DEFAULT_GS_RATIO_BUY_THRESHOLD:g})은 위 "
            "학술적 관행값(80)보다 보수적으로 설정된 값으로, 서로 다른 목적의 수치입니다."
        ),
        "wti": "정상관 (단기 효과 중심)",
        "vix": "약한 정상관 — 조건부이며 최근 표본에서 효과 약화 추세",
    },
}

ROW_ORDER = ["구조", "의미", "상관관계 방향"]

# Machine-readable version of the "상관관계 방향" row above, used to decide what
# counts as "gold-friendly" for MA-row highlighting:
# - "positive": indicator rising above its MA is gold-friendly (WTI, VIX)
# - "inverse": indicator falling below its MA is gold-friendly (real rate, DXY)
# - "threshold": not a directional signal (gold/silver ratio) — keeps its own
#   existing highlight logic (highlighted while above its MA), unchanged
CORRELATION_DIRECTION = {
    "real_rate": "inverse",
    "dxy": "inverse",
    "gold_silver_ratio": "threshold",
    "wti": "positive",
    "vix": "positive",
}

FOOTNOTES = {
    "real_rate": (
        "Erb & Harvey (2013), <i>The Golden Dilemma</i>, Financial Analysts Journal — "
        "저자들은 표본 기간에 따라 이 상관관계가 약해질 수 있음을 명시함."
    ),
    "dxy": (
        "World Gold Council 분석 자료 — 구조적 역상관 관계(상관계수 약 -0.5~-0.8 수준)로 보고됨."
    ),
    "gold_silver_ratio": (
        "Solt & Swanson (1981); CME Group Research (2021) — 80은 경험적 기술적 임계값이며 "
        "절대적 기준은 아님."
    ),
    "wti": (
        "Sari, Hammoudeh & Soytas (2010), <i>Energy Economics</i> — 단기 정(+)의 효과만 확인되었고, "
        "장기 공적분 관계는 발견되지 않음 (신뢰도 낮음)."
    ),
    "vix": (
        "Baur & Lucey (2010) — 최근 연구에서는 금의 안전자산 효과가 약화되는 추세로 지적됨."
    ),
}

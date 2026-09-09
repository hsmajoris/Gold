"""Static configuration for the gold correlation dashboard: indicator metadata,
fixed reference text (structure/meaning/correlation direction), and footnotes."""

INDICATOR_ORDER = ["real_rate", "dxy", "gold_silver_ratio", "wti", "vix"]

MA_WINDOWS = [60, 30, 5]

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
        "real_rate": "기회비용의 요인",
        "dxy": "달러가치 상승 시 해외에서 금 수요 악화",
        "gold_silver_ratio": "귀금속 하락 시 은이 금보다 변동성이 큼",
        "wti": "물가 상승 요인",
        "vix": "위험회피 심리 지표",
    },
    "상관관계 방향": {
        "real_rate": "역방향",
        "dxy": "역방향",
        "gold_silver_ratio": "80 이상이면 하락 가능성(기술적 하락)",
        "wti": "정방향",
        "vix": "약한 정방향(조건부, 최근 효과 약화 추세)",
    },
}

ROW_ORDER = ["구조", "의미", "상관관계 방향"]

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

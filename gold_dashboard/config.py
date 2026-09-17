"""Static configuration for the gold correlation dashboard: indicator metadata,
fixed reference text (structure/meaning/correlation direction), and footnotes."""

from datetime import date

INDICATOR_ORDER = ["real_rate", "dxy", "wti", "vix"]

# Calendar-day (역일) windows, not trading-day counts — metrics.compute_sma
# averages every observation within the trailing N calendar days, whatever
# number of trading days that happens to contain. Standardized to the
# 일주일(week)/한달(month)/세달(quarter) units used throughout this project.
MA_WINDOWS = [60, 20, 7]

# Indicators that actually feed a real buy/sell trigger (the real_rate+dxy
# green_count condition). WTI and VIX are reference-only — never used in any
# buy/sell trigger — so anything that highlights "this signal currently favors
# gold" (main table row highlighting in build_table.py, main dashboard chart
# shading in app.py) must never mark them, regardless of their own MA-crossing
# state.
GREEN_COUNT_SIGNAL_INDICATORS = {"real_rate", "dxy"}

# Which price series the backtest ("유효성 검증") page's entire pipeline uses for
# gold itself — a single shared session_state key so the choice is one piece of
# state no matter which page renders the control. "intl" (default) is GC=F, the
# same series the app has always used. "krx" swaps in KRX's actual domestic
# gold-spot market (04020000, "금 99.99_1kg", quoted in KRW/gram) fetched from
# Naver's aggregation API — an apples-to-apples real quote, not a USD price
# multiplied by an exchange rate. The main dashboard's own table/charts are
# deliberately NOT affected by this choice — see gold_dashboard/data_sources.py
# and gold_dashboard/timeseries.py.
GOLD_PRICE_BASIS_INTL = "intl"
GOLD_PRICE_BASIS_KRX = "krx"
# ③~⑥ 고려아연/미래에셋증권/현대차/코스피200(KODEX 200 ETF) — 금 자체가 아니라,
# 유효성 검증 페이지에서만 선택 가능한 "대리 자산(proxy)" 옵션. green_count·
# 52주 신고가/신저가·노이즈 필터 등 신호 로직은 기존과 완전히 동일하게
# real_rate/dxy·"gold" 컬럼만 보고 동작하므로, 이 옵션들은 단지 그 "gold"
# 컬럼에 무엇을 채워 넣는지(매수·매도 대상 가격)만 바꾼다 — 대시보드 메인
# 화면은 항상 GOLD_PRICE_BASIS_DEFAULT(현재 KRX)로 고정 계산되므로 영향받지
# 않는다. 고려아연은 그나마 원자재(비철금속) 관련 종목이지만, 미래에셋증권
# (금융업)·현대차(자동차업)는 금 시세와 아무 연관도 없는 종목을, 코스피200은
# 개별종목이 아닌 시장 전체를 일부러 골라 "이 신호가 아무 종목·지수에나
# 통하는 우연인지, 아니면 뭔가 더 있는지"를 대조해보기 위한 순수 placebo
# 대조군이다.
GOLD_PRICE_BASIS_KOREA_ZINC = "korea_zinc"
GOLD_PRICE_BASIS_MIRAE_ASSET = "mirae_asset"
GOLD_PRICE_BASIS_HYUNDAI_MOTOR = "hyundai_motor"
GOLD_PRICE_BASIS_KOSPI200 = "kospi200"
GOLD_PRICE_BASIS_STATE_KEY = "gold_price_basis"
GOLD_PRICE_BASIS_LABELS = {
    GOLD_PRICE_BASIS_INTL: "① 국제 금 시세 (USD/oz, GC=F)",
    GOLD_PRICE_BASIS_KRX: "② KRX 금현물 (KRW/g, 실제 국내 시세)",
    GOLD_PRICE_BASIS_KOREA_ZINC: "③ 고려아연 (KOSPI 010130, 대리 자산)",
    GOLD_PRICE_BASIS_MIRAE_ASSET: "④ 미래에셋증권 (KOSPI 006800, 무관 대조군)",
    GOLD_PRICE_BASIS_HYUNDAI_MOTOR: "⑤ 현대차 (KOSPI 005380, 무관 대조군)",
    GOLD_PRICE_BASIS_KOSPI200: "⑥ 코스피200 (KODEX 200 ETF, 069500, 시장 전체 대조군)",
}
# What a fresh session (and any caller that doesn't specify gold_price_basis
# explicitly) starts on.
GOLD_PRICE_BASIS_DEFAULT = GOLD_PRICE_BASIS_KRX

# KRX's gold-spot market (04020000) opened on this date — no earlier data exists
# at the source, so any analysis window under GOLD_PRICE_BASIS_KRX is clamped to
# not start before it (see timeseries.gold_window_would_clamp_to_krx).
KRX_GOLD_TICKER = "M04020000"
KRX_GOLD_EARLIEST_DATE = date(2014, 3, 24)

# ③④⑤의 KOSPI 상장 보통주 티커 — basis 값으로 바로 룩업할 수 있게 딕셔너리로
# 관리(timeseries.fetch_gold_price_series가 이 딕셔너리에 있는 basis는 전부
# 동일한 방식(yfinance 종가)으로 처리하므로, 새 대리종목을 추가할 때 여기에
# 한 줄만 더하면 된다). 개별 보통주는 매도 시 증권거래세가 붙는다(아래 참고).
KOREA_ZINC_TICKER = "010130.KS"
MIRAE_ASSET_TICKER = "006800.KS"
HYUNDAI_MOTOR_TICKER = "005380.KS"
KOREA_STOCK_PROXY_TICKERS = {
    GOLD_PRICE_BASIS_KOREA_ZINC: KOREA_ZINC_TICKER,
    GOLD_PRICE_BASIS_MIRAE_ASSET: MIRAE_ASSET_TICKER,
    GOLD_PRICE_BASIS_HYUNDAI_MOTOR: HYUNDAI_MOTOR_TICKER,
}
# ⑥ 코스피200 자체는 yfinance에 역사적 데이터가 없는 지수(^KS200, 최근 1일치만
# 존재)라, 대신 그 지수를 그대로 추종하는 가장 오래되고 유동성 큰 ETF인
# KODEX 200(069500, 2007년 상장)의 실제 시장가격을 쓴다 — 실제로 거래 가능한
# 가격이라는 점에서 오히려 더 현실적이다. ETF는 국내 세법상 증권거래세가
# 면제되므로(개별 보통주와 다름) 별도 딕셔너리로 분리했다.
KOSPI200_ETF_TICKER = "069500.KS"
KOREA_ETF_PROXY_TICKERS = {
    GOLD_PRICE_BASIS_KOSPI200: KOSPI200_ETF_TICKER,
}
# 가격 수집·look-ahead bias 보정·배당 조회처럼 "개별주 vs ETF" 구분이 필요 없는
# 공통 로직에서 쓰는 합집합.
ALL_KOSPI_PROXY_TICKERS = {**KOREA_STOCK_PROXY_TICKERS, **KOREA_ETF_PROXY_TICKERS}
# 금과 의도적으로 무관한 종목/지수만 모은 부분집합(고려아연은 그나마 원자재
# 관련이라 제외) — UI가 "이건 순수 placebo 대조군입니다" 문구를 추가로 보여줄
# 대상을 판단하는 데만 쓰인다.
GOLD_PRICE_BASIS_PLACEBO_SET = {
    GOLD_PRICE_BASIS_MIRAE_ASSET,
    GOLD_PRICE_BASIS_HYUNDAI_MOTOR,
    GOLD_PRICE_BASIS_KOSPI200,
}

# ③④ 공통 — KRX 금현물과는 완전히 다른 수수료·세금 구조(코스피 주식 매매)를
# 쓰므로 별도로 분리했다. 고려아연·미래에셋증권 둘 다 같은 코스피 보통주라
# 수수료 구조 자체는 동일(이 두 상수를 공유).
# 매매수수료(위탁수수료): 매수·매도 각각 부과, 온라인 기준 낮은 요율을 기본값으로
# 잡되 사용자가 화면에서 직접 조정 가능(증권사·거래 채널별로 차이가 큼).
KOREA_STOCK_DEFAULT_BROKERAGE_FEE_PCT = 0.015
# 증권거래세 + 농어촌특별세: 코스피 상장주식은 매도 체결 시에만 부과(매수 시엔
# 없음). 세율은 세법 개정으로 바뀔 수 있어 이 상수 하나로만 관리 — 2026-01
# 기준 거래세 0.05% + 농특세 0.15% = 0.2%.
KOREA_STOCK_SECURITIES_TRANSACTION_TAX_PCT = 0.2
# 배당소득세(15.4% = 소득세 14% + 지방소득세 1.4%) — 배당금 반영 옵션에서 "세후"를
# 선택했을 때만 적용되는 별도 토글용 상수(기본은 세전 배당금 그대로 가산).
DIVIDEND_INCOME_TAX_PCT = 15.4

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
        "wti": "WTI 가격",
        "vix": "S&P500 옵션 내재변동성지수",
    },
    "의미": {
        "real_rate": "금 보유의 기회비용을 결정하는 핵심 변수 — 실질금리 상승 시 무이자자산인 금의 상대적 매력이 감소함",
        "dxy": "달러 표시 자산인 금의 역내 구매력에 영향 — 달러 강세 시 비달러권의 실물 수요가 위축되는 경로로 작용함",
        "wti": "원자재발 인플레이션 기대를 매개로 금의 인플레이션 헤지 수요에 영향을 미침",
        "vix": "시장의 위험회피 심리를 나타내는 변동성 지표로, 금의 안전자산 수요와 연동되는 경향이 있음",
    },
    "상관관계 방향": {
        "real_rate": "역상관 (기회비용 가설)",
        "dxy": "역상관 (구조적 음(-)의 상관)",
        "wti": "정상관 (단기 효과 중심)",
        "vix": "약한 정상관 — 조건부이며 최근 표본에서 효과 약화 추세",
    },
}

ROW_ORDER = ["구조", "의미", "상관관계 방향"]

# Machine-readable version of the "상관관계 방향" row above, used to decide what
# counts as "gold-friendly" for MA-row highlighting:
# - "positive": indicator rising above its MA is gold-friendly (WTI, VIX)
# - "inverse": indicator falling below its MA is gold-friendly (real rate, DXY)
CORRELATION_DIRECTION = {
    "real_rate": "inverse",
    "dxy": "inverse",
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
    "wti": (
        "Sari, Hammoudeh & Soytas (2010), <i>Energy Economics</i> — 단기 정(+)의 효과만 확인되었고, "
        "장기 공적분 관계는 발견되지 않음 (신뢰도 낮음)."
    ),
    "vix": (
        "Baur & Lucey (2010) — 최근 연구에서는 금의 안전자산 효과가 약화되는 추세로 지적됨."
    ),
}

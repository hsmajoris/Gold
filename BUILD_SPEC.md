# 빌드 스펙 — 금(Gold) 상관관계 대시보드

이 문서는 이 저장소를 한 줄도 보지 않고도 **동일한 아키텍처·동일한 동작**으로 다시 만들 수
있을 만큼 구체적으로 적는 구현 스펙입니다. "왜 만들었는지"는 `PROJECT_OVERVIEW.md`, "어떻게
쓰는지"는 `README.md`를 보세요 — 이 문서는 오직 **어떻게 짜는지**(아키텍처·정확한 공식과
기본값·데이터 소스·캐싱 전략·화면 구성·구현상 함정)만 다룹니다.

---

## 1. 프로젝트 개요 및 기술 스택

### 1.1 앱 구조

Streamlit **멀티페이지 앱**이되, Streamlit의 "classic" `pages/` 폴더 자동 인식 방식을 쓰지
않고 `app.py`의 `main()` 안에서 `st.navigation([...])`으로 **페이지를 명시적으로 등록**합니다.

```python
def main() -> None:
    st.set_page_config(page_title="금(Gold) 상관관계 대시보드", layout="wide")
    pages = st.navigation([
        st.Page(render_dashboard, title="대시보드", url_path="", default=True),
        st.Page("pages/1_백테스트.py", title="유효성 검증", url_path="백테스트"),
        st.Page("pages/2_자산비교.py", title="자산비교", url_path="자산비교"),
    ])
    pages.run()

main()
```

이렇게 하는 이유 두 가지:

- `st.set_page_config()`는 앱 전체에서 **딱 한 번만** 호출할 수 있어, 각 페이지 파일이
  각자 호출하면 충돌합니다 — `app.py` 한 곳에 모아둡니다.
- `url_path`를 파일명에서 자동으로 뽑는 옛 방식이 아니라 **직접 고정**해 둡니다
  (`""` = 루트, `"백테스트"`, `"자산비교"`) — 메인 대시보드는 콜러블(`render_dashboard`)로,
  나머지 두 페이지는 파일 경로 문자열로 등록합니다(콜러블로 등록된 페이지는 함수 호출 시
  실행되고, 문자열 경로로 등록된 페이지는 그 `.py` 파일을 모듈처럼 `exec`합니다).

### 1.2 기술 스택 (requirements.txt)

```
streamlit>=1.35
pandas>=2.0
yfinance>=0.2.40
requests>=2.31
tenacity>=8.2
altair>=5.0
plotly>=5.24
```

- **Streamlit**: UI 프레임워크. `st.cache_data`로 함수 결과 캐싱, `st.session_state`로
  페이지 간 상태 공유, `st.iframe`으로 커스텀 JS가 포함된 Plotly 차트 임베드.
- **pandas/numpy**: 시계열 정렬·리샘플링·회귀.
- **plotly**: 모든 인터랙티브 차트(`go.Figure`를 만들고 `fig.to_html()`로 직접 렌더링).
- **altair**: 메인 대시보드의 지표별 시계열 차트만 Altair로 그립니다(Plotly가 아님 — 이
  부분은 줌 연동이 필요 없어 `st.altair_chart`를 그대로 씁니다).
- **yfinance**: 달러인덱스·VIX·코스피 상장종목·비트코인(최근 구간)·하이닉스·코스피지수 등.
- **requests + tenacity**: 모든 외부 API 호출의 공통 재시도 로직(`stop_after_attempt(3)`,
  `wait_exponential(multiplier=5, min=5, max=10)`).

### 1.3 배포 아키텍처

- **Streamlit Community Cloud**에 배포 — `app.py`가 엔트리포인트.
- 일부 데이터는 **매일/매월 GitHub Actions가 미리 받아서 JSON으로 커밋**해 두고, 앱은 그
  캐시 파일을 읽기만 합니다(아래 §2 참고) — 느리거나(수백 번 API 호출) 돈이 드는(API 키)
  수집을 페이지 로드마다 라이브로 하지 않기 위함입니다.
- 비밀값(API 키)은 전부 **GitHub Actions 시크릿 + 환경변수**로만 전달하고, 코드에 하드코딩
  하거나 로그에 출력하거나 커밋하지 않습니다(§6 참고).

### 1.4 레포 레이아웃

```
app.py                                  st.navigation 등록 + 메인 대시보드 페이지 본문
gold_dashboard/
  __init__.py                           (비어 있음)
  config.py                             지표 메타데이터, 금 가격 기준(①~⑧) 상수, 수수료 상수
  data_sources.py                       외부 API/소스별 raw fetch 함수 (FRED/yfinance/Naver/
                                         blockchain.info/RTMS 캐시 읽기)
  timeseries.py                         fetch_raw_series/fetch_gold_price_series/
                                         fetch_backtest_frame — 백테스트·대시보드가 공유하는
                                         시계열 조합 레이어
  signals.py                            gold_friendly_vs_ma / compute_green_count — "신호가
                                         금에 우호적인가"의 단일 소스
  metrics.py                            compute_sma(역일 이동평균) / breakout_streak
  regime.py                             국면 구간표 + 참여율 계산 (대시보드 핵심요약·백테스트
                                         참여율 카드가 공유)
  backtest.py                           신호 상태기계(run_backtest)·CAGR 계산·시뮬레이션 전체
  build_table.py                        메인 대시보드 표 payload 조립(build())
  chart_utils.py                        범용 줌 가능 Plotly 차트 렌더러(자산비교 페이지가 사용)
  update_data.py                        CLI: data/latest.json 생성 (일 1회 GitHub Actions)
  update_banpo_data.py                  CLI: data/banpo_xi_trend.json 생성 (월 1회 GitHub Actions)
  timeutil.py                          today_kst() — Asia/Seoul 기준 "오늘"
pages/
  1_백테스트.py                          유효성 검증(백테스트) 페이지 — 가장 큰 파일
  2_자산비교.py                          자산군 비교 페이지
data/
  latest.json                           메인 대시보드 캐시(일간)
  banpo_xi_trend.json                   반포자이 실거래가 추세 캐시(월간)
.github/workflows/
  update_dashboard_data.yml             일 1회 data/latest.json 갱신
  update_banpo_data.yml                 월 1회 data/banpo_xi_trend.json 갱신
```

---

## 2. 데이터 소스 및 수집 전략

모든 네트워크 호출은 `gold_dashboard/data_sources.py`에 모여 있고, 다음 공통 재시도 데코레이터를
씁니다 (`tenacity`):

```python
_retry_network_call = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=5, min=5, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
```

즉 **최대 3회, 5~10초 지수 백오프**로 재시도하고 그래도 실패하면 마지막 예외를 다시 던집니다.

### 2.1 FRED (미국 연준 경제데이터)

- URL: `https://api.stlouisfed.org/fred/series/observations`
- 필요한 환경변수: **`FRED_API_KEY`**
- 쓰는 시리즈 ID:
  - `DFII10` — 실질금리(10년 TIPS 수익률)
  - `DCOILWTICO` — WTI 유가
  - `DEXKOUS` — 원/달러 현물환율 (비트코인 전체 역사 복원용, §5.6 참고)
  - (대시보드 핵심요약 문구가 참조하는 `REAINTRATREARAT10Y`/`TWEXBGSMTH`은 코드가 직접
    호출하지는 않고, 정적 텍스트의 출처로만 언급됨)
- `end` 파라미터로 특정 과거 시점까지만 조회 가능(히스토리컬 as-of 지원).

### 2.2 Yahoo Finance (yfinance 패키지)

- 티커 목록과 역할:
  - `DX-Y.NYB` / `^DXY` / `DX=F` (fallback 체인) — 달러인덱스
  - `^VIX` — VIX
  - `GC=F` — 국제 금 시세(①)
  - `M04020000`은 yfinance가 아니라 Naver API(§2.3)
  - `010130.KS`(고려아연, ③), `006800.KS`(미래에셋증권, ④), `005380.KS`(현대차, ⑤),
    `069500.KS`(KODEX 200 ETF, ⑥), `BTC-KRW`(비트코인, ⑦)
  - 자산비교 페이지: `000660.KS`(하이닉스), `^KS11`(코스피지수)
- `fetch_yfinance_close(tickers, start, end)`는 **티커 리스트**를 받아 순서대로 시도하고
  첫 번째로 성공한 것을 반환(폴백 체인, 예: DXY 세 후보).
- **함정**: `yf.Ticker(ticker).history(start=..., end=...)`를 호출할 때 `start`/`end`를
  둘 다 `None`으로 주면 yfinance는 기본값 `period="1mo"`로 조용히 최근 한 달치만 반환합니다
  — 항상 명시적 `start=date(...)`를 넘기세요.
- 배당: `fetch_yfinance_dividends(ticker)`는 `Ticker.dividends` 전체 히스토리를 그대로
  반환(기간 제한 없음) — 호출부가 알아서 가격 인덱스에 맞춰 reindex.

### 2.3 Naver 증권 API (KRX 금현물 미러)

- URL: `https://api.stock.naver.com/marketindex/metals/{ticker}/prices`
- 티커: `M04020000` (KRX 04020000, "금 99.99_1kg"을 KRW/g으로 변환해 제공)
- 인증 불필요, `page`/`pageSize`(최대 60) 파라미터로 페이지네이션.
- `fetch_krx_gold_krw_per_gram()`은 페이지가 빈 배열을 반환할 때까지 **역순으로 전체
  히스토리를 끝까지 페이징**합니다(최대 4000페이지 안전 상한).
- KRX 금현물시장 개장일: **2014-03-24**(`config.KRX_GOLD_EARLIEST_DATE`) — 이보다 앞선
  분석 기간을 요청하면 자동으로 이 날짜로 클램프됩니다.

### 2.4 blockchain.info (비트코인 2010년대 초반 USD 시세)

- URL: `https://api.blockchain.info/charts/market-price`
- 파라미터: `{"timespan": "all", "format": "json", "sampled": "false"}`
  — **`sampled=false`가 중요**: 기본값은 약 1,600포인트로 다운샘플링된 값을 주고,
  `sampled=false`라야 2009년부터의 완전한 일별 해상도(6,500여 포인트)를 받습니다.
- 인증 불필요. 응답은 `{"status": "ok", "values": [{"x": unix_timestamp, "y": usd_price}, ...]}`.
- **함정**: 유닉스 타임스탬프를 pandas Timestamp로 바꿀 때 `pd.Timestamp.utcfromtimestamp(x)`를
  쓰면 안 됩니다 — 최신 pandas에서 이 메서드가 **tz-aware(UTC)** Timestamp를 반환하도록
  바뀌어, FRED 쪽의 tz-naive 인덱스와 reindex할 때
  `TypeError: Cannot compare dtypes datetime64[us] and datetime64[us, UTC]`가 납니다.
  `pd.Timestamp(x, unit="s")`를 쓰면 tz-naive로 안전하게 만들어집니다.

### 2.5 국토교통부 RTMS (반포자이 아파트 실거래가)

- URL: `https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade`
- 필요한 환경변수: **`RTMS_API_KEY`** (FRED/data.go.kr과 별개의 서비스/키 — REB R-ONE API와도
  다름)
- 파라미터: `serviceKey`, `LAWD_CD`(법정동코드), `DEAL_YMD`("YYYYMM"), `numOfRows=2000`,
  `pageNo=1`. 응답은 XML.
- **이 API는 한 번에 딱 한 달치만 돌려줍니다** — 전체 역사(2008-12~현재, 200개월+)를 받으려면
  200번 이상 순차 호출해야 해서, 페이지 로드마다 라이브로 받는 건 불가능합니다 → **월 1회
  GitHub Actions 캐시**로 처리(아래 §2.7).
- 필터링 상수(`config.py`):
  - `BANPO_LAWD_CD = "11650"`(서초구), `BANPO_UMD_NM = "반포동"`, `BANPO_APT_NAME = "반포자이"`
    (부분 일치), `BANPO_EXCLUSIVE_AREA = "84.943"`(전용면적 ㎡, 문자열 그대로 비교)
  - `BANPO_FETCH_START_YM = "200812"`(입주월 + 안전마진, 실제 최초 거래는 2009-03-22)

### 2.6 look-ahead bias 보정 (공통 패턴)

이 레포 전체에서 반복되는 패턴: **데이터가 "발표/공표"되는 시점은 그 데이터가 가리키는
날짜보다 항상 늦다** — 신호 로직이 미래 정보를 쓰지 않도록 항상 발표 지연만큼 날짜를 밀어야
합니다.

| 데이터 | 밀어주는 양 | 이유 |
|---|---|---|
| 미국 지표(실질금리·달러인덱스) vs KST 기준 자산 | +1 캘린더일 | 미국 장마감(ET 16~17시)은 KST로 다음날 06~07시에야 확정 — KRX/KOSPI/비트코인 종가는 그보다 이른 시각 또는 같은 날 확정이라 날짜를 밀어야 같은 날짜로 조인해도 미래 정보 누출이 없음 (`timeseries.fetch_backtest_frame` 참고) |
| 반포자이 실거래 | +`BANPO_REPORTING_LAG_DAYS`(45일) | 부동산 실거래는 계약일로부터 **법정 30일 이내** 신고 — 30일 + 15일 안전마진 = 45일 뒤를 "확인 가능일"(`available_date`)로 기록 |

### 2.7 GitHub Actions 예약 캐시 (공통 패턴)

이 레포는 "라이브로 받기엔 느리거나 API 호출 수가 과한 데이터"를 **CLI 스크립트 + 예약
워크플로 + JSON 커밋**으로 캐싱합니다. 패턴은 항상 동일합니다:

1. `gold_dashboard/update_*.py`가 데이터를 전부 받아 `data/*.json`에 씀.
2. `.github/workflows/update_*.yml`이 cron으로 그 스크립트를 실행.
3. 변경이 있으면 `git commit` + `git push`(워크플로 자체 봇 커밋, `github-actions[bot]`).
4. 앱 쪽 코드는 네트워크 호출 없이 그 JSON 파일을 그냥 읽음 — 파일이 없으면(막 클론한
   직후, 첫 예약 실행 전) 명확한 `RuntimeError`로 "아직 캐시가 없다"를 알리고 그 기준만
   비활성화, 나머지는 정상 동작.

현재 이 패턴을 쓰는 두 파이프라인:

| | `update_dashboard_data.yml` | `update_banpo_data.yml` |
|---|---|---|
| 스케줄 | `0 22 * * *` (22:00 UTC = 07:00 KST, 매일) | `0 0 1 * *` (00:00 UTC, 매월 1일) |
| 실행 스크립트 | `python -m gold_dashboard.update_data` | `python -m gold_dashboard.update_banpo_data` |
| 필요 시크릿 | `FRED_API_KEY` | `RTMS_API_KEY` |
| 출력 파일 | `data/latest.json` | `data/banpo_xi_trend.json` |
| 왜 라이브 불가 | 매일 받을 만큼 가볍지만, 기준일(과거 날짜)을 선택하면 그때는 어차피 라이브 계산(`build_table.build(as_of=...)`)으로 폴백 — "오늘" 조회만 캐시로 빠르게 | 한 번에 한 달치만 반환하는 API라 전체 히스토리는 200+ 호출 필요 |
| `workflow_dispatch` | 수동 실행 가능 | 수동 실행 가능 |

워크플로 공통 구조(두 파일 거의 동일):

```yaml
permissions:
  contents: write
jobs:
  update-data:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt
      - env: { FRED_API_KEY: ${{ secrets.FRED_API_KEY }} }   # 또는 RTMS_API_KEY
        run: python -m gold_dashboard.update_data              # 또는 update_banpo_data
      - run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/latest.json                              # 또는 banpo_xi_trend.json
          git diff --cached --quiet || git commit -m "chore: ..."
          git push
```

### 2.8 캐시 파일 스키마

`data/latest.json` (메인 대시보드):
```json
{
  "generated_at": "ISO8601 (Asia/Seoul)",
  "requested_as_of": null,
  "as_of": "YYYY-MM-DD",
  "indicator_order": ["real_rate", "dxy", "wti", "vix"],
  "indicators": {
    "real_rate": {
      "label": "실질금리", "source": "FRED DFII10", "as_of": "YYYY-MM-DD",
      "prev_close": {"value": 2.62, "display": "2.62%"},
      "sma": {"90": {"ma_value": ..., "ma_display": ..., "status_text": "상향 돌파",
                     "display": "...", "streak": N, "breakout": bool,
                     "streak_display": "N일째", "gold_friendly": bool}, "30": {...}, "7": {...}}
    }, "dxy": {...}, "wti": {...}, "vix": {...}
  },
  "static_rows": {...}, "row_order": [...], "ma_windows": [90, 30, 7], "footnotes": {...}
}
```

`data/banpo_xi_trend.json` (반포자이 추세):
```json
{
  "generated_at": "ISO8601",
  "source": "국토교통부 아파트매매 실거래가 공개 데이터(RTMSDataSvcAptTrade), ...",
  "methodology": "거래일 순 직전 10건 단순이동평균(원시 거래금액, 원) — ...",
  "transaction_count": 550,
  "rows": [
    {"available_date": "2009-05-06", "trend_value": 1090000000, "raw_value": 1080000000},
    ...
  ]
}
```
`rows[].raw_value`는 그 시점 개별 거래 원값(평활화 전) — 자산비교 페이지가 추세선과 함께
실제 체결가를 점으로 겹쳐 보여줄 때 씁니다.

---

## 3. 메인 대시보드 페이지 설계

### 3.1 추적 지표 4개

| key | 라벨 | 소스 | 상관관계 방향 | 실제 매매신호 사용 |
|---|---|---|---|---|
| `real_rate` | 실질금리 | FRED `DFII10` | 역상관(inverse) | ✅ green_count |
| `dxy` | 달러인덱스 | yfinance `DX-Y.NYB`(폴백: `^DXY`, `DX=F`) | 역상관(inverse) | ✅ green_count |
| `wti` | WTI | FRED `DCOILWTICO` | 정상관(positive) | ⛔️ 참고용만 |
| `vix` | VIX | yfinance `^VIX` | 정상관(positive) | ⛔️ 참고용만 |

이동평균 윈도: `config.MA_WINDOWS = [60, 20, 7]`(역일/캘린더일 기준, 거래일 수 아님).

### 3.2 `gold_friendly_vs_ma` — "이 신호가 금에 우호적인가"의 단일 함수

```python
def gold_friendly_vs_ma(value, sma, direction):
    if direction == "inverse":
        return (value <= sma).fillna(False)   # 역상관: MA 이하일 때 우호적
    return (value > sma).fillna(False)         # 정상관: MA 돌파 시 우호적
```

메인 표 하이라이팅(`build_table.py`), 백테스트 green_count(`backtest.py`), 메인 대시보드
차트 음영(`app.py`) **셋 다 이 함수 하나만** 호출합니다 — 각자 따로 비교 로직을 짜면 조용히
어긋날 수 있어서, 단일 소스로 강제합니다.

### 3.3 green_count (0~6점)

`real_rate`와 `dxy` 각각에 대해 `MA_WINDOWS`의 3개 윈도 SMA를 구하고, 그 중 "금에 우호적인
쪽"에 있는 셀 수를 **2개 지표 × 3개 윈도 = 6개 셀** 전체에서 합산합니다.

```python
def compute_green_count(real_rate, dxy):
    # real_rate/dxy 각각에 대해 7/20/60일 SMA 3개 구하고
    # gold_friendly_vs_ma로 bool 플래그 6개 만든 뒤 행별 합
    return sum_of_6_boolean_flags  # 0~6 정수
```

**중요한 설계 결정**: `compute_green_count`는 **`real_rate`+`dxy`만 합친 프레임**에서
계산하고, "gold"(금 가격 자체, 또는 대리자산) 컬럼은 그 이후에 합류시킵니다. 이유: SMA는
행 기준(row-wise) 평균이라, 만약 gold를 먼저 합치고 나서 SMA를 구하면 gold 쪽 거래일
캘린더(예: 비트코인은 주말에도 거래)가 outer join으로 real_rate/dxy에 중복 forward-fill된
행을 섞어 넣어 SMA 윈도 안의 표본이 basis마다 달라지는 버그가 생깁니다(실측: 수정 전
비트코인 기준은 다른 기준 대비 약 5% 거래일에서 green_count가 달랐음). **반드시
real_rate+dxy만으로 green_count를 구하고 나서 gold를 합류**시키세요.

### 3.4 표 레이아웃

- 헤더: 4개 지표 열.
- 고정 행: "구조"/"의미"/"상관관계 방향"(정적 텍스트, `config.STATIC_ROWS`).
- MA 행(윈도별 1행씩, 60→20→7 순서는 아니고 `config.MA_WINDOWS` 그대로): 셀에 "MA값 (종가 X →
  상향 돌파/이평선 아래)" + 작은 글씨로 연속 돌파일수. `real_rate`/`dxy` 6개 셀 블록은 굵은
  외곽선으로 시각적으로 묶음(green_count가 보는 블록임을 알리기 위함).
- 마지막 행: 전일종가(오늘 조회 시) 또는 종가(과거 날짜 조회 시).
- 초록 배경(rgba(76,175,80,0.28))은 `gold_friendly=True`인 셀에만 — WTI/VIX는 절대
  초록이 되지 않음(green_count에 안 쓰이므로).

### 3.5 핵심 요약 콜아웃

표 위에 고정된 "[분석 개요]/[분석 결과]/[유의사항]" 3단 서술형 텍스트 + "상승장 보유 비중
X~Y%, 하락장 Z~W%" 문구. 이 숫자만 매 실행 **동적 계산**(나머지 텍스트는 고정):

```python
signals_df = backtest.prepare_signals(as_of=..., years=backtest.MAX_BACKTEST_YEARS,
                                        gold_price_basis=config.GOLD_PRICE_BASIS_DEFAULT)
held = regime.compute_threshold_holding(signals_df, **regime.default_backtest_kwargs(...))
rates = regime.participation_rates(signals_df.index, held)
bull_lo, bull_hi = regime.participation_rate_range(rates, "상승장")
bear_lo, bear_hi = regime.participation_rate_range(rates, "하락장")
```

백테스트 페이지의 **기본 파라미터**(사용자가 그 페이지에서 뭘 바꿨든 무관) + **최대 분석
기간**(`MAX_BACKTEST_YEARS=15`)으로 고정 계산 — 대시보드 전체의 변하지 않는 기준점 역할.
하드코딩된 숫자를 손으로 바꾸는 곳은 어디에도 없게 하는 게 설계 의도입니다.

### 3.6 지표별 시계열 차트 (Altair)

- 버튼을 누른 지표만 그 시점에 최근 10년(`CHART_YEARS=10`, 백테스트 페이지의 조정 가능한
  기간과는 **독립**)치를 받아와 그림 — 기본은 아무 차트도 미리 계산 안 함.
- 왼쪽 축: 지표 자신의 종가(진한 파랑) + 60/20/7일 SMA(옅어지는 파랑 3단계, 점선).
  오른쪽 축: 금 가격(GC=F, 주황).
- 빨간 음영(`rgba` opacity 0.16): `real_rate`/`dxy`만, `MA_WINDOWS`의 **모든** 윈도가
  동시에 gold-friendly인 날(`signals.all_windows_gold_friendly_for`) — green_count
  6점 만점과는 다른, 더 엄격한 단일 지표 기준.
- 캐싱: `st.cache_data(ttl=86400)`(일 1회) — 지표별, as_of별로 키.

### 3.7 캐싱 요약

| 함수 | ttl | 비고 |
|---|---|---|
| `load_data` | 3600s | 오늘 조회 + 캐시 파일 있으면 그 파일 그대로 반환(네트워크 無), 그 외엔 `build_table.build()` 라이브 호출 |
| `load_chart_data` | 86400s | 지표별 10년 시계열 |
| `load_regime_participation` | 3600s | 핵심요약 참여율 |

---

## 4. 백테스트("유효성 검증") 페이지 설계

가장 복잡한 페이지. `pages/1_백테스트.py`(UI) + `gold_dashboard/backtest.py`(순수 로직)로
나뉩니다 — UI는 세션 상태를 쥐고 있다가 `backtest.simulate()`를 호출만 하고, 상태기계
자체는 `backtest.py`에 전부 있습니다.

### 4.1 금 가격 기준 ①~⑧ (`config.GOLD_PRICE_BASIS_*`)

메인 대시보드와 **세션 상태 키 하나**(`config.GOLD_PRICE_BASIS_STATE_KEY = "gold_price_basis"`)를
공유합니다. **단, 그 키를 각 페이지 위젯의 `key=`로 직접 쓰면 안 됩니다** — `st.navigation`은
어떤 위젯이 그 실행(run)에서 인스턴스화되지 않으면(= 다른 페이지로 이동하는 순간) 그
위젯의 `session_state` 항목을 기본값으로 초기화해 버립니다(이 Streamlit 버전에서 직접
확인됨). 그래서 패턴은:

```python
st.session_state.setdefault(config.GOLD_PRICE_BASIS_STATE_KEY, config.GOLD_PRICE_BASIS_DEFAULT)
gold_price_basis = st.radio(
    "금 가격 기준", options=_gold_basis_options,
    index=_gold_basis_options.index(st.session_state[config.GOLD_PRICE_BASIS_STATE_KEY]),
    key="_gold_price_basis_widget_backtest",   # 페이지 로컬 전용 키 — 공유 키와 다름!
    ...
)
st.session_state[config.GOLD_PRICE_BASIS_STATE_KEY] = gold_price_basis   # 공유 키에 다시 씀
```

양쪽 페이지의 `_gold_basis_options` 리스트는 **반드시 서로 완전히 동일한 슈퍼셋**이어야
합니다 — 한쪽에만 새 옵션을 추가하면 다른 쪽 `st.radio`의 `index=` 조회가 `ValueError`.

| # | basis 상수값 | 가격 소스 | 비고 |
|---|---|---|---|
| ① | `"intl"` | yfinance `GC=F` (USD/oz) | 기존부터 있던 기본 국제 시세 |
| ② | `"krx"` | Naver 미러 KRX 금현물(KRW/g) | `GOLD_PRICE_BASIS_DEFAULT` |
| ③ | `"korea_zinc"` | `010130.KS` 고려아연 | placebo(원자재 관련) |
| ④ | `"mirae_asset"` | `006800.KS` 미래에셋증권 | placebo(무관 업종) |
| ⑤ | `"hyundai_motor"` | `005380.KS` 현대차 | placebo(무관 업종) |
| ⑥ | `"kospi200"` | `069500.KS` KODEX 200 ETF | placebo(시장 전체) |
| ⑦ | `"bitcoin"` | `BTC-KRW` | "디지털 금" 가설 검증용, placebo 세트엔 제외 |
| ⑧ | `"seoul_apt"` | 반포자이 실거래가 추세(월간 캐시) | 상수 이름은 과거 "서울 아파트 지수" 시절 유산, 값은 세션 호환 위해 유지 |

⑧의 구성 — `gold_dashboard/timeseries.py`의 `fetch_gold_price_series`:
```python
raw = ds.fetch_banpo_xi_trend_series()           # 캐시 JSON 읽기, 이미 평활화·지연보정 끝난 값
series = raw[(raw.index >= fetch_start) & (raw.index <= end_date)]
return (series / series.iloc[0] * 100.0)          # 분석 구간 첫 값=100으로 리베이스만
```
반포자이 추세 자체의 구성(`update_banpo_data.py`):
1. RTMS에서 서초구(법정동코드 `11650`) 전체 아파트매매 실거래를 월별로 받아,
   `umdNm=="반포동"` AND `aptNm`에 `"반포자이"` 포함 AND `excluUseAr=="84.943"`로 필터.
2. 거래일 순으로 정렬 후 **직전 10건 단순이동평균**(`BANPO_TREND_WINDOW_TRANSACTIONS=10`)을
   "추세값"으로.
3. 각 거래의 `available_date = deal_date + 45일`(`BANPO_REPORTING_LAG_DAYS`)로 look-ahead
   방지.
4. 결과를 `{"available_date", "trend_value", "raw_value"}` 행의 리스트로 JSON 저장.

### 4.2 매수·매도 신호 (OR 결합, 지연 없음)

```
매수(미보유일 때만): green_count >= buy_green_count(기본 6)
                   OR 52주 신고가 신규 갱신(use_new_high_trigger, 기본 ON)
매도(보유 중일 때만): green_count <= sell_green_count(기본 0)
                   OR 52주 신저가 신규 갱신(use_new_low_trigger, 기본 ON)
```
- "52주"는 `FIFTY_TWO_WEEK_WINDOW_DAYS = 365` 캘린더일, `closed="left"` 롤링 윈도(오늘
  자신은 "직전 365일"에서 제외 — 아니면 오늘이 제 고점/저점과 같아져 "신규 돌파"가 영원히
  성립 안 함)로 "오늘 처음 그 구간의 최고/최저를 갱신"한 날만 True(그냥 "현재 52주
  최고/최저 상태"가 아님).
- 트리거 자체엔 쿨다운/빈도 제한이 없음(원하면 "매매 쿨다운" 옵션으로 별도 제한 가능, §4.4).
- 모든 체결은 **신호가 뜬 바로 그날 종가**에 즉시 일어남(지연 설정 없음).

### 4.3 노이즈 필터 (매도/매수 각각, 기본 ON)

**목적**: 대세 상승장 중 일시적 조정에 바로 손절(혹은 하락장 중 일시 반등에 바로 진입)하는
걸 막음.

**게이트 조건** — 신호가 뜬 날(D0) 종가가 180일(`LONG_TREND_WINDOW`) 역일 SMA 대비:
- 매도: **+5% 이상**(`DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT`) 높을 때만 필터 작동(그 외엔
  즉시 매도)
- 매수: **-5% 이상**(`DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT`) 낮을 때만 필터 작동(그 외엔
  즉시 매수)

**확인 방식 ① — 매일 갱신 2시그마 밴드**(기본 ON, `use_daily_band_confirmation`):

$$
\text{daily\_vol} = \frac{\text{monthly\_vol}(4.9\%)}{\sqrt{21}} = 1.0694\%
\qquad
\text{band}(t) = 2 \times \text{daily\_vol} \times \sqrt{t}
$$

(`t` = D0로부터 경과한 **거래일** 수, 역일 아님 — 월간 변동성을 거래일 21일로 역산했기
때문에 분모 단위를 맞춰야 함)

| t(거래일) | band(t) |
|---|---|
| 1~7 | 체크 안 함(무조건 보류) — `NOISE_BAND_MIN_CHECK_TRADING_DAYS=8` 미만 |
| 8 | 6.05% |
| 10 | 6.76% |
| 14 | 8.00% |
| 21 | 9.80% (= `2 × monthly_vol` 정확히, √21이 서로 상쇄) |

t=8..21(`NOISE_BAND_MAX_TRADING_DAYS`) 동안 종가가 `d0_close × (1 ∓ band(t))`에 처음
도달하는 날 즉시 체결. 21거래일 내내 도달 못 하면 **에피소드 해제**(D0는 없었던 일로 처리,
다음 신호부터 새로 시작).

**확인 방식 ② — 고정 D0+7일**(레거시, 체크박스 OFF): D0로부터 역일 7일
(`SELL_NOISE_FILTER_WINDOW_DAYS`/`BUY_NOISE_FILTER_WINDOW_DAYS`) 뒤 한 시점만 보고,
`drop_pct`/`rise_pct`(기본 각 5%) 이상이면 체결, 아니면 해제.

에피소드가 열려 있는 동안 추가로 뜨는 신호는 **참고 기록(occurrence_count)**으로만 쌓이고
D0나 판정 방식을 바꾸지 않습니다.

### 4.4 그 외 상태기계 옵션

- **최소 보유일수**(`min_holding_days`, 기본 0): 매수 후 이 일수(역일) 전까지는 어떤 매도
  트리거도 평가 자체를 안 함.
- **매매 쿨다운**(`use_trade_cooldown`, 기본 OFF, `trade_cooldown_days` 기본 14일): 아무
  거래(매수든 매도든)가 체결되면 **52주 신고가/신저가 트리거만** 그 뒤 N일간 비활성화
  (green_count는 영향 없음) — 바닥매수 직후 추가 하락으로 바로 52주 신저가에 손절당하는
  휩소를 막기 위함.

### 4.5 수수료·세금·배당 모델 (기준별로 완전히 다른 구조)

| 기준 | 매수 수수료 | 매도 수수료(세금 포함) | 보유 비용 | 배당 |
|---|---|---|---|---|
| ① 국제시세 | 0(참고가격, 실물 아님) | 0 | 0 | 없음 |
| ② KRX 금현물 | `DEFAULT_BUY_FEE_PCT=0.165%` (편도, 매매 때마다) | `DEFAULT_SELL_FEE_PCT=0.165%` | `DEFAULT_DAILY_HOLDING_FEE_PCT=0.00022%`(일률, 매일 누적) | 없음 |
| ② TIGER KRX금현물(ETF) | 0% | 0% | 연 총보수 0.15%를 `(1+연율)^(1/365)-1`로 일할 환산 | 없음 |
| ③④⑤ 코스피 개별주(고려아연/미래에셋증권/현대차) | `KOREA_STOCK_DEFAULT_BROKERAGE_FEE_PCT=0.015%` | 매매수수료 0.015% + 증권거래세·농특세 `KOREA_STOCK_SECURITIES_TRANSACTION_TAX_PCT=0.2%`(매도 시에만) | 없음(실물 보관 개념 자체 없음) | ex-dividend일에 배당수익률 가산(옵션: `DIVIDEND_INCOME_TAX_PCT=15.4%` 세후 전환) |
| ⑥ 코스피200(ETF) | 0.015% | 0.015%(거래세 면제) | 없음 | 배당 반영(동일 로직) |
| ⑦ 비트코인 | `CRYPTO_DEFAULT_EXCHANGE_FEE_PCT=0.05%` | 0.05% | 없음 | 없음(증권거래세·배당 개념 자체 없음) |
| ⑧ 반포자이 실거래가 | 0 | 0 | 0 | 없음(중개수수료·취득세·양도세 등 모델링 안 함) |

보유 비용은 `_daily_fee_decay(elapsed_days, daily_fee_pct) = (1 - daily_fee_pct/100) ** elapsed_days`
— 연율이 아니라 **하루치 요율을 그대로 복리 적용**. 매수/매도 수수료는 그 체결 시점 1회성
곱셈(`(1 - fee_pct/100)`)이고, 이건 거래 횟수에 비례해 누적되지 거래 시점의 1회성 할인일
뿐 홀딩 기간과 무관합니다.

### 4.6 CAGR 계산 — 두 가지 분모

| 지표 | 분모 | 미보유 기간 취급 |
|---|---|---|
| ③ 신호전략(보유기간) `strategy_cagr` | **실제 보유했던 일수만**(`invested_days`) | 반영 안 함(그 기간은 아예 분모에서 제외) |
| ④ 신호전략(기대수익률 포함) `hybrid_cagr` | **분석 기간 전체**(Buy & Hold와 같은 분모) | `bond_annual_yield`(기본 연 10%, UI에서 조정 가능)로 그 기간을 채워 넣음 |
| Buy & Hold `bh_cagr` | 분석 기간 전체 | 해당 없음(항상 보유) |

```python
strategy_cagr = final_equity ** (365.25 / invested_days) - 1.0       # ③
hybrid_cagr   = hybrid_equity ** (365.25 / total_days) - 1.0          # ④, compute_hybrid_cagr
bh_cagr       = bh_final_equity ** (365.25 / total_days) - 1.0        # Buy & Hold
```

③과 Buy & Hold의 CAGR을 **그대로 나란히 비교하면 안 됨**(분모가 다름) — ④와 Buy & Hold는
분모가 같아 비교 가능. `compute_hybrid_cagr`는 보유 구간은 실제 금 수익률(+배당+보유비용
반영)로, 미보유 구간은 `(1+bond_annual_yield)^(elapsed_days/365.25)`로 스텝별 복리.
"보유 중이었나"는 **그 스텝에 들어가는 시점(day i-1)의 상태**로 분류(진입일 자체는 그날
가격 변동을 못 받는다는 컨벤션과 일치시키기 위함).

최대 낙폭(MDD): `(equity_curve / equity_curve.cummax().clip(lower=1.0) - 1.0).min()` —
`clip(lower=1.0)`은 최초 매수 당일 수수료로 인한 하락을 "낙폭"으로 잘못 세지 않기 위한
바닥 고정.

### 4.7 신호 계산 파이프라인 (캐싱 2단)

```python
# 1) 네트워크 바운드 + 가장 넓은 창(15년+430일 버퍼)으로 한 번만 계산, (as_of, basis)로 캐시
@st.cache_data(ttl=3600)
def _cached_max_window_signals(as_of_iso, gold_price_basis):
    raw = fetch_raw_data(as_of, years=MAX_BACKTEST_YEARS, gold_price_basis=gold_price_basis)
    return compute_signals(raw)

# 2) "분석 기간(N년)" 슬라이더를 바꿔도 네트워크 재호출 없이, 메모리에서 트림만
def prepare_signals(as_of, years, gold_price_basis):
    full = _cached_max_window_signals(as_of.isoformat(), gold_price_basis)
    return trim_to_backtest_window(full, as_of, years=years)
```
모든 롤링 계산(SMA, 52주 고저)이 **과거만 보는** 연산이라 넓은 창을 미리 계산해두고 좁게
자르는 게 직접 좁은 창으로 계산한 것과 바이트 단위로 동일 — 특히 KRX 금현물 가격(매번 전체
히스토리를 다시 페이징하는 Naver API)의 재요청을 막는 게 핵심 이득.

### 4.8 커스텀 줌 가능 Plotly 차트 (가장 복잡한 구현 디테일)

**왜 `st.plotly_chart`를 안 쓰는가**: Streamlit이 그 차트를 자체 iframe 안에 렌더링하는데,
거기에 임의의 커스텀 JS를 붙일 훅이 없습니다(공식 지원 이벤트는 클릭/박스-셀렉트/라쏘
선택뿐, Y축 자동 재조정이나 X축 틱 동적 전환 같은 임의 로직은 못 붙임).

**대신**: `fig.to_html(include_plotlyjs="cdn", full_html=False, div_id=...)`로 뽑은 HTML에
`<script>`를 그냥 문자열로 이어붙여서 `st.iframe(html_string, height=...)`로 띄웁니다
— `st.iframe`은 원시 HTML을 받아 그대로 실행 가능한 sandboxed iframe을 만들어 줌.

**Y축 자동 스케일(줌 시)**:
1. `gd.on("plotly_relayout", onRelayout)`로 모든 x축 변화(줌/팬/리셋) 구독.
2. 현재 보이는 x범위 안의 점들만 모든 "보이는"(legendonly 아닌) 트레이스에서 훑어 min/max
   계산, 8% 패딩 적용, `Plotly.relayout(gd, {"yaxis.range": [...], "yaxis.autorange": false})`.
3. x축이 "전체"로 리셋되면(`ev["xaxis.autorange"] === true`) y축도 `autorange: true`로
   되돌림.
4. **보조축(secondary y-axis) 지원**(자산비교 페이지가 사용): 트레이스의 `yaxis` 속성
   (`"y"` 기본/`"y2"` 보조)별로 **독립적으로** 같은 로직을 반복 — 한 축에 모든 트레이스를
   몰아넣으면 스케일 차이가 큰 두 자산 중 한쪽이 거의 평평해 보이는 문제 해결.

**X축 연/월 틱 동적 전환**: 단일 Plotly `dtick`으로는 "1월은 연도로, 나머지 달은 숫자로"
같은 두 가지 라벨 형식을 못 섞으므로, `tickvals`/`ticktext` 배열을 직접 계산해 매 줌마다
다시 넘깁니다.
```
visible span > 6년 → 1월만 ("2024")
4~6년           → 1월·7월
2~4년           → 1·4·7·10월
≤2년            → 매달
```
실제 데이터 포인트에 스냅하지 않고 **달력상의 월초(UTC)**로 틱을 생성 — 데이터 밀도와
무관하게 절대 중복/충돌 없음.

**`{dtype, bdata}` 압축 배열 디코딩**: Plotly.py의 기본 JSON 인코더는 숫자 배열을
`{"dtype": "f8", "bdata": "<base64>"}` 같은 압축 포맷으로 씁니다. plotly.js는 이걸 자기
내부 렌더링용으로만 디코드하고 `gd.data[i].y`에는 **디코드된 배열을 다시 써 넣지 않습니다**
— 그래서 커스텀 JS가 `tr.y[i]`를 직접 읽으면 조용히 `undefined`가 나옵니다. 직접 디코드하는
`toArray(v)` 헬퍼가 필수:
```js
function toArray(v) {
    if (Array.isArray(v) || ArrayBuffer.isView(v)) return v;
    if (v && typeof v.bdata === "string") {
        var bin = atob(v.bdata);
        var bytes = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        var ctors = {f8: Float64Array, f4: Float32Array, i1: Int8Array, u1: Uint8Array,
                     i2: Int16Array, u2: Uint16Array, i4: Int32Array, u4: Uint32Array};
        return new (ctors[v.dtype] || Float64Array)(bytes.buffer);
    }
    return null;
}
```

**국면 음영 + 참여율 카드**(백테스트 페이지 전용, `chart_utils.py`에는 없음): 국면 날짜
구간·임계값별 보유 배열을 파이썬에서 한 번 계산해 `<script type="application/json">`으로
JSON 그대로 임베드하고, 줌마다의 "현재 보이는 구간 참여율" 재계산은 **서버 왕복 없이
JS가 그 자리에서** 수행(배열을 다시 pandas로 돌려보내지 않음). 음영은 `yref="y domain"`
(플롯 영역의 고정 비율, Y축 재조정과 무관하게 두께 불변) — 미보유 구간 회색 음영은
`yref="y"`(Y=0 위쪽만, 국면 음영과 안 겹치게).

**`get_plotlyjs()` 메모이제이션**: `fig.to_html(include_plotlyjs="cdn")` 호출의 ~40%
비용이 plotly.js 번들(~3MB)을 디스크에서 매번 다시 읽어 SHA-256 해시를 계산하는 것(CDN
`<script integrity=...>` 속성용) — 이 함수는 인자도 없고 프로세스 내내 결과가 불변이므로
`functools.lru_cache(maxsize=1)`로 감싸 프로세스당 한 번만 계산:
```python
if not hasattr(_plotly_html.get_plotlyjs, "cache_clear"):
    _plotly_html.get_plotlyjs = functools.lru_cache(maxsize=1)(_plotly_html.get_plotlyjs)
```

### 4.9 국면/참여율 분석 (`regime.py`)

- 국면 구간표 4개(수동 큐레이션, 공식에서 도출된 게 아님): `REGIME_UPTREND`/`DOWNTREND`
  (일반 국면), `REGIME_SECULAR_UP`/`DOWN`(장기 국면, 겹치면 우선).
- 5단계 분류(`classify_regime_5way`): 대세상승장 > 대세하락장 > 상승장 > 하락장 > 보합장
  (어느 구간표에도 안 걸리면 보합장) 우선순위로 하루하루 라벨링.
- `THRESHOLD_PAIRS = [(6,0), (5,1), (4,2)]` 세 가지 green_count 매수/매도 임계값 조합 각각
  에 대해 `backtest.simulate()`를 다시 돌려 보유곡선(holding_curve)만 뽑고, 국면별 보유
  비율(%)을 계산 — 메인 대시보드 핵심요약과 백테스트 페이지의 참여율 카드가 **같은 이
  함수**를 공유.

---

## 5. 자산비교 페이지 설계

백테스트 페이지(가격 기준·매매신호·수수료)와 **완전히 독립** — 매매신호 로직을 전혀 안 쓰고
순수 통계(회귀·R²) 탐색 도구입니다.

### 5.1 목적과 구조

반포자이 실거래가 추세를 **기준(benchmark)**으로 놓고, 선택 가능한 비교 자산(비트코인·
하이닉스·코스피·국내금)을 월별로 지수화해 겹쳐보며, λ(거듭제곱 변환)·래그(개월)로 맞춰보는
R²를 확인합니다. 기본(아무것도 선택 안 함) 화면은 반포자이 실거래(점)+추세(선)만 표시.

### 5.2 월별 집계 — "없는 달은 0이 아니라 공란"

```python
def monthly_mean_frame(series_dict):
    monthly = {name: s.copy().groupby(level=0.to_period("M")).mean() for ...}
    all_months = union_of_every_series_own_month_range   # 교집합이 아니라 합집합
    return pd.DataFrame({name: s.reindex(all_months) for ...})  # 없는 달은 NaN
```
각 자산을 자기 PeriodIndex로 `to_period("M")` 그룹화 → 산술평균 → 전체 자산 month 인덱스의
**합집합**으로 reindex(교집합이 아님) — 한 자산만 있는 달도 그 자산 칸만 채워지고 나머지는
NaN. 0으로 채우면(ffill도 포함) R²/인덱스 계산이 거짓으로 왜곡되므로 반드시 NaN 유지.

### 5.3 λ 거듭제곱 변환

```python
def transform(x, lam):
    return np.log(x) if abs(lam) < 1e-9 else np.power(x, lam)

def inverse_transform(y, lam):
    return np.exp(y) if abs(lam) < 1e-9 else np.power(y, 1.0 / lam)
```
**Box-Cox의 `(x^λ - 1) / λ`가 아니라 순수 `x^λ`**(λ=0일 때만 log로 분기)를 씁니다. 이유:
(1) λ=1일 때 진짜 항등(identity)이 되어야 함(Box-Cox식은 λ=1에서 `x-1`), (2) 이후 지수화
(첫 유효값으로 나누기)를 할 때 Box-Cox 변환이 낮은 가격대(예: 2010년의 1달러 미만 비트코인)
에서 음수 부호를 뒤집는 위험을 아예 없애기 위함.

### 5.4 래그(lag) 의미론

`fit_and_r2(monthly, banpo_monthly, asset, lam, lag, excl_start, excl_end, window_start=None, window_end=None)`:
```python
source_month = monthly.index - lag          # asset이 lag개월 "먼저" 움직였다고 가정
asset_vals = monthly[asset].reindex(source_month)   # t행에 asset[t-lag] 매칭
banpo_vals = banpo_monthly.reindex(monthly.index)   # 반포자이는 t 그대로
```
즉 "반포자이[t]는 asset[t-lag]로 설명된다"(asset이 lag개월 선행). **차트에 그릴 때도 이와
일관되게**, asset의 선을 실제 관측 시점이 아니라 `관측시점 + lag`에 그려 반포자이와 시각적
으로 나란히 맞춥니다(`display_x = (idx.index + lag).to_timestamp()`) — 그래야 R²가 높을
때 차트도 실제로 겹쳐 보입니다(래그를 차트에 반영 안 하면 R²는 높은데 그림은 안 맞아
보이는 불일치가 생김 — 실제로 이 레포에서 그 버그를 겪고 고쳤습니다).

### 5.5 R² 계산 + 제외구간 / 구간 지정

- `np.polyfit(x, y, 1)`로 수동 OLS, `R² = 1 - SS_res/SS_tot`.
- **제외구간**(`excl_start`/`excl_end`): asset의 **post-lag 소스월**(`source_month`,
  실제 관측 시점) 기준으로 체크해서 그 구간에 속하는 행을 **제외**. ⚠️ **함정**: 신규월
  (nominal month, 즉 반포자이 기준 행의 시점)로 체크하면 lag가 클 때 제외가 아무 효과도
  없을 수 있습니다 — 예를 들어 lag=12면 "올해" 데이터가 아직 "올해" 행의 소스가 안 돼
  있어서(소스는 작년 데이터이므로) "올해"를 제외해도 실제로 지워지는 행이 없습니다.
  반드시 `source_month`(= `monthly.index - lag`)로 체크하세요.
- **"특정 구간만의 R²"**(`window_start`/`window_end`): 위와 반대로, **반포자이의 nominal
  월(t)**이 그 구간에 드는 행만 **유지**(제외구간과 반대 극성). 이건 차트가 그리는 두 선
  (반포자이 자신 + lag만큼 밀린 asset)이 둘 다 화면상 같은 x좌표(t)에 있기 때문에, "지금
  화면에 보이는 구간"과 일치시키려면 nominal 기준이어야 합니다.
  - 원래 이 기능은 **차트의 줌 상태를 실시간으로 Python에 돌려받아 자동 동기화**하는
    진짜 양방향 Streamlit 커스텀 컴포넌트(`streamlit.components.v1.declare_component`,
    `componentReady`/`render`/`setComponentValue`/`setFrameHeight` postMessage 프로토콜을
    수동 구현)로 구현을 시도했으나, 최소 재현 케이스(`declare_component`로 만든 echo 전용
    테스트 컴포넌트)에서도 handshake 후 `streamlit:render` 메시지가 전혀 전달되지 않는
    현상이 재현되어(원인 미상 — 이 환경의 Streamlit 1.66.1 프런트엔드 번들 내부 동작과
    관련된 것으로 추정) 포기했습니다. 대신 **수동 `st.date_input` 구간 선택**으로 같은
    목적(임의 구간의 R² 확인)을 달성합니다 — 사용자가 차트에서 본 기간을 그대로 입력.

### 5.6 비트코인 전체 역사 복원

yfinance의 `BTC-KRW`는 **2014-09-17 이전 데이터가 전혀 없습니다**(Yahoo의 암호화폐 티커
공통 개시일). 실제 거래는 2010년 중반부터 있었으므로:

```python
def fetch_bitcoin_full_history_krw():
    usd = blockchain.info 일별 USD 시세(2009~)
    fx = fetch_fred_series("DEXKOUS")              # 원/달러 환율 — 이미 쓰는 FRED_API_KEY 재사용
    synthetic = usd * fx.reindex(usd.index, method="ffill")   # 2014-09-17 이전 구간 추정치
    real = fetch_yfinance_close("BTC-KRW", start="2014-01-01")  # 2014-09-17~ 실제 KRW 시세
    cutover = real.index.min()
    return concat(synthetic[< cutover], real[>= cutover])   # 리스케일 없이 그냥 이어붙임
```
컷오버 지점에서 두 구간 값이 이미 1~2% 이내로 비슷해 리스케일 없이 이어붙여도 눈에 띄는
불연속이 없습니다(합성 추정치는 실제 거래가가 아니라 USD×환율 추정치라는 한계는 있음).

### 5.7 λ/원 데이터 토글이 R²에도 반영

표시 모드(`"원 데이터(지수)"`/`"λ 변환 데이터(지수)"`) 라디오는 차트뿐 아니라 **화면에
보여줄 R²도 같이 바꿉니다** — "원 데이터" 선택 시 λ=1(변환 없음) 기준 R², "λ 변환 데이터"
선택 시 사용자가 입력한 λ 기준 R². 이렇게 하면 토글만으로 "λ 변환이 적합도를 실제로
개선하는가"를 바로 비교할 수 있습니다. 단, **시나리오 계산기(§5.8)는 이 토글과 무관하게
항상 사용자가 입력한 λ 값 그대로** 사용합니다(화면에 뭘 보고 있든 시나리오 결과가 안
바뀌게).

### 5.8 시나리오 계산기

"반포자이가 N년 안에 k배 오르면, 같은 회귀식이 유지된다고 가정할 때 다른 자산은 얼마가
돼야 하는가"를 역산:
```python
banpo_target = banpo_now * fold_change
x_required = (banpo_target - intercept) / slope      # 회귀식 역산
asset_required = inverse_transform(x_required, lam)
asset_fold = asset_required / asset_now
relative_fold = asset_fold / fold_change               # 1배 넘으면 반포자이보다 오버퍼폼 필요
out_of_range = asset_required > asset_max*1.5 or asset_required < asset_min*0.67   # ±50% 외삽 경고
```
**"회귀식상 불가능(음수)" 엣지 케이스**: `x_required <= 0`이면 발생. λ<0인 자산(국내금
λ=-0.70 등)은 `x = price^λ`가 가격이 오를수록 **0에 점근**(절대 음수가 안 됨)하는 감소
함수입니다. 반포자이·자산이 둘 다 장기 상승했다는 걸 이 모양(x는 감소, y는 증가)으로
맞추려면 회귀 기울기(slope)가 음수가 돼야 하는데, slope<0·x>0 조합에서 이 직선이 표현할
수 있는 반포자이의 **이론적 상한**은 x→0(가격→∞)일 때의 `intercept` 값입니다. 목표
반포자이 값이 이 상한을 넘으면, 가격을 아무리 올려도(무한대로 보내도) 그 회귀식을 만족할
수 없어 "회귀식상 불가능"이 뜹니다 — R²가 높은 것과는 무관한, **음수 λ 변환의 구조적
포화(점근선)**가 원인입니다.

### 5.9 보조축 듀얼 차트

반포자이(실거래 점 + 추세선)는 항상 **주축(왼쪽, `yaxis="y"`)**, 선택된 비교 자산은
**보조축(오른쪽, `yaxis="y2"`, `fig.update_layout(yaxis2=dict(overlaying="y", side="right"))`)**.
변동폭 차이가 큰 두 자산(예: 반포자이 5배 vs 비트코인 1,000,000배, 2010년 기준)을 같은
축에 두면 한쪽이 거의 평평해 보이는 문제 때문 — `chart_utils.render_zoomable_chart`의
보조축 일반화(§4.8)가 줌마다 두 축을 각자 독립적으로 재조정.

---

## 6. 공통 구현 디테일 / 함정 목록

- **yfinance 기본 기간 함정**: `start`/`end`를 둘 다 `None`으로 두면 `period="1mo"`로
  조용히 최근 한 달만 반환 — 항상 명시적 `start=`를 넘길 것.
- **tz-naive vs tz-aware datetime64 reindex 에러**: `pd.Timestamp.utcfromtimestamp(x)`
  대신 `pd.Timestamp(x, unit="s")`를 쓸 것(§2.4) — 서로 다른 소스(FRED는 naive, 잘못
  만들면 blockchain.info 쪽만 aware)를 reindex하면
  `TypeError: Cannot compare dtypes datetime64[us] and datetime64[us, UTC]`.
- **래그 + 제외구간 상호작용 버그**: 제외구간을 "신규(nominal) 월" 기준으로 체크하면, 래그가
  클 때 사실상 아무 효과가 없을 수 있음 — 반드시 "post-lag 소스월" 기준으로 체크(§5.5).
  반대로 "지금 화면에 보이는 구간"과 맞추려는 필터(§5.5의 window_start/end)는 **nominal
  월** 기준이어야 함 — 둘을 혼동하지 말 것.
- **green_count는 gold 컬럼 합류 전에 계산**: gold(또는 그 대리자산)의 거래 캘린더가 더
  촘촘하면(예: 비트코인은 주말도 거래) SMA 롤링 윈도에 중복 행이 섞여 조용히 값이 달라짐
  (§3.3) — `real_rate`+`dxy`만의 프레임에서 먼저 계산.
  
- **`st.navigation`이 위젯 세션 상태를 리셋**: 두 페이지가 같은 "공유 상태"를 표현하고
  싶으면, 그 공유 키를 위젯의 `key=`로 직접 쓰지 말고, 각 페이지가 자기 로컬 위젯 키로
  렌더링한 뒤 그 결과를 공유 세션 키에 다시 써 넣는 패턴을 쓸 것(§4.1).
- **`st.iframe`에는 Python으로 돌아오는 채널이 없음**: 일반 `st.iframe(html)`은 그냥
  샌드박스 iframe이라 postMessage로 Streamlit 프런트엔드에 값을 되돌릴 방법이 전혀
  없습니다. 값을 되돌리려면 `streamlit.components.v1.declare_component`로 선언한 **진짜
  컴포넌트**가 필요(그 경우만 Streamlit이 그 iframe에서 오는 postMessage를 실제로
  듣습니다) — 이 저장소에서 자산비교 페이지의 "줌 구간 자동 동기화"를 위해 그 방식을
  시도했으나, 이 환경에서는 핸드셰이크 후 `render` 메시지가 전달되지 않아 실패해 수동
  date_input으로 대체했습니다(§5.5) — 다시 시도한다면 Streamlit 버전별 프런트엔드 번들
  (`ComponentInstance.*.js`)에서 정확한 postMessage 타입 문자열
  (`streamlit:componentReady`/`streamlit:render`/`streamlit:setComponentValue`/
  `streamlit:setFrameHeight`)을 직접 확인하고 최소 echo 컴포넌트로 먼저 검증할 것.
- **Plotly 압축 배열 디코딩 필수**: `fig.to_html()`로 내보낸 수치 배열은 `{dtype,bdata}`
  base64 포맷일 수 있고, plotly.js는 이걸 `gd.data[i].y`에 평범한 배열로 되돌려 쓰지
  않습니다 — 커스텀 JS가 직접 디코드해야 함(§4.8).
- **리베이스-투-100은 수학적으로 무해함**: 금/반포자이 가격 윈도를 "구간 첫 값=100"으로
  재정규화하는 건 green_count/52주 신고가·신저가/수익률 계산 전부 **비율 기반**이라
  스케일 상수 곱셈에 불변 — 어떤 상수 c로 `series * c`를 해도 `a/b`나 `max(window)` 비교
  결과는 동일하므로, 화면 표시용 리베이스는 신호 로직에 전혀 영향을 주지 않습니다. (증명
  스케치: `green_count`는 MA 대비 비교, `52주 신고가`는 롤링 max 대비 비교, `CAGR`/수익률은
  `final/initial` 비율 — 전부 `c`가 분자·분모 양쪽에 똑같이 곱해져 상쇄됨.)
- **API 키 취급 원칙**: `FRED_API_KEY`/`RTMS_API_KEY` 전부 `os.environ[...]`으로만 읽고,
  코드에 하드코딩하거나 로그/커밋/출력에 남기지 않습니다. GitHub Actions에서는
  `secrets.FRED_API_KEY`/`secrets.RTMS_API_KEY`를 워크플로 `env:`로만 주입.
- **`st.cache_data` 키 설계**: `as_of`처럼 날짜가 바뀌면 정말 다시 계산해야 하는 인자는
  캐시 키(함수 인자)에 넣고, "분석 기간(N년)"처럼 더 넓은 창에서 메모리 트림만으로 해결
  가능한 인자는 **캐시 키에서 빼고** 가장 넓은 창을 한 번만 계산해 바깥에서 자르는 패턴을
  씀(§4.7) — 네트워크 호출 횟수를 줄이는 핵심 기법.

---

## 7. 파일 구조 요약표

| 파일 | 역할 |
|---|---|
| `app.py` | `st.navigation` 등록(엔트리포인트) + 메인 대시보드 페이지 렌더링(`render_dashboard`) |
| `gold_dashboard/config.py` | 지표 메타데이터, MA 윈도, 금 가격 기준(①~⑧) 상수·라벨·수수료 기본값, 반포자이 필터링 상수 |
| `gold_dashboard/data_sources.py` | FRED/yfinance/Naver/blockchain.info/RTMS-캐시 raw fetch 함수 전부, 공통 재시도 데코레이터 |
| `gold_dashboard/timeseries.py` | `fetch_raw_series`/`fetch_gold_price_series`/`fetch_backtest_frame` — 여러 소스를 조합해 신호용 프레임 구성 |
| `gold_dashboard/signals.py` | `gold_friendly_vs_ma`/`compute_green_count` — "신호가 금에 우호적인가"의 단일 소스 |
| `gold_dashboard/metrics.py` | `compute_sma`(역일 이동평균)/`breakout_streak` |
| `gold_dashboard/regime.py` | 국면 구간표(수동 큐레이션) + 5단계 분류 + 참여율 계산(대시보드·백테스트 공유) |
| `gold_dashboard/backtest.py` | 신호 상태기계(`run_backtest`)·노이즈 필터·CAGR·`simulate()`/`run()` 전체 백테스트 로직 |
| `gold_dashboard/build_table.py` | 메인 대시보드 표 payload 조립(`build()`) — `update_data.py`와 `app.py` 둘 다 호출 |
| `gold_dashboard/chart_utils.py` | 범용 줌 가능 Plotly 차트 렌더러(`render_zoomable_chart`) — 자산비교 페이지 전용 |
| `gold_dashboard/update_data.py` | CLI: `data/latest.json` 생성(일 1회 GitHub Actions) |
| `gold_dashboard/update_banpo_data.py` | CLI: `data/banpo_xi_trend.json` 생성(월 1회 GitHub Actions), RTMS 전체 fetch+평활화 |
| `gold_dashboard/timeutil.py` | `today_kst()` — Asia/Seoul 기준 "오늘" |
| `pages/1_백테스트.py` | 유효성 검증 페이지: UI 전체(기준 선택·조건 입력·수수료·차트·거래내역) + `render_backtest_chart` |
| `pages/2_자산비교.py` | 자산군 비교 페이지: λ/래그 R² 탐색 + 시나리오 계산기 |

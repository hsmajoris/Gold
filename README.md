# Gold 상관관계 대시보드

금(Gold) 가격과 상관관계가 있는 5개 지표(실질금리, 달러인덱스, 금/은비율, WTI, VIX)를
한 화면에서 확인하는 Streamlit 대시보드입니다.

## 구성

- `gold_dashboard/config.py` — 지표별 구조/의미/상관관계 방향(고정 텍스트)과 출처 각주
- `gold_dashboard/data_sources.py` — FRED 공식 API 및 yfinance 데이터 수집 (재시도/백오프 포함)
- `gold_dashboard/metrics.py` — 5/30/60일 이동평균 및 "돌파지속 일수" 계산
- `gold_dashboard/build_table.py` — 지표별 데이터 수집 + 계산 결과를 하나의 표로 조립
- `gold_dashboard/update_data.py` — `data/latest.json`을 생성하는 CLI 스크립트
- `app.py` — Streamlit 대시보드 화면
- `.github/workflows/update_dashboard_data.yml` — 매일 07:00(KST)에 데이터를 자동 갱신하는 GitHub Actions 워크플로우

## 데이터 소스

| 지표 | 소스 |
| --- | --- |
| 실질금리 | FRED `DFII10` |
| 달러인덱스 | Yahoo Finance `DX-Y.NYB` (실패 시 `^DXY`, `DX=F` 순으로 재시도) |
| 금/은비율 | yfinance `GC=F` ÷ `SI=F` 일별 종가 |
| WTI | FRED `DCOILWTICO` |
| VIX | Yahoo Finance `^VIX` |

FRED 데이터는 공식 API(`https://api.stlouisfed.org/fred/series/observations`)로 받아오며,
[무료 API 키](https://fred.stlouisfed.org/docs/api/api_key.html)가 필요합니다. 환경변수
`FRED_API_KEY`로 전달하고, GitHub Actions에서는 저장소 Secrets의 `FRED_API_KEY`를 사용합니다.
네트워크 오류(타임아웃 등) 발생 시 최대 3회까지 5~10초 지수 백오프로 재시도합니다.

## 갱신 주기

데이터 소스가 모두 일봉(전일 확정 종가) 기준이라 "실시간"이 아니라 **매일 아침 7시(KST)에
전일 미국장 마감 종가로 자동 갱신**됩니다. GitHub Actions가 매일 22:00 UTC(=07:00 KST)에
`gold_dashboard/update_data.py`를 실행해 `data/latest.json`을 갱신·커밋하고, Streamlit
앱은 이 파일을 읽어 즉시 표시합니다(파일이 없으면 그때그때 실시간으로 계산).

## 돌파지속 일수 로직

각 지표의 종가가 해당 이동평균선(5/30/60일)을 상향 돌파한 뒤 계속 그 위에 머물러 있는
일수를 센 값입니다. 종가가 이평선 아래로 다시 내려가면 즉시 0으로 리셋됩니다. 세 이평선은
각각 독립적으로 계산되며, 돌파 중인 셀은 옅은 녹색 배경으로 표시됩니다.

## 로컬 실행

```bash
pip install -r requirements.txt
export FRED_API_KEY=your_fred_api_key
python -m gold_dashboard.update_data   # data/latest.json 생성(선택)
streamlit run app.py
```

## 지표별 참고 출처(요약)

- 실질금리 역방향: Erb & Harvey (2013), *The Golden Dilemma*, Financial Analysts Journal
  (표본 기간에 따라 상관관계가 약해질 수 있음을 저자들이 명시)
- 달러인덱스 역방향: World Gold Council 분석 자료(상관계수 약 -0.5~-0.8)
- 금/은비율 80 임계값: Solt & Swanson (1981); CME Group Research (2021)
  (경험적 기술적 임계값이며 절대적 기준은 아님)
- WTI 정방향: Sari, Hammoudeh & Soytas (2010), *Energy Economics*
  (단기 정효과만 확인, 장기 공적분 관계는 미발견 — 신뢰도 낮음)
- VIX 약한 정방향: Baur & Lucey (2010) (최근 안전자산 효과 약화 추세 지적)

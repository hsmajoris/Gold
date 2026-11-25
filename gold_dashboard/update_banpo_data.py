"""Monthly-refresh CLI (run by a separate, monthly-scheduled GitHub Actions
workflow — real-estate transactions don't need daily refreshes the way price
series do) that fetches 반포자이(서초구 반포동) 전용 84.943㎡("국민평형") 실거래
내역via 국토교통부 아파트매매 실거래가 공개 데이터 API(RTMSDataSvcAptTrade,
data.go.kr 경유, REB의 R-ONE API와는 별개의 키가 필요함 — RTMS_API_KEY
환경변수) and writes data/banpo_xi_trend.json for the backtest page's ⑧ 기준
(timeseries.fetch_gold_price_series) to read, exactly like update_data.py
writes data/latest.json for the main dashboard.

Why this is cached instead of fetched live by the Streamlit app itself: this
API's responses only return one calendar month per call, so building the full
history (2008-12~현재, 200+ months) takes 200+ sequential requests — far too
slow to run on every page load. Real transaction data also doesn't change
within a day, so a monthly cache is already more than fresh enough.
"""

import datetime as dt
import json
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from . import config

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "banpo_xi_trend.json"
RTMS_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
REQUEST_TIMEOUT = 30


def _add_month(ymd: str, n: int) -> str:
    y, m = divmod(int(ymd), 100)
    total = y * 12 + (m - 1) + n
    y2, m2 = divmod(total, 12)
    return f"{y2:04d}{m2 + 1:02d}"


def _fetch_month(lawd_cd: str, deal_ymd: str, api_key: str, retries: int = 4) -> list[dict]:
    """One calendar month's raw transaction rows for `lawd_cd` (법정동코드).
    Retried up to `retries` times (3s between attempts) since the upstream
    gateway occasionally resets the connection transiently."""
    params = {
        "serviceKey": api_key,
        "LAWD_CD": lawd_cd,
        "DEAL_YMD": deal_ymd,
        "numOfRows": "2000",
        "pageNo": "1",
    }
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            resp = requests.get(RTMS_URL, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            result_code = root.findtext(".//resultCode")
            if result_code != "000":
                raise RuntimeError(f"RTMS API error {result_code}: {root.findtext('.//resultMsg')}")
            return [
                {child.tag: (child.text or "").strip() for child in item}
                for item in root.findall(".//item")
            ]
        except Exception as exc:  # network blip or transient gateway reset — retry
            last_err = exc
            time.sleep(3)
    raise RuntimeError(f"{deal_ymd} 조회 {retries}회 재시도 후 실패: {last_err!r}")


def fetch_all_transactions(lawd_cd: str, start_ymd: str, end_ymd: str, api_key: str) -> list[dict]:
    """Every raw transaction row (all 단지/면적, not just 반포자이) for
    `lawd_cd` across every month in [start_ymd, end_ymd] (both "YYYYMM",
    inclusive) — the API only supports querying one district+month at a time,
    so this makes one call per month."""
    rows: list[dict] = []
    cur = start_ymd
    while int(cur) <= int(end_ymd):
        rows.extend(_fetch_month(lawd_cd, cur, api_key))
        cur = _add_month(cur, 1)
    return rows


def build_trend(rows: list[dict]) -> dict:
    """Filter raw transaction rows down to 반포자이의 config.BANPO_EXCLUSIVE_AREA
    평형, then compute a `BANPO_TREND_WINDOW_TRANSACTIONS`-transaction trailing
    simple moving average of the raw deal amount (KRW), applying
    `BANPO_REPORTING_LAG_DAYS` to each transaction's own deal date before using
    it — real-estate transactions must legally be reported within 30 days of
    contract, so treating a transaction as "known" on its deal date would be a
    look-ahead bias (same category of fix as REB's old publication-lag
    correction, see TRADING_LOGIC.md ⑧ section)."""
    matched = []
    for r in rows:
        if r.get("umdNm") != config.BANPO_UMD_NM or config.BANPO_APT_NAME not in r.get("aptNm", ""):
            continue
        if r.get("excluUseAr") != config.BANPO_EXCLUSIVE_AREA:
            continue
        deal_date = dt.date(int(r["dealYear"]), int(r["dealMonth"]), int(r["dealDay"]))
        amount = int(r["dealAmount"].replace(",", "")) * 10000
        matched.append({"deal_date": deal_date, "amount": amount})
    matched.sort(key=lambda x: x["deal_date"])

    win = config.BANPO_TREND_WINDOW_TRANSACTIONS
    out_rows = []
    for i, m in enumerate(matched):
        window = matched[max(0, i - win + 1): i + 1]
        trend_value = sum(w["amount"] for w in window) / len(window)
        available_date = m["deal_date"] + dt.timedelta(days=config.BANPO_REPORTING_LAG_DAYS)
        out_rows.append({
            "available_date": available_date.isoformat(),
            "trend_value": round(trend_value),
            # 개별 거래 원값(평활화 전) — pages/2_자산비교.py가 추세선과 함께
            # 실제 체결가를 작은 점으로 같이 보여줄 때 쓴다.
            "raw_value": m["amount"],
        })

    return {
        "generated_at": dt.datetime.now().isoformat(),
        "source": (
            "국토교통부 아파트매매 실거래가 공개 데이터(RTMSDataSvcAptTrade), "
            f"{config.BANPO_LAWD_CD}({config.BANPO_UMD_NM}) {config.BANPO_APT_NAME} "
            f"전용 {config.BANPO_EXCLUSIVE_AREA}㎡('국민평형')"
        ),
        "methodology": (
            f"거래일 순 직전 {win}건 단순이동평균(원시 거래금액, 원) — 신고기한(계약일로부터 "
            f"30일 이내) + 안전마진을 더한 {config.BANPO_REPORTING_LAG_DAYS}일 뒤를 '확인 가능일'"
            "(available_date)로 삼아 look-ahead bias를 방지"
        ),
        "transaction_count": len(matched),
        "rows": out_rows,
    }


def main() -> None:
    api_key = os.environ["RTMS_API_KEY"]
    end_ymd = dt.date.today().strftime("%Y%m")
    rows = fetch_all_transactions(config.BANPO_LAWD_CD, config.BANPO_FETCH_START_YM, end_ymd, api_key)
    payload = build_trend(rows)
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {DATA_PATH} ({len(payload['rows'])} trend points from {payload['transaction_count']} transactions)")


if __name__ == "__main__":
    main()

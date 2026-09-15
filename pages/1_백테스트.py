"""Backtest page: real-rate/DXY MA breakout signal (green_count) + 52-week
new-high/new-low triggers, vs. a same-period Buy & Hold benchmark."""

import functools
import json
import uuid
from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io._html as _plotly_html
import streamlit as st

from gold_dashboard import backtest, config, regime
from gold_dashboard import timeseries
from gold_dashboard.timeutil import today_kst

# Profiling fig.to_html(include_plotlyjs="cdn") showed ~40% of its cost was
# plotly.py re-reading its entire bundled plotly.js (~3MB) off disk and
# SHA-256-hashing it from scratch on every single call, purely to fill in
# the CDN <script>'s `integrity=` (Subresource Integrity) attribute — see
# plotly/io/_html.py's `include_plotlyjs == "cdn"` branch. That content
# (get_plotlyjs()) is 100% static for a given plotly.py install, takes no
# arguments, and never changes at runtime, so it's a safe memoization target:
# this computes the exact same hash/output, just once per process instead of
# once per chart render, with the rendered HTML unaffected either way.
# Guarded so re-running this page script (Streamlit reruns the whole module
# top-to-bottom on every interaction) only wraps it the first time.
if not hasattr(_plotly_html.get_plotlyjs, "cache_clear"):
    _plotly_html.get_plotlyjs = functools.lru_cache(maxsize=1)(_plotly_html.get_plotlyjs)


def render_backtest_chart(
    fig: go.Figure,
    regime_payload: dict,
    height: int = 460,
) -> None:
    """Render the 누적수익률 chart with all of its zoom-driven and
    control-driven interactivity. st.plotly_chart renders inside an iframe
    Streamlit controls, with no hook for attaching custom JS, so this
    bypasses it via st.iframe (which allows script execution) and embeds the
    figure's own HTML together with:

    1. Y-axis autoscale on zoom: by default the y-axis stays fixed to the
       full-series range even when the x-axis is zoomed in (rangeslider drag,
       rangeselector button, or box-zoom), which makes a zoomed-in view look
       flat. On every x-range change this recomputes min/max over the
       now-visible points of every currently-visible trace and applies that
       as the new y-range (with a small padding), restoring full y-autorange
       when the x-range resets to "전체".

    2. X-axis year/month tick switching: ticks are computed as an explicit
       (tickvals, ticktext) array rather than a single dtick, so January ticks
       can read as a bare year ("2024") while other month ticks read as
       "4월" etc. — a single d3 tickformat string can't mix those two label
       shapes. On every x-range change (zoom in/out, rangeslider drag,
       rangeselector button, reset to "전체") this recomputes which months
       get a tick from the total visible span: >6년 = Jan only (연도만),
       4~6년 = Jan/Jul, 2~4년 = Jan/Apr/Jul/Oct, ≤2년 = every month. Ticks are
       always generated from real calendar month starts (never snapped to a
       data point), so they can never collide/duplicate regardless of tier.

    3. 국면 음영 라디오 ("표시 안함"/"장기 국면"/"일반 국면", 서로 배타적): 선택된
       세트의 구간을 파랑 계열(상승 계열)·빨강 계열(하락 계열)로 칠한다. 이 음영은
       `yref="y domain"`으로 그려 차트 plot area 높이의 고정 비율(6%)만 차트
       **최하단에 붙여서** 칠한다 — Y축 데이터 값이 아니라 플롯 영역 자체를
       기준으로 하므로, Y축이 확대/축소로 재조정돼도 음영 두께(픽셀 기준)가
       줄어들거나 늘어나지 않고 항상 동일하게 유지된다. 매수/매도 보유기간을
       나타내는 회색 음영은 그대로 데이터 좌표 기준(`yref="y"`)으로 **Y=0% 위쪽만**
       (0%~차트 최상단) 칠해서 국면 음영과 겹치지 않는다 — 이 페이지는 `fig`에
       shape을 미리 그려두지 않고 `regime_payload`로 넘어온 날짜 구간(국면 음영·
       미보유 구간 둘 다)만 가지고 매번 이 함수의 JS가 직접 그린다. X축은 Plotly가
       현재 보이는 구간에 맞춰 자동으로 잘라 그려주므로 줌이 바뀔 때마다 좌표를
       다시 계산할 필요가 없지만, 회색 음영의 Y축 최댓값은 Y-autoscale로 줌마다
       바뀌므로 y축이 재조정될 때마다(국면 음영은 라디오 선택이 바뀔 때도) 다시
       계산한다.

    4. 신호전략/Buy & Hold 라인 표시 체크박스: 신호전략 체크박스는 보유·미보유
       라인 2개와 매수·매도 마커까지 함께 숨긴다 — 각 트레이스에 심어둔 `meta`
       태그("line_bh"/"line_strategy"/"marker_strategy")로 구분한다.

    5. 신호강도(6/0·5/1·4/2) 드롭다운 + 5개 국면(대세상승/상승/보합/하락/대세하락)
       참여율 카드: `regime_payload`로 넘어온 일별 국면 라벨과 임계값별 보유
       배열(둘 다 파이썬에서 미리 계산해 그대로 임베드)을 가지고, 현재 보이는
       x축 구간(줌 상태)에 맞춰 서버 왕복 없이 그 자리에서 재계산한다. 음영
       라디오 상태와는 무관하게 항상 5개 국면 전부를 보여준다(요구사항).
    """
    div_id = f"pyauto_{uuid.uuid4().hex}"
    plot_html = fig.to_html(
        include_plotlyjs="cdn",
        full_html=False,
        div_id=div_id,
        config={"responsive": True},
    )
    payload_id = f"{div_id}_payload"
    payload_json = json.dumps(regime_payload, ensure_ascii=False)
    card_labels = regime.REGIME_LABELS

    controls_top = f"""
<div style="display:flex;gap:20px;flex-wrap:wrap;align-items:center;
     font-size:14px;margin-bottom:6px;font-family:inherit;">
  <div>
    <span style="font-weight:600;margin-right:6px;">국면 표시</span>
    <label style="margin-right:10px;"><input type="radio" name="regimeSet_{div_id}"
        class="regimeSetRadio_{div_id}" value="none" checked> 표시 안함</label>
    <label style="margin-right:10px;"><input type="radio" name="regimeSet_{div_id}"
        class="regimeSetRadio_{div_id}" value="A"> 장기 국면 표시</label>
    <label><input type="radio" name="regimeSet_{div_id}"
        class="regimeSetRadio_{div_id}" value="B"> 일반 국면 표시</label>
  </div>
  <div>
    <label style="margin-right:10px;"><input type="checkbox" id="chkStrategy_{div_id}"
        checked> 신호전략(기대수익률 포함)</label>
    <label style="margin-right:10px;"><input type="checkbox" id="chkBH_{div_id}" checked> Buy &amp; Hold</label>
    <label><input type="checkbox" id="chkNetFees_{div_id}" checked> 수수료 반영</label>
  </div>
</div>
"""

    cards_html = "".join(
        f"""<div style="border:1px solid #d8dbe0;border-radius:8px;padding:8px 14px;
             min-width:88px;text-align:center;">
          <div style="font-size:12px;color:#666;">{lbl}</div>
          <div id="card_{div_id}_{i}" style="font-size:18px;font-weight:700;">–</div>
        </div>"""
        for i, lbl in enumerate(card_labels)
    )
    controls_bottom = f"""
<div style="margin-top:10px;font-size:14px;font-family:inherit;">
  <div style="margin-bottom:8px;">
    <span style="font-weight:600;margin-right:6px;">신호강도</span>
    <select id="selThreshold_{div_id}">
      <option value="6/0" selected>6/0</option>
      <option value="5/1">5/1</option>
      <option value="4/2">4/2</option>
    </select>
    <span style="color:#888;margin-left:8px;">국면 참여율(선택된 신호강도가 해당
      국면의 거래일 중 보유 상태였던 비율 · 현재 보이는 기간 기준)</span>
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;">{cards_html}</div>
</div>
"""

    script = f"""
<script type="application/json" id="{payload_id}">{payload_json}</script>
<script>
(function() {{
    var gd = document.getElementById("{div_id}");
    if (!gd) return;
    var busy = false;
    // Set while updateShapes() issues its own Plotly.relayout({{shapes: ...}})
    // call, so onRelayout doesn't mistake that shapes-only relayout event for
    // a user-driven x-zoom and reset the view (Plotly.relayout fires
    // "plotly_relayout" for *any* layout change, shapes included).
    var suppressRelayout = false;
    var regimeData = JSON.parse(document.getElementById("{payload_id}").textContent);
    var cardLabels = {json.dumps(card_labels, ensure_ascii=False)};
    var currentRegimeSet = "none";
    var currentThreshold = "6/0";

    // Plotly.py's default JSON encoder writes numeric arrays in a compact
    // {{dtype, bdata}} (base64) wire format instead of a plain JSON array.
    // plotly.js does NOT decode this back into a plain/typed array on
    // gd.data[i].{{x,y}} (it only decodes it internally for its own
    // rendering/autorange math), so reading tr.y[i] directly on that compact
    // form silently returns undefined for every point. Decode it ourselves
    // rather than depend on any Plotly-internal (e.g. gd._fullData) copy.
    function toArray(v) {{
        if (v == null) return null;
        if (Array.isArray(v) || ArrayBuffer.isView(v)) return v;
        if (typeof v === "object" && typeof v.bdata === "string") {{
            if (v.__decoded) return v.__decoded;
            var bin = atob(v.bdata);
            var buf = new ArrayBuffer(bin.length);
            var bytes = new Uint8Array(buf);
            for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            var ctors = {{
                f8: Float64Array, f4: Float32Array,
                i1: Int8Array, u1: Uint8Array,
                i2: Int16Array, u2: Uint16Array,
                i4: Int32Array, u4: Uint32Array,
            }};
            var decoded = new (ctors[v.dtype] || Float64Array)(buf);
            v.__decoded = decoded;
            return decoded;
        }}
        return null;
    }}

    function visibleYRange(x0, x1) {{
        var t0 = new Date(x0).getTime();
        var t1 = new Date(x1).getTime();
        var ymin = Infinity, ymax = -Infinity;
        (gd.data || []).forEach(function(tr) {{
            if (tr.visible === false || tr.visible === "legendonly") return;
            var xs = toArray(tr.x), ys = toArray(tr.y);
            if (!xs || !ys) return;
            for (var i = 0; i < xs.length; i++) {{
                var xv = new Date(xs[i]).getTime();
                var yv = ys[i];
                if (yv === null || yv === undefined || isNaN(yv)) continue;
                if (xv >= t0 && xv <= t1) {{
                    if (yv < ymin) ymin = yv;
                    if (yv > ymax) ymax = yv;
                }}
            }}
        }});
        if (!isFinite(ymin) || !isFinite(ymax)) return null;
        if (ymin === ymax) {{ ymin -= 0.01; ymax += 0.01; }}
        var pad = (ymax - ymin) * 0.08;
        return [ymin - pad, ymax + pad];
    }}

    // 총 표시 기간(연 단위)에 따라 매년 몇 개월에 눈금을 찍을지 결정 — 1월은
    // 항상 포함(연도 레이블), 나머지는 아래 규칙의 달만 추가로 포함해 "N월"로
    // 표시한다. dtick 하나로는 "1월만 연도, 나머지는 월"처럼 서로 다른 두 라벨
    // 형식을 섞을 수 없어서, tickvals/ticktext를 직접 계산해 넘긴다.
    var MS_PER_YEAR = 365.25 * 24 * 60 * 60 * 1000;
    function monthsForSpan(yearsSpan) {{
        if (yearsSpan <= 2) return [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];
        if (yearsSpan <= 4) return [0, 3, 6, 9];
        if (yearsSpan <= 6) return [0, 6];
        return [0];
    }}
    function xTickUpdates(x0, x1) {{
        var t0 = new Date(x0);
        var t1 = new Date(x1);
        var yearsSpan = (t1.getTime() - t0.getTime()) / MS_PER_YEAR;
        var months = monthsForSpan(yearsSpan);
        // 보이는 구간 양쪽으로 1년씩 여유를 둬서, 확대/축소 중 살짝 벗어난
        // 연도의 눈금도 항상 준비돼 있도록 한다(Plotly가 실제 보이는 x범위
        // 밖의 틱은 알아서 그리지 않으므로 여기서 굳이 딱 잘라낼 필요는 없음).
        var startYear = t0.getUTCFullYear() - 1;
        var endYear = t1.getUTCFullYear() + 1;
        var tickvals = [], ticktext = [];
        for (var y = startYear; y <= endYear; y++) {{
            months.forEach(function(m) {{
                tickvals.push(new Date(Date.UTC(y, m, 1)).toISOString());
                ticktext.push(m === 0 ? String(y) : (m + 1) + "월");
            }});
        }}
        return {{
            "xaxis.tickmode": "array",
            "xaxis.tickvals": tickvals,
            "xaxis.ticktext": ticktext,
        }};
    }}

    var fullXRange = null;
    function fullXExtent() {{
        if (fullXRange) return fullXRange;
        var xmin = Infinity, xmax = -Infinity;
        (gd.data || []).forEach(function(tr) {{
            var xs = toArray(tr.x);
            if (!xs) return;
            for (var i = 0; i < xs.length; i++) {{
                var xv = new Date(xs[i]).getTime();
                if (xv < xmin) xmin = xv;
                if (xv > xmax) xmax = xv;
            }}
        }});
        fullXRange = [new Date(xmin).toISOString(), new Date(xmax).toISOString()];
        return fullXRange;
    }}

    // ---- 국면 참여율 카드: 현재 보이는 x축 구간(줌 상태) 안의 날짜만, 선택된
    // 신호강도의 보유 배열과 5국면 라벨 배열을 이용해 그 자리에서 재계산 ----
    function updateCards() {{
        var xr = (gd.layout.xaxis && gd.layout.xaxis.range) ? gd.layout.xaxis.range : fullXExtent();
        var t0 = new Date(xr[0]).getTime();
        var t1 = new Date(xr[1]).getTime();
        var dates = regimeData.dates;
        var labels = regimeData.regime5;
        var held = regimeData.held[currentThreshold] || [];
        var sums = {{}}, counts = {{}};
        cardLabels.forEach(function(l) {{ sums[l] = 0; counts[l] = 0; }});
        for (var i = 0; i < dates.length; i++) {{
            var tv = new Date(dates[i]).getTime();
            if (tv < t0 || tv > t1) continue;
            var lbl = labels[i];
            if (!(lbl in counts)) continue;
            counts[lbl] += 1;
            sums[lbl] += held[i];
        }}
        cardLabels.forEach(function(l, i) {{
            var el = document.getElementById("card_{div_id}_" + i);
            if (!el) return;
            el.textContent = counts[l] > 0 ? (sums[l] / counts[l] * 100).toFixed(1) + "%" : "–";
        }});
    }}

    // ---- 국면 음영 shapes: 기존(회색 미보유구간) shapes는 항상 유지하고, 그
    // 위에 라디오로 선택된 세트(A/B/없음)의 shapes만 갈아끼운다. Y축은 0%(수익률
    // 기준선) 위로는 칠하지 않고 차트 최하단(현재 Y축 최솟값)~0%까지만 채워서,
    // 매수/매도 보유기간을 나타내는 회색 음영(전체 높이)과 시각적으로 겹치지
    // 않게 한다 — 그 구간의 국면 여부 자체는 하단 참여율 카드로 이미 보여주므로,
    // 음영 자체가 0% 위쪽(수익률이 플러스인 영역)까지 덮을 필요는 없다. ----
    function currentYTop() {{
        if (gd.layout.yaxis && Array.isArray(gd.layout.yaxis.range)) {{
            return gd.layout.yaxis.range[1];
        }}
        var full = fullXExtent();
        var yr = visibleYRange(full[0], full[1]);
        return yr ? yr[1] : 0;
    }}
    // 차트 plot area 높이의 고정 비율(6%)로 그린다 — yref를 데이터 좌표("y")가
    // 아니라 "y domain"(0=축 최하단, 1=축 최상단, Y축 스케일과 무관한 상대
    // 비율)으로 두면 Y-autoscale로 확대/축소해 Y축 값 범위가 바뀌어도 이 띠의
    // 두께(픽셀 기준)가 얇아지거나 두꺼워지지 않고 항상 동일하게 유지된다.
    var REGIME_BAND_DOMAIN_FRAC = 0.06;
    function shapeDictsFor(setName) {{
        if (setName === "none") return [];
        var list = setName === "A" ? regimeData.shapesA : regimeData.shapesB;
        return list.map(function(s) {{
            return {{
                type: "rect", xref: "x", yref: "y domain",
                x0: s.x0, x1: s.x1, y0: 0, y1: REGIME_BAND_DOMAIN_FRAC,
                // 선명한 파랑/빨강 계열 — 0.15~0.25 범위 안에서 라인·마커가
                // 가려지지 않는 선의 최대치인 0.25로 더 진하게.
                fillcolor: s.kind === "up" ? "rgba(37,99,235,0.25)" : "rgba(220,38,38,0.25)",
                line: {{width: 0}}, layer: "below",
            }};
        }});
    }}
    // ---- 미보유(회색) 구간 shapes: 국면 음영과 대칭으로, Y=0%~차트 최상단(현재
    // Y축 최댓값)까지만 칠해서 국면 음영(0%~최하단)과 절대 겹치지 않는다. 이제는
    // 정적으로 fig에 미리 그려두지 않고, 국면 음영과 마찬가지로 Y축이 재조정될
    // 때마다 이 함수가 그 자리에서 다시 계산한다. ----
    function nonHoldingShapeDicts(yTop) {{
        var bands = regimeData.nonHoldingBands || [];
        return bands.map(function(b) {{
            return {{
                type: "rect", xref: "x", yref: "y",
                x0: b.x0, x1: b.x1, y0: 0, y1: yTop,
                fillcolor: "rgba(90,96,104,0.32)",
                line: {{width: 0}}, layer: "below",
            }};
        }});
    }}
    function updateShapes() {{
        // 요청된 렌더링 순서(국면 음영이 맨 아래, 그 위에 보유기간 회색 음영)
        // 그대로 배열 순서에 반영 — 다만 둘 다 layer:"below"라서 어느 쪽이든
        // Buy&Hold/신호전략 라인과 매수·매도 마커(둘 다 실제 trace)보다는 항상
        // 아래에 그려진다; 이 순서는 두 shape끼리의 상대적 배치만 결정한다.
        var shapes = shapeDictsFor(currentRegimeSet)
            .concat(nonHoldingShapeDicts(currentYTop()));
        suppressRelayout = true;
        Plotly.relayout(gd, {{shapes: shapes}}).then(function() {{ suppressRelayout = false; }});
    }}

    // ---- 신호전략/Buy&Hold 라인 표시 체크박스 (신호전략은 마커까지 함께) +
    // 수수료 반영/미반영(_net/_gross) 토글 — 세 체크박스를 조합해서 4개 트레이스
    // 그룹(전략_net/전략_gross/BH_net/BH_gross)의 visible을 각각 계산한다.
    // "수수료 반영"은 _net과 _gross를 서로 배타적으로 전환할 뿐 신호전략/BH
    // 표시 여부 자체와는 독립적이라, showStrategy·showBH가 꺼져 있으면 그
    // 그룹은 net/gross 상태와 무관하게 항상 숨는다.
    function indicesForMeta(tag) {{
        var idx = [];
        (gd.data || []).forEach(function(tr, i) {{ if (tr.meta === tag) idx.push(i); }});
        return idx;
    }}
    function updateLineVisibility() {{
        var showStrategy = document.getElementById("chkStrategy_{div_id}").checked;
        var showBH = document.getElementById("chkBH_{div_id}").checked;
        var showNet = document.getElementById("chkNetFees_{div_id}").checked;
        var stratNetIdx = indicesForMeta("line_strategy_net").concat(indicesForMeta("marker_strategy_net"));
        var stratGrossIdx = indicesForMeta("line_strategy_gross").concat(indicesForMeta("marker_strategy_gross"));
        var bhNetIdx = indicesForMeta("line_bh_net");
        var bhGrossIdx = indicesForMeta("line_bh_gross");
        if (stratNetIdx.length) Plotly.restyle(gd, {{visible: showStrategy && showNet}}, stratNetIdx);
        if (stratGrossIdx.length) Plotly.restyle(gd, {{visible: showStrategy && !showNet}}, stratGrossIdx);
        if (bhNetIdx.length) Plotly.restyle(gd, {{visible: showBH && showNet}}, bhNetIdx);
        if (bhGrossIdx.length) Plotly.restyle(gd, {{visible: showBH && !showNet}}, bhGrossIdx);
    }}

    function onRelayout(ev) {{
        if (busy || suppressRelayout) return;
        var x0 = ev["xaxis.range[0]"];
        var x1 = ev["xaxis.range[1]"];
        if ((x0 === undefined || x1 === undefined) && Array.isArray(ev["xaxis.range"])) {{
            x0 = ev["xaxis.range"][0];
            x1 = ev["xaxis.range"][1];
        }}
        if (ev["xaxis.autorange"] === true || x0 === undefined || x1 === undefined) {{
            var full = fullXExtent();
            var updates = xTickUpdates(full[0], full[1]);
            updates["yaxis.autorange"] = true;
            busy = true;
            Plotly.relayout(gd, updates).then(function() {{
                busy = false;
                updateCards();
                updateShapes(); // y축이 새로 자동조정됐으니 회색 음영의 y1(상단)도 맞춰 다시 계산
            }});
            return;
        }}
        var yr = visibleYRange(x0, x1);
        var updates = xTickUpdates(x0, x1);
        if (yr) {{
            updates["yaxis.range"] = yr;
            updates["yaxis.autorange"] = false;
        }}
        busy = true;
        Plotly.relayout(gd, updates).then(function() {{
            busy = false;
            updateCards();
            updateShapes(); // 줌으로 y축 범위가 바뀌었으니 회색 음영의 y1(상단)도 그에 맞춰 재계산
        }});
    }}
    gd.on("plotly_relayout", onRelayout);

    document.querySelectorAll(".regimeSetRadio_{div_id}").forEach(function(r) {{
        r.addEventListener("change", function() {{
            if (this.checked) {{ currentRegimeSet = this.value; updateShapes(); }}
        }});
    }});
    document.getElementById("chkStrategy_{div_id}").addEventListener("change", updateLineVisibility);
    document.getElementById("chkBH_{div_id}").addEventListener("change", updateLineVisibility);
    document.getElementById("chkNetFees_{div_id}").addEventListener("change", updateLineVisibility);
    document.getElementById("selThreshold_{div_id}").addEventListener("change", function() {{
        currentThreshold = this.value;
        updateCards();
    }});

    // 페이지 로드 시점에도 전체 기간 기준으로 연/월 틱을 한 번 맞춰 준다(줌
    // 조작이 있기 전까지는 fig가 Python에서 설정한 정적 dtick="M12"만 갖고
    // 있으므로) — suppressRelayout으로 감싸서 이 자체 relayout이 onRelayout의
    // x-줌 감지 로직을 건드리지 않게 한다.
    var initFull = fullXExtent();
    suppressRelayout = true;
    Plotly.relayout(gd, xTickUpdates(initFull[0], initFull[1])).then(function() {{
        suppressRelayout = false;
        updateCards();
        updateShapes(); // 회색 미보유 음영이 이제 정적 shape이 아니라 여기서 처음 그려짐
    }});
}})();
</script>
"""
    st.iframe(controls_top + plot_html + controls_bottom + script, height=height + 260)

# Page config (title/layout) is centralized in app.py's main(), since
# st.navigation there replaces the classic pages/-folder auto-discovery this
# file used to rely on for its own page config, and st.set_page_config can
# only be called once per app run.

# dataviz reference palette: strategy/benchmark use categorical slots 2-3 (orange/aqua)
# so they stay visually distinct from the buy/sell markers below, which reuse the
# conventional blue=buy / red=sell pair (slots 1 and 8).
STRATEGY_COLOR = "#eb6834"
BH_COLOR = "#1baf7a"
BUY_COLOR = "#2a78d6"
SELL_COLOR = "#e34948"
# Third line (신호전략 + 기대수익률 포함) gets its own categorical slot (violet)
# distinct from both STRATEGY_COLOR and BH_COLOR — never red/pink, since a
# lighter tint of this is used for the non-holding stretch and red/pink would
# read as "loss" there. NONHOLDING is that lighter tint (mixed toward white),
# used only for the dashed non-holding segments of the hybrid line.
STRATEGY_HYBRID_COLOR = "#7b5ea8"
STRATEGY_HYBRID_NONHOLDING_COLOR = "#c9bfe0"
STRATEGY_LABEL = "신호전략"
BH_LABEL = "Buy & Hold"

# 누적수익률 차트의 국면 음영/참여율 카드가 쓰는 국면 구간표·5단계 분류·shape
# 변환 헬퍼는 gold_dashboard/regime.py로 옮겨 대시보드 핵심 요약 문구([1]
# 참여율 자동 계산 요구사항)와 공유하는 단일 소스로 관리한다 — 아래
# `regime.REGIME_UPTREND` 등으로 참조.

DEFAULTS = {
    "bt_years": backtest.BACKTEST_YEARS,
    "bt_asof_years_ago": 0,
    "bt_buy_green_count": backtest.BUY_GREEN_COUNT,
    "bt_sell_green_count": backtest.SELL_GREEN_COUNT,
    "bt_use_new_high_trigger": backtest.DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER,
    "bt_use_new_low_trigger": backtest.DEFAULT_USE_FIFTY_TWO_WEEK_LOW_TRIGGER,
    "bt_min_holding_days": backtest.DEFAULT_MIN_HOLDING_DAYS,
    "bt_bond_yield_pct": backtest.DEFAULT_BOND_ANNUAL_YIELD * 100.0,
    "bt_use_sell_noise_filter": True,
    "bt_use_daily_band_confirmation": backtest.DEFAULT_SELL_NOISE_USE_DAILY_BAND,
    "bt_sell_noise_filter_drop_pct": backtest.DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
    "bt_use_buy_noise_filter": True,
    "bt_use_buy_daily_band_confirmation": backtest.DEFAULT_BUY_NOISE_USE_DAILY_BAND,
    "bt_buy_noise_filter_rise_pct": backtest.DEFAULT_BUY_NOISE_FILTER_RISE_PCT,
    "bt_apply_fees": True,
    "bt_buy_fee_pct": backtest.DEFAULT_BUY_FEE_PCT,
    "bt_sell_fee_pct": backtest.DEFAULT_SELL_FEE_PCT,
    "bt_daily_holding_fee_pct": backtest.DEFAULT_DAILY_HOLDING_FEE_PCT,
}
for _key, _default in DEFAULTS.items():
    st.session_state.setdefault(_key, _default)

st.title("신호 기반 매매 전략 백테스트")
st.caption(
    "실질금리·달러인덱스의 이동평균 돌파 신호와 52주 신고가/신저가 갱신을 결합한 매수·매도 "
    "규칙을, 동일 시작일의 Buy & Hold와 비교합니다."
)

# Shared with the main dashboard page via config.GOLD_PRICE_BASIS_STATE_KEY —
# but NOT via that key's own widget binding: st.navigation resets a widget's
# session_state entry back to its default the instant that exact widget isn't
# instantiated in a run (i.e. the moment you navigate to a different page), so
# a `key=` shared across two pages' widgets does NOT survive navigation
# between them (verified directly against this Streamlit version). The fix is
# to keep the shared choice in that plain session_state entry (which does
# survive navigation) and seed this page's own, page-local widget from it via
# `index=`, writing the widget's result straight back after every rerun.
# Deliberately NOT part of DEFAULTS above: it's a data-source choice, not a
# backtest tuning parameter, so "기본값으로 초기화" leaves it untouched.
_gold_basis_options = [config.GOLD_PRICE_BASIS_INTL, config.GOLD_PRICE_BASIS_KRX]
st.session_state.setdefault(config.GOLD_PRICE_BASIS_STATE_KEY, config.GOLD_PRICE_BASIS_DEFAULT)
gold_price_basis = st.radio(
    "금 가격 기준",
    options=_gold_basis_options,
    format_func=lambda v: config.GOLD_PRICE_BASIS_LABELS[v],
    index=_gold_basis_options.index(st.session_state[config.GOLD_PRICE_BASIS_STATE_KEY]),
    key="_gold_price_basis_widget_backtest",
    horizontal=True,
    help="이 페이지 전체(이동평균·장기추세 필터·매수매도 신호·백테스트·요약지표·그래프)가 이 "
    "기준으로 다시 계산됩니다. 대시보드 페이지와 상태를 공유하므로 여기서 바꾸면 그쪽에도 "
    "반영됩니다.",
)
st.session_state[config.GOLD_PRICE_BASIS_STATE_KEY] = gold_price_basis
st.caption(
    "② KRX 금현물은 환율을 곱해 환산한 값이 아니라, KRX 금현물시장(04020000, \"금 99.99_1kg\") "
    "실제 국내 시세(KRW/g)를 그대로 사용합니다(출처: Naver 증권). ② 선택 시 최초 데이터 수집에 "
    "1분 내외 걸릴 수 있습니다(이후 캐시되어 즉시 표시)."
)

with st.expander("전략 규칙 보기"):
    st.markdown(
        """
- **매수** (미보유 상태일 때만): 아래 "매수 조건" 카드의 `green_count ≥ 임계값` — 지연 없이
  **당일 즉시 매수**. **또는** (고급 설정의 **52주 신고가 갱신 시 매수**가 켜져 있을 때만,
  기본값 ON) 금 종가가 직전 365일(역일) 중 최고 종가를 처음으로 넘어서는 날(52주 신고가
  신규 경신일) — 이것도 당일 즉시 매수, 자체 빈도 제한 없음. 아무 조건이나 먼저 만족하면
  매수합니다
- **매도** (보유 상태일 때만): "매도 조건" 카드의 `green_count ≤ 임계값` — 지연 없이 **당일
  즉시 매도**. **또는** (고급 설정의 **52주 신저가 갱신 시 매도**가 켜져 있을 때만, 기본값 ON)
  금 종가가 직전 365일(역일) 중 최저 종가보다 낮아지는 날(52주 신저가 신규 경신일) — 이것도
  당일 즉시 매도, 자체 빈도 제한 없음. 둘 중 아무 조건이나 먼저 만족하면 매도합니다
- 모든 매수·매도 조건은 지연 없이 신호 당일 종가에 즉시 체결됩니다(과거에 있던 지연 체결
  기능은 완전히 제거됨)
- 고급 설정의 **최소 보유일수**(역일/달력일 기준, 주말·공휴일 관계없이 매수일로부터의 날짜
  차이로 계산)를 설정하면, 매수 후 그 일수가 지나기 전까지는 매도 조건(green_count·52주
  신저가 갱신 모두)을 아예 확인하지 않습니다 — 단기 매매가 아니라 최소 보유 기간을
  두는 전략을 시뮬레이션할 때 사용
- 고급 설정의 **상승추세 중 매도신호 노이즈 필터** (기본값 ON)는 매도신호가 실제로
  체결되기 직전(green_count 또는 52주 신저가 갱신)에 개입합니다.
  그날(D0) 종가가 180일(역일) 이동평균보다 5% 이상 높을 때만 작동하며
  (미만이면 이 필터 없이 항상 그대로 즉시 매도), 작동하면 D0의 매도신호는 무시하고 관찰을
  시작합니다. 관찰 중 추가로 뜨는 매도신호는 매도 여부에 전혀 영향을 주지 않으며 참고용
  기록으로만 남습니다. 매도 실행 여부를 확인하는 방식은 **매도 확인 - 매일 갱신 2시그마 밴드**
  설정에 따라 둘 중 하나입니다:
  - **켜짐(기본값)**: D0 이후 1~7거래일은 하락폭과 무관하게 **무조건 보류**합니다(며칠 사이의
    등락은 랜덤워크 노이즈일 확률이 커서, 그 정도 기간만으로는 진짜 하락인지 판단하지 않음).
    8거래일차부터 21거래일차(약 3주)까지는 매일, 그날까지 경과한 거래일수의 제곱근에 비례해
    넓어지는 확인 밴드를 계산합니다 — `그날의 밴드(%) = 2 × 일간표준편차 × √(경과 거래일수)`,
    일간표준편차는 30년 금 가격 기준 월간 변동성(4.9%)을 √21(한 달 거래일수)로 나눠 환산(예:
    8거래일차 -6.05%, 10거래일차 -6.76%, 14거래일차 -8.00%, 21거래일차 -9.80%). 종가가 D0
    종가 대비 그날의 밴드만큼(또는 그 이상) 하락한 **첫날** 즉시 매도합니다. 21거래일 동안
    한 번도 도달하지 못하면 관찰을 종료하고 D0의 신호는 없었던 것으로 처리합니다(다음
    매도신호부터 처음부터 다시 시작 — 대세 상승장 중 며칠 새 노이즈나 완만한 조정 때문에
    일찍 매도되는 것을 방지하되, 8일차 이후로는 하락 속도가 빠를수록 더 일찍 확인되도록 설계).
  - **꺼짐**: D0+7일(역일 기준) 고정 시점의 종가만을 D0 종가와 비교합니다 — "매도 확인
    하락률"(기본 5%) 이상 낮으면 그날 매도, 그만큼 낮지 않으면 관찰모드를 해제하고 D0의 신호는
    없었던 것으로 처리합니다
- 고급 설정의 **하락추세 중 매수신호 노이즈 필터** (기본값 ON)는 위 매도신호 노이즈 필터를
  방향만 반대로 완전히 대칭시킨 조건입니다. 매수신호(green_count·52주 신고가 갱신
  무엇이든)가 실제로 체결되기 직전에 개입하며, 그날(D0) 종가가 180일(역일) 이동평균보다 5%
  이상 낮을 때만 작동합니다(그 이상이면 이 필터 없이 항상 그대로 즉시 매수). 작동하면 D0의
  매수신호는 무시하고 미보유 상태를 유지하며 관찰을 시작합니다. 확인 방식도 매도 쪽과
  동일하게 **매수 확인 - 매일 갱신 2시그마 밴드**(기본 ON, 하락 대신 상승 기준 — 8거래일차
  +6.05% ~ 21거래일차 +9.80%) 또는 D0+7일 고정 시점("매수 확인 상승률", 기본 5%) 중 하나로
  전환할 수 있습니다.
- 고급 설정의 **수수료**(② KRX 금현물 선택 시에만 적용, ① 국제 금 시세에는 적용되지 않음)는
  세 가지로 나뉩니다: **매수/매도 수수료**(각 기본 0.165%, 편도)는 매수·매도 체결이 일어날
  때마다 그 시점에 1회성으로 차감되어 매매 횟수에 비례해 총액이 늘어나고, **보관수수료**
  (기본 0.00022%, 일률)는 보유 잔량에 매일 누적됩니다(신호전략은 실제 보유 중일 때만, Buy &
  Hold는 전체 기간). "수수료 반영" 체크박스로 전부 껐다 켤 수 있고, 차트에서는 이 값과
  별개로 수수료 반영/미반영 곡선을 토글로 비교할 수 있습니다
- 분석 기간: **{years}년** (1~15년 조정 가능, 이동평균 계산용으로 그 이전 {buffer}캘린더일치
  데이터를 추가로 사용). 기본은 오늘을 기준으로 최근 {years}년이지만, "기준일 (오늘로부터
  N년 전)"을 0보다 크게 설정하면 분석 종료일 자체가 그만큼 과거로 이동합니다 — 예:
  분석 기간 10년 + 기준일 3년 전이면 "13년 전 ~ 3년 전"을 분석합니다(현재 설정:
  기준일 {asof_years_ago}년 전)
        """.format(
            years=int(st.session_state["bt_years"]),
            buffer=backtest.BUFFER_DAYS,
            asof_years_ago=int(st.session_state["bt_asof_years_ago"]),
        )
    )

header_col, reset_col = st.columns([5, 1])
with header_col:
    st.subheader("분석 기간 · 매수·매도 조건")
with reset_col:
    st.write("")
    if st.button("↺ 기본값으로 초기화", use_container_width=True):
        for _key, _default in DEFAULTS.items():
            st.session_state[_key] = _default
        st.rerun()

# Independent of the main dashboard: these two keys (bt_asof_years_ago,
# bt_years) are only ever read or written on this page, and the main
# dashboard's per-indicator charts always fetch a fixed CHART_YEARS window
# regardless of what's set here. Must be instantiated after the reset button
# above (Streamlit forbids writing to a widget's session_state key once that
# widget has been instantiated in the same script run).
asof_col, years_col = st.columns(2)
with asof_col:
    asof_years_ago = st.number_input(
        "기준일 (오늘로부터 N년 전)",
        min_value=0,
        max_value=backtest.MAX_BACKTEST_YEARS,
        step=1,
        key="bt_asof_years_ago",
        help="0이면(기본값) 오늘을 기준으로 분석 종료일을 잡습니다(기존과 동일한 동작). N을 "
        "입력하면 분석 종료일 자체가 오늘로부터 N년 전으로 이동하고, 분석 시작일은 거기서 "
        "다시 '분석 기간'만큼 더 과거로 이동합니다 — 예: 분석 기간 10년 + 기준일 3년 전이면 "
        "'오늘로부터 13년 전 ~ 3년 전'을 분석합니다(최근 급등기 등 특정 구간을 일부러 "
        "제외하고 과거 구간만 보고 싶을 때 사용).",
    )
with years_col:
    years = st.number_input(
        "분석 기간 (기준일로부터 N년)",
        min_value=backtest.MIN_BACKTEST_YEARS,
        max_value=backtest.MAX_BACKTEST_YEARS,
        step=1,
        key="bt_years",
        help="이 페이지의 백테스트 결과(거래 내역·승률·CAGR·아래 그래프)에만 영향을 줍니다 — "
        "메인 대시보드의 지표별 그래프는 이 값과 무관하게 항상 고정된 기간으로 표시됩니다.",
    )

# today_kst() - N*365일 = 분석 종료일(기준일). N=0이면 오늘 그대로라 이전과 완전히 동일하게
# 동작함(하위 호환).
as_of_date = today_kst() - timedelta(days=int(asof_years_ago) * 365)

if gold_price_basis == config.GOLD_PRICE_BASIS_KRX and timeseries.gold_window_would_clamp_to_krx(
    as_of_date, int(years), backtest.BUFFER_DAYS
):
    st.info(
        f"KRX 금현물시장은 {config.KRX_GOLD_EARLIEST_DATE} 이후 데이터만 존재합니다. "
        f"분석 시작일이 자동으로 {config.KRX_GOLD_EARLIEST_DATE}로 조정됩니다(이동평균 "
        "계산용 사전 데이터가 짧아지는 만큼, 분석 기간 첫 구간의 180일선/장기추세 필터·"
        "52주 신고가·신저가 판정 신뢰도가 낮을 수 있습니다)."
    )

buy_card, sell_card = st.columns(2)
with buy_card:
    with st.container(border=True):
        st.markdown("#### 🔵 매수 조건")
        buy_green_count = st.number_input(
            "green_count 임계값 (이상)",
            min_value=0, max_value=6, step=1, key="bt_buy_green_count",
            help="실질금리·달러인덱스 × 7/30/90일(역일) 이평선, 총 6개 셀 중 금값에 우호적인 셀 수가 "
            "이 값 이상이면 그날 즉시 매수 신호.",
        )
with sell_card:
    with st.container(border=True):
        st.markdown("#### 🔴 매도 조건")
        sell_green_count = st.number_input(
            "green_count 임계값 (이하)",
            min_value=0, max_value=6, step=1, key="bt_sell_green_count",
            help="금값에 우호적인 셀 수가 이 값 이하로 떨어지면 그날 즉시 매도 신호.",
        )

if sell_green_count >= buy_green_count:
    st.warning(
        "매도 임계값이 매수 임계값보다 크거나 같습니다. 매수 즉시 매도 조건도 함께 만족해 "
        "거의 바로 청산될 수 있습니다."
    )

with st.expander("⚙️ 고급 설정 (최소 보유일수 등 — 기본값 그대로 둬도 무방)"):
    st.caption(
        "모든 매수·매도 조건은 지연 없이 항상 신호 당일 즉시 체결됩니다."
    )
    adv_buy_col, adv_sell_col = st.columns(2)
    with adv_buy_col:
        st.markdown("**매수 관련**")
        use_new_high_trigger = st.checkbox(
            "52주 신고가 갱신 시 매수",
            key="bt_use_new_high_trigger",
            help="종가가 직전 365일(역일) 중 최고 종가보다 높아지는 날(신규 52주 신고가 경신일, "
            "단순히 '지금 52주 최고가 상태'가 아니라 그날 처음 갱신된 경우만) 즉시 매수합니다"
            "(green_count와 무관하게 추가로 작동, 둘 중 아무거나 먼저 만족해도 매수). 이 "
            "트리거에는 자체 쿨다운이 없어 신고가를 경신할 때마다(단, 이미 보유 중이면 매수를 "
            "다시 하지 않음) 작동합니다.",
        )
        use_buy_noise_filter = st.checkbox(
            "하락추세 중 매수신호 노이즈 필터 (180일선 -5% 이하)",
            key="bt_use_buy_noise_filter",
            help="아래 '상승추세 중 매도신호 노이즈 필터'를 매수 쪽에 그대로 대칭시킨 조건입니다. "
            "매수신호가 발생한 날(D0) 종가가 180일 이동평균보다 "
            f"{backtest.DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT:g}% 이상 낮을 때만 작동합니다(그 이상이면 "
            "이 필터와 무관하게 항상 즉시 매수). 켜두면: D0의 매수신호는 무시하고 미보유를 유지하며, "
            "아래 '매수 확인 - 매일 갱신 2시그마 밴드' 설정에 따라 매일 확대되는 밴드(기본) 또는 "
            f"D0+{backtest.BUY_NOISE_FILTER_WINDOW_DAYS}일(역일 기준) 고정 시점(끄면) 방식으로 매수 "
            "여부를 확인합니다. 관찰 기간 중 추가로 뜨는 매수신호는 매수 여부에 전혀 영향을 주지 "
            "않고 참고 기록으로만 남습니다. 끄면 매수 신호가 뜨는 즉시 항상 매수합니다.",
        )
        use_buy_daily_band_confirmation = st.checkbox(
            "매수 확인 - 매일 갱신 2시그마 밴드",
            key="bt_use_buy_daily_band_confirmation",
            disabled=not use_buy_noise_filter,
            help="켜두면(기본값): 관찰 시작(D0) 후 1~7거래일은 상승폭과 무관하게 무조건 보류합니다"
            "(며칠 새 등락은 랜덤워크 노이즈일 확률이 커서 판단하지 않음). 8~21거래일차(3주)까지는 "
            "매일, 그날까지 경과한 거래일수의 제곱근에 비례해 넓어지는 확인 밴드(√t 법칙, 2시그마 "
            "— 매도 필터와 동일한 공식·수치)를 계산해, D0 종가 대비 그날의 밴드만큼(또는 그 이상) "
            "상승한 첫날 즉시 매수합니다. 21거래일 동안 한 번도 밴드에 도달하지 못하면 관찰을 "
            "종료하고 D0의 신호는 없었던 것으로 처리합니다. 끄면: 아래 '매수 확인 상승률'을 사용하는 "
            "기존 방식(D0+7일 고정 시점 확인)으로 동작합니다.",
        )
        buy_noise_filter_rise_pct = st.number_input(
            "매수 확인 상승률 (%, D0 대비 D0+7일 — 위 2시그마 밴드가 꺼져 있을 때만 사용)",
            min_value=0.0, max_value=50.0, step=0.5, key="bt_buy_noise_filter_rise_pct",
            disabled=not use_buy_noise_filter or use_buy_daily_band_confirmation,
            help="위 '매수 확인 - 매일 갱신 2시그마 밴드'가 꺼져 있을 때만 작동하는 고정 방식 "
            "설정입니다. D0+7일 종가가 D0 종가보다 이 %만큼(또는 그 이상) 높아야만 매수를 "
            "실행합니다(예: 5이면 5% 이상 상승해야 매수, 살짝만 오른 경우는 대세 하락장으로 보고 "
            "관찰모드를 해제해 계속 미보유). 0으로 두면 이전처럼 '조금이라도 높으면 매수'와 "
            "동일해집니다.",
        )

    with adv_sell_col:
        st.markdown("**매도 관련**")
        use_new_low_trigger = st.checkbox(
            "52주 신저가 갱신 시 매도",
            key="bt_use_new_low_trigger",
            help="위 '52주 신고가 갱신 시 매수'의 매도판 대칭 조건입니다. 종가가 직전 365일"
            "(역일) 중 최저 종가보다 낮아지는 날(신규 52주 신저가 경신일, 그날 처음 갱신된 "
            "경우만) 즉시 매도합니다(green_count 조건과 무관하게 추가로 작동, 둘 중 아무거나 "
            "먼저 만족해도 매도). 이 트리거에도 자체 쿨다운은 없어 신저가를 경신할 때마다"
            "(단, 이미 미보유 상태면 매도를 다시 하지 않음) 작동하며, 아래 '최소 보유일수'가 "
            "지나기 전에는 이 트리거를 포함해 모든 매도 조건이 평가되지 않습니다.",
        )
        use_sell_noise_filter = st.checkbox(
            "상승추세 중 매도신호 노이즈 필터 (180일선 +5% 이상)",
            key="bt_use_sell_noise_filter",
            help="매도신호가 발생한 날(D0) 종가가 180일 이동평균보다 "
            f"{backtest.DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT:g}% 이상 높을 때만 작동합니다(그 미만이면 "
            "이 필터와 무관하게 항상 즉시 매도). 켜두면: D0의 매도신호는 무시하고 보유를 유지하며, "
            "아래 '매도 확인 - 매일 갱신 2시그마 밴드' 설정에 따라 매일 확대되는 밴드(기본) 또는 "
            f"D0+{backtest.SELL_NOISE_FILTER_WINDOW_DAYS}일(역일 기준) 고정 시점(끄면) 방식으로 매도 "
            "여부를 확인합니다. 관찰 기간 중 추가로 뜨는 매도신호는 매도 여부에 전혀 영향을 주지 "
            "않고 참고 기록으로만 남습니다. 끄면 매도 신호가 뜨는 즉시 항상 매도합니다(이 로직 "
            "도입 이전과 동일).",
        )
        use_daily_band_confirmation = st.checkbox(
            "매도 확인 - 매일 갱신 2시그마 밴드",
            key="bt_use_daily_band_confirmation",
            disabled=not use_sell_noise_filter,
            help="켜두면(기본값): 관찰 시작(D0) 후 1~7거래일은 하락폭과 무관하게 무조건 보류합니다"
            "(며칠 새 등락은 랜덤워크 노이즈일 확률이 커서 판단하지 않음). 8~21거래일차(3주)까지는 "
            "매일, 그날까지 경과한 거래일수의 제곱근에 비례해 넓어지는 확인 밴드(√t 법칙, 2시그마 "
            "— 예: 8일차 -6.05%, 10일차 -6.76%, 14일차 -8.00%, 21일차 -9.80%)를 계산해, D0 종가 "
            "대비 그날의 밴드만큼(또는 그 이상) 하락한 첫날 즉시 매도합니다. 21거래일 동안 한 번도 "
            "밴드에 도달하지 못하면 관찰을 종료하고 D0의 신호는 없었던 것으로 처리합니다. "
            "끄면: 아래 '매도 확인 하락률'을 사용하는 기존 방식(D0+7일 고정 시점 확인)으로 "
            "동작합니다.",
        )
        sell_noise_filter_drop_pct = st.number_input(
            "매도 확인 하락률 (%, D0 대비 D0+7일 — 위 2시그마 밴드가 꺼져 있을 때만 사용)",
            min_value=0.0, max_value=50.0, step=0.5, key="bt_sell_noise_filter_drop_pct",
            disabled=not use_sell_noise_filter or use_daily_band_confirmation,
            help="위 '매도 확인 - 매일 갱신 2시그마 밴드'가 꺼져 있을 때만 작동하는 고정 방식 "
            "설정입니다. D0+7일 종가가 D0 종가보다 이 %만큼(또는 그 이상) 낮아야만 매도를 "
            "실행합니다(예: 5이면 5% 이상 하락해야 매도, 살짝만 빠진 경우는 대세 상승장으로 보고 "
            "관찰모드를 해제해 계속 보유). 0으로 두면 이전처럼 '조금이라도 낮으면 매도'와 "
            "동일해집니다.",
        )
        min_holding_days = st.number_input(
            "매수 후 최소 보유일수 (일, 역일 기준)",
            min_value=0, max_value=1825, step=1, key="bt_min_holding_days",
            help="매수 이후 이 일수가 지나기 전까지는 매도 조건(green_count·52주 신저가 갱신 "
            "모두)을 아예 확인하지 않습니다. 단기 트레이딩이 아닌 전략에 적합합니다. "
            "주말·공휴일과 무관하게 매수일로부터의 달력일 차이(date2 - date1)로 계산됩니다.",
        )
        st.caption(f"≈ {min_holding_days / 30:.1f}개월간 매도 조건을 무시하고 무조건 보유")

    st.markdown("**수수료** (② KRX 금현물 선택 시에만 적용 — ① 국제 금 시세는 실물이 아닌 "
                "참고 가격이라 적용되지 않음)")
    apply_fees = st.checkbox(
        "수수료 반영",
        key="bt_apply_fees",
        disabled=gold_price_basis != config.GOLD_PRICE_BASIS_KRX,
        help="체크를 해제하면 아래 세 수수료 입력값과 무관하게 전부 0으로 두고 계산합니다"
        "(입력값 자체는 그대로 남아있어 다시 체크하면 복원됩니다). 차트에서는 이 체크박스와 "
        "별개로 '수수료 반영/미반영' 곡선을 토글로 바로 비교해볼 수 있습니다(줌 상태 유지).",
    )
    fee_buy_col, fee_sell_col, fee_holding_col = st.columns(3)
    with fee_buy_col:
        buy_fee_pct = st.number_input(
            "매수 수수료 (%, 편도)",
            min_value=0.0, max_value=5.0, step=0.001, format="%.3f", key="bt_buy_fee_pct",
            disabled=gold_price_basis != config.GOLD_PRICE_BASIS_KRX or not apply_fees,
            help="매수 체결 시마다 그날 매수금액에 부과되는 1회성 수수료입니다(기본값 "
            "0.165%, 미래에셋증권 KRX 금현물 매매수수료 기준). 보유기간과 무관하게 매수할 "
            "때마다 매번 발생하므로, 매매 횟수가 많은 신호강도일수록 총액도 커집니다.",
        )
    with fee_sell_col:
        sell_fee_pct = st.number_input(
            "매도 수수료 (%, 편도)",
            min_value=0.0, max_value=5.0, step=0.001, format="%.3f", key="bt_sell_fee_pct",
            disabled=gold_price_basis != config.GOLD_PRICE_BASIS_KRX or not apply_fees,
            help="매도 체결 시마다 그날 매도금액에 부과되는 1회성 수수료입니다(기본값 "
            "0.165%). Buy & Hold는 분석기간 종료 시점에 전량 매도한다고 가정해 이 수수료를 "
            "마지막 날 1회 반영합니다.",
        )
    with fee_holding_col:
        daily_holding_fee_pct = st.number_input(
            "보관수수료 (%, 일률)",
            min_value=0.0, max_value=1.0, step=0.00001, format="%.5f", key="bt_daily_holding_fee_pct",
            disabled=gold_price_basis != config.GOLD_PRICE_BASIS_KRX or not apply_fees,
            help="보유 잔량에 매일 부과되는 수수료입니다(기본값 0.00022%, 부가세 포함 추정치 "
            "— 연율이 아니라 하루 치 요율입니다). 정확한 산정 주기(일할 계산 후 월초 청구 "
            "vs. 월말 잔량 기준 월 1회 부과)가 아직 확인되지 않아, 우선 '매일 이 %만큼 "
            "누적'으로 가정해 계산합니다. 매수/매도 수수료와 달리 실제 보유 기간에만(신호전략은 "
            "보유 중일 때만, Buy & Hold는 전체 기간) 발생합니다.",
        )

# The "기대수익률" value is needed here (as simulate()'s bond_annual_yield)
# before the widget that lets the user edit it gets rendered — that widget now
# lives inside the ④ 신호전략(미보유기간 기대수익률 포함) group, right below its
# CAGR metric, which only happens further down the script. Reading it out of
# session_state directly (rather than calling st.number_input here) doesn't
# instantiate a widget, so it has no effect on layout; it just gets this run's
# already-current value (DEFAULTS' setdefault() above guarantees the key
# exists). The actual st.number_input(key="bt_bond_yield_pct") call below is
# reached on every run regardless of whether simulate() succeeds or fails
# (see the "if result is not None" branching after the try/except) — it must
# be, since a widget Streamlit doesn't instantiate in a given run has its
# session_state entry cleared at the end of that run; if it were only reached
# on the happy path and simulate() ever raised (bad data, an edge-case
# parameter combo, a flaky refetch), the next successful run would find the
# key missing and DEFAULTS.setdefault() would silently reset it back to the
# 10% default (confirmed via a Streamlit AppTest repro: forcing one simulate()
# call to fail reverted a user-set 7.5% straight back to 10% on the very next
# rerun, even though that rerun itself succeeded).
bond_yield_pct = float(st.session_state["bt_bond_yield_pct"])

@st.cache_data(ttl=3600, show_spinner="데이터를 내려받는 중입니다...")
def load_signals(as_of_iso: str, years: int, gold_price_basis: str) -> pd.DataFrame:
    return backtest.prepare_signals(
        as_of=date.fromisoformat(as_of_iso), years=years, gold_price_basis=gold_price_basis
    )


def _format_gold_price(value: float, basis: str = gold_price_basis) -> str:
    if basis == config.GOLD_PRICE_BASIS_KRX:
        return f"{value:,.0f}원"
    return f"${value:,.2f}"


# Meaningless (and not applied) unless the KRX basis is active AND "수수료
# 반영" is checked — the three inputs stay enabled-looking with their
# defaults either way, but only actually reach the simulation when relevant.
_fees_active = apply_fees and gold_price_basis == config.GOLD_PRICE_BASIS_KRX
effective_buy_fee_pct = float(buy_fee_pct) if _fees_active else 0.0
effective_sell_fee_pct = float(sell_fee_pct) if _fees_active else 0.0
effective_daily_holding_fee_pct = float(daily_holding_fee_pct) if _fees_active else 0.0

# Shared by both the fee-adjusted ("net") and always-zero-fee ("gross") runs
# below, AND by _threshold_holding's per-threshold reruns further down —
# trades/holding_curve never depend on the fee rates at all (every buy/sell
# trigger is computed purely from `signals`), only the equity numbers differ,
# so this is the only place these settings need to be typed. Deliberately
# excludes buy_green_count/sell_green_count: the main runs below need this
# page's own selected threshold, while compute_threshold_holding fills in its
# own three (6/0·5/1·4/2) per call — bundling them here would collide with
# both.
_shared_sim_kwargs = dict(
    use_new_high_trigger=use_new_high_trigger,
    use_new_low_trigger=use_new_low_trigger,
    min_holding_days=int(min_holding_days),
    bond_annual_yield=float(bond_yield_pct) / 100.0,
    use_sell_noise_filter=use_sell_noise_filter,
    use_daily_band_confirmation=use_daily_band_confirmation,
    sell_noise_filter_drop_pct=float(sell_noise_filter_drop_pct),
    use_buy_noise_filter=use_buy_noise_filter,
    use_buy_daily_band_confirmation=use_buy_daily_band_confirmation,
    buy_noise_filter_rise_pct=float(buy_noise_filter_rise_pct),
)

try:
    signals = load_signals(as_of_date.isoformat(), int(years), gold_price_basis)
    result = backtest.simulate(
        signals,
        **_shared_sim_kwargs,
        buy_green_count=int(buy_green_count),
        sell_green_count=int(sell_green_count),
        buy_fee_pct=effective_buy_fee_pct,
        sell_fee_pct=effective_sell_fee_pct,
        daily_holding_fee_pct=effective_daily_holding_fee_pct,
    )
    # "총수익률(수수료 미반영)" — 차트의 수수료 반영/미반영 토글이 비교할 상대편.
    # trades/holding_curve는 위 result와 완전히 동일(신호 자체가 수수료와 무관)
    # 하고, equity/누적수익률 계열만 다르다.
    result_gross = backtest.simulate(
        signals,
        **_shared_sim_kwargs,
        buy_green_count=int(buy_green_count),
        sell_green_count=int(sell_green_count),
        buy_fee_pct=0.0,
        sell_fee_pct=0.0,
        daily_holding_fee_pct=0.0,
    )
except Exception as exc:
    st.error(f"백테스트를 실행하지 못했습니다: {exc}")
    result = None
    result_gross = None

# `m` (and everything derived from `result`) stays None on failure rather than
# calling st.stop() immediately — the "1. 요약 지표" section right below still
# needs to render on every run (its ④ group hosts the "기대수익률" widget,
# which must always be instantiated; see the comment above `bond_yield_pct`).
# Only once that section has rendered does the page actually stop on failure
# (see the "if result is None: st.stop()" guard right after it) — everything
# past that point (chart, yearly bar chart, trade table) genuinely can't
# render without `result`/`signals`.
if result is not None:
    m = result["metrics"]
    equity = result["equity_curve"]
    bh_equity = result["bh_equity_curve"]
    holding_curve = result["holding_curve"]
    hybrid_equity = result["hybrid_equity_curve"]
    yearly = result["yearly_returns"]
    trades = result["trades"]

    bh_equity_gross = result_gross["bh_equity_curve"]
    hybrid_equity_gross = result_gross["hybrid_equity_curve"]

    # 누적수익률 차트의 "신호강도(6/0·5/1·4/2)" 드롭다운 + 국면별 참여율 카드용 —
    # 현재 페이지의 다른 모든 설정(52주 트리거·매도·매수 노이즈필터 등)은 그대로 두고
    # green_count 매수/매도 임계값만 regime.THRESHOLD_PAIRS의 세 조합으로 바꿔가며
    # 같은 signals에 다시 돌린다(gold_dashboard/regime.py — 대시보드 핵심 요약
    # 문구도 같은 함수로 참여율을 계산하는 단일 소스). 참여율은 보유 여부만 보므로
    # 수수료 값 자체는 결과에 영향이 없다.
    _threshold_holding = regime.compute_threshold_holding(
        signals,
        **_shared_sim_kwargs,
        buy_fee_pct=effective_buy_fee_pct,
        sell_fee_pct=effective_sell_fee_pct,
        daily_holding_fee_pct=effective_daily_holding_fee_pct,
    )

    start_date = equity.index[0].date()
    end_date = equity.index[-1].date()
    st.caption(
        f"분석 기간: **{start_date} ~ {end_date}** "
        "(신호전략과 Buy & Hold 모두 이 기간의 첫날에 시작 — 동일 시작일 비교)"
    )
else:
    m = None

# ---- 1. 요약 지표 (설정 바로 아래에 배치 — 값을 바꿔가며 바로 확인) ----
# 4개 그룹으로 묶어서 표시: ① 매매 개요 / ② Buy & Hold / ③ 신호전략(보유기간만) /
# ④ 신호전략(미보유기간 기대수익률 포함, 기대수익률 입력도 이 그룹 안에 위치).
st.subheader("요약 지표")
holding_fraction = (
    1.0 - m["non_holding_fraction"]
    if m is not None and m["non_holding_fraction"] is not None
    else None
)

overview_col, bh_col, strategy_held_col, strategy_hybrid_col = st.columns(4)
with overview_col:
    st.markdown("###### ① 매매 개요")
    st.metric("매매횟수", f"{m['closed_trade_count']}회" if m is not None else "-")
    st.metric(
        "보유기간",
        f"{holding_fraction:.1%}" if holding_fraction is not None else "-",
        help="분석 기간 전체(캘린더일 기준) 중 신호전략이 실제로 금을 보유하고 있던 기간의 비중.",
    )
    st.metric("승률", f"{m['win_rate']:.1%}" if m is not None and m["win_rate"] is not None else "-")
with bh_col:
    st.markdown(f"###### ② {BH_LABEL}")
    st.metric("누적수익률", f"{m['bh_total_return']:.1%}" if m is not None else "-")
    st.metric("연환산수익률(CAGR)", f"{m['bh_cagr']:.1%}" if m is not None else "-")
with strategy_held_col:
    st.markdown(f"###### ③ {STRATEGY_LABEL} (보유기간)")
    st.metric(
        "누적수익률",
        f"{m['strategy_total_return']:.1%}" if m is not None else "-",
        help="보유 기간에만 투자했다고 가정한 누적수익률(미보유 기간은 반영하지 않음).",
    )
    st.metric(
        "연환산수익률(CAGR)",
        f"{m['strategy_cagr']:.1%}" if m is not None and m["strategy_cagr"] is not None else "-",
        help="실제로 금을 보유했던 기간의 일수만 분모로 사용한 연환산수익률(현금 보유 기간 제외) "
        "— 분모가 달라 Buy & Hold의 CAGR과 직접 비교할 수 없습니다.",
    )
with strategy_hybrid_col:
    st.markdown(f"###### ④ {STRATEGY_LABEL} (미보유기간 기대수익률 포함)")
    st.metric(
        "누적수익률",
        f"{m['hybrid_total_return']:.1%}" if m is not None and m["hybrid_total_return"] is not None else "-",
        help="보유 기간엔 실제 금 수익률을, 미보유 기간엔 아래 '기대수익률'을 적용해 이어 붙인 "
        "전체 분석기간 기준 누적수익률입니다.",
    )
    st.metric(
        "연환산수익률(CAGR)",
        f"{m['hybrid_cagr']:.1%}" if m is not None and m["hybrid_cagr"] is not None else "-",
        help="위 누적수익률을 분석 기간 전체를 기준으로 연환산한 값입니다.",
    )
    # Instantiated HERE, right below the CAGR it drives — reached on every
    # run regardless of whether simulate() above succeeded (see the comment
    # by this run's earlier plain session_state read of the same key, right
    # before the try/except, for why that matters).
    bond_yield_pct = st.number_input(
        "기대수익률 (연, %)",
        min_value=0.0, max_value=20.0, step=0.1, key="bt_bond_yield_pct",
        help="신호가 없어 금을 보유하지 않는 기간 동안, 그 돈을 이 연이율로 운용했다고 "
        "가정합니다(예: 채권 매입). 값을 바꾸면 이 그룹의 누적수익률·CAGR이 바로 "
        "재계산됩니다.",
    )
    bond_yield_pct = float(bond_yield_pct)

if m is not None:
    if m["has_open_position"]:
        st.info(
            "현재 포지션을 보유 중입니다. 마지막 거래는 미청산 상태이며, 위 수익률·아래 거래 내역에 "
            "표시된 값은 오늘 종가 기준 평가손익입니다."
        )
    if m["strategy_cagr"] is None:
        st.caption("ℹ️ 신호전략이 이 기간 동안 한 번도 매수 신호를 내지 않아 CAGR을 계산할 수 없습니다.")

if result is None:
    st.stop()

# ---- 2. 누적수익률 라인차트 (+ 매수/매도 시점 마커) ----
st.subheader("누적수익률")
STRAT_HYBRID_LABEL = f"{STRATEGY_LABEL}(기대수익률 포함)"

# 신호전략(보유기간만)은 이 차트에서 제외 — Buy & Hold와 신호전략(기대수익률 포함) 둘만 표시.
dates = equity.index

# 국면 음영(요구사항1)/참여율 카드(요구사항3)에 쓸 데이터를 JS로 그대로 넘기기 위한
# 준비 — 전부 파이썬에서 한 번만 계산해 JSON으로 임베드하고, 줌 구간 필터링·비율
# 재계산 자체는 서버 왕복 없이 JS가 그 자리에서 한다.
_regime5_labels = regime.classify_regime_5way(dates)
_held_by_threshold = {
    key: hc.reindex(dates).fillna(False).astype(int).tolist()
    for key, hc in _threshold_holding.items()
}
_regime_payload = {
    "dates": [d.isoformat() for d in dates],
    "regime5": _regime5_labels,
    "held": _held_by_threshold,
    "shapesA": regime.regime_shading_shapes(
        regime.REGIME_SECULAR_UP, regime.REGIME_SECULAR_DOWN, dates.min(), dates.max()
    ),
    "shapesB": regime.regime_shading_shapes(
        regime.REGIME_UPTREND, regime.REGIME_DOWNTREND, dates.min(), dates.max()
    ),
}

bh_returns = bh_equity.reindex(equity.index).to_numpy() - 1.0
hybrid_returns = hybrid_equity.reindex(equity.index).to_numpy() - 1.0
# "총수익률(수수료 미반영)" 대응 곡선 — 차트의 수수료 반영/미반영 체크박스가
# 토글하는 상대편. holding_curve/dates는 result_gross와 result가 완전히
# 동일(수수료는 신호 자체에 영향을 주지 않음)하므로 equity.index로 그대로
# reindex해도 안전하다.
bh_returns_gross = bh_equity_gross.reindex(equity.index).to_numpy() - 1.0
hybrid_returns_gross = hybrid_equity_gross.reindex(equity.index).to_numpy() - 1.0
holding_bool = holding_curve.reindex(equity.index).fillna(False).to_numpy()
n_points = len(hybrid_returns)
# Segment j spans (point j, point j+1) and is a "holding" segment iff
# holding_bool[j] — matching compute_hybrid_cagr's own convention (a step is
# classified by the state going INTO it). A point belongs to the holding
# sub-line if either segment touching it is a holding segment (so the two
# sub-lines share their shared boundary point and visually connect there).
point_in_holding = np.zeros(n_points, dtype=bool)
point_in_nonholding = np.zeros(n_points, dtype=bool)
if n_points > 1:
    seg_holding = holding_bool[:-1]
    point_in_holding[:-1] |= seg_holding
    point_in_holding[1:] |= seg_holding
    point_in_nonholding[:-1] |= ~seg_holding
    point_in_nonholding[1:] |= ~seg_holding
else:
    point_in_holding[:] = holding_bool
    point_in_nonholding[:] = ~holding_bool

nonholding_note = f"기대수익률 연 {bond_yield_pct:g}% 가정 적용 구간"

fig = go.Figure()

# 미보유 구간 배경 음영(회색) — 연속 미보유 구간을 하나의 띠로 묶어서 표시.
# fig.add_vrect로 Python에서 직접 그리지 않고 날짜 구간만 계산해 _regime_payload에
# 실어 보낸다 — 이 음영도 (국면 음영처럼) Y축이 줌에 따라 자동 재조정될 때마다
# "Y=0~현재 Y축 최댓값"으로 다시 계산돼야 해서, 정적으로 한 번 그려두는 대신 JS가
# 매번 그 자리에서 계산한다(render_backtest_chart 참고).
_nonholding_bands = []
if n_points > 1:
    seg_df = pd.DataFrame({"start": dates[:-1], "end": dates[1:], "holding": holding_bool[:-1]})
    seg_df["run_id"] = (seg_df["holding"] != seg_df["holding"].shift()).cumsum()
    runs = seg_df.groupby("run_id").agg(
        start=("start", "first"), end=("end", "last"), holding=("holding", "first")
    )
    bands_df = runs.loc[~runs["holding"], ["start", "end"]]
    _nonholding_bands = [
        {"x0": row["start"].isoformat(), "x1": row["end"].isoformat()}
        for _, row in bands_df.iterrows()
    ]
_regime_payload["nonHoldingBands"] = _nonholding_bands

fig.add_trace(
    go.Scatter(
        x=dates,
        y=bh_returns,
        mode="lines",
        name=BH_LABEL,
        meta="line_bh_net",
        line=dict(color=BH_COLOR, width=2),
        hovertemplate="%{x|%Y-%m-%d}<br>" + BH_LABEL + ": %{y:.1%}<extra></extra>",
    )
)
# "총수익률(수수료 미반영)" 버전 — 기본은 숨김(visible=False, 플롯·범례 모두에서
# 제외). 차트의 "수수료 반영" 체크박스가 이 세트와 위 "_net" 세트를 서로
# 배타적으로 토글한다(render_backtest_chart의 updateLineVisibility 참고).
fig.add_trace(
    go.Scatter(
        x=dates,
        y=bh_returns_gross,
        mode="lines",
        name=BH_LABEL,
        meta="line_bh_gross",
        visible=False,
        line=dict(color=BH_COLOR, width=2),
        hovertemplate="%{x|%Y-%m-%d}<br>" + BH_LABEL + "(수수료 미반영): %{y:.1%}<extra></extra>",
    )
)

# ④(기대수익률 포함) curve — 보유 구간(실선). 미보유 구간과 같은 범례 항목을
# 공유하도록 name을 동일하게 두고, 아래 미보유 구간 트레이스는 showlegend=False로
# 숨겨 범례에 3번째 항목이 생기지 않게 함.
fig.add_trace(
    go.Scatter(
        x=dates,
        y=np.where(point_in_holding, hybrid_returns, np.nan),
        mode="lines",
        name=STRAT_HYBRID_LABEL,
        meta="line_strategy_net",
        connectgaps=False,
        line=dict(color=STRATEGY_HYBRID_COLOR, width=2),
        hovertemplate="%{x|%Y-%m-%d}<br>" + STRAT_HYBRID_LABEL + ": %{y:.1%}<extra></extra>",
    )
)

# 미보유 구간: 옅은 톤 + 점선, 범례에는 표시하지 않음(같은 STRAT_HYBRID_LABEL
# 시리즈의 연장선일 뿐).
fig.add_trace(
    go.Scatter(
        x=dates,
        y=np.where(point_in_nonholding, hybrid_returns, np.nan),
        mode="lines",
        name=STRAT_HYBRID_LABEL,
        meta="line_strategy_net",
        showlegend=False,
        connectgaps=False,
        line=dict(color=STRATEGY_HYBRID_NONHOLDING_COLOR, width=2, dash="dash"),
        customdata=np.full(n_points, nonholding_note),
        hovertemplate=(
            "%{x|%Y-%m-%d}<br>누적수익률(기대수익률 적용): %{y:.1%}<br>%{customdata}<extra></extra>"
        ),
    )
)

# 신호전략 총수익률(수수료 미반영) — 위 net 라인 두 개(보유/미보유)와 동일한
# 구조로 기본 숨김. 보유·미보유를 나누는 이유는 net과 동일(색·점선 스타일로
# 구분).
fig.add_trace(
    go.Scatter(
        x=dates,
        y=np.where(point_in_holding, hybrid_returns_gross, np.nan),
        mode="lines",
        name=STRAT_HYBRID_LABEL,
        meta="line_strategy_gross",
        visible=False,
        connectgaps=False,
        line=dict(color=STRATEGY_HYBRID_COLOR, width=2),
        hovertemplate="%{x|%Y-%m-%d}<br>" + STRAT_HYBRID_LABEL + "(수수료 미반영): %{y:.1%}<extra></extra>",
    )
)
fig.add_trace(
    go.Scatter(
        x=dates,
        y=np.where(point_in_nonholding, hybrid_returns_gross, np.nan),
        mode="lines",
        name=STRAT_HYBRID_LABEL,
        meta="line_strategy_gross",
        visible=False,
        showlegend=False,
        connectgaps=False,
        line=dict(color=STRATEGY_HYBRID_NONHOLDING_COLOR, width=2, dash="dash"),
        customdata=np.full(n_points, nonholding_note),
        hovertemplate=(
            "%{x|%Y-%m-%d}<br>누적수익률(수수료 미반영, 기대수익률 적용): %{y:.1%}<br>"
            "%{customdata}<extra></extra>"
        ),
    )
)

marker_rows = []
marker_rows_gross = []
for t in trades:
    marker_rows.append(
        {
            "date": t["entry_date"],
            "구분": "매수",
            "return": float(hybrid_equity.loc[t["entry_date"]]) - 1.0,
            "가격": round(t["entry_price"], 2),
            "사유": t["entry_reason"] or "-",
        }
    )
    marker_rows_gross.append(
        {
            "date": t["entry_date"],
            "구분": "매수",
            "return": float(hybrid_equity_gross.loc[t["entry_date"]]) - 1.0,
            "가격": round(t["entry_price"], 2),
            "사유": t["entry_reason"] or "-",
        }
    )
    if not t["open"]:
        marker_rows.append(
            {
                "date": t["exit_date"],
                "구분": "매도",
                "return": float(hybrid_equity.loc[t["exit_date"]]) - 1.0,
                "가격": round(t["exit_price"], 2),
                "사유": t["exit_reason"] or "-",
            }
        )
        marker_rows_gross.append(
            {
                "date": t["exit_date"],
                "구분": "매도",
                "return": float(hybrid_equity_gross.loc[t["exit_date"]]) - 1.0,
                "가격": round(t["exit_price"], 2),
                "사유": t["exit_reason"] or "-",
            }
        )
marker_df = pd.DataFrame(marker_rows)
marker_df_gross = pd.DataFrame(marker_rows_gross)

# Basis-aware price display: KRX (KRW/g) shows no decimals and no "$", intl
# (USD/oz) keeps the original "$" formatting.
_price_tooltip_format = "$,.2f" if gold_price_basis == config.GOLD_PRICE_BASIS_INTL else ",.0f"
_price_tooltip_title = "체결가" if gold_price_basis == config.GOLD_PRICE_BASIS_INTL else "체결가 (원)"

if not marker_df.empty:
    for label, color, symbol in (
        ("매수", BUY_COLOR, "triangle-up"),
        ("매도", SELL_COLOR, "triangle-down"),
    ):
        sub = marker_df[marker_df["구분"] == label]
        if sub.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=sub["date"],
                y=sub["return"],
                mode="markers",
                name=label,
                meta="marker_strategy_net",
                # 흰색 테두리(halo) — 국면 음영이 파랑/빨강으로 진해지면서 같은
                # 색 계열 마커(매수=파랑/매도=빨강)가 배경에 묻히지 않도록, 배경이
                # 무엇이든 마커 윤곽이 항상 도드라지게 한다.
                marker=dict(symbol=symbol, color=color, size=11, line=dict(color="white", width=2)),
                customdata=np.stack([sub["가격"].to_numpy(), sub["사유"].to_numpy()], axis=-1),
                hovertemplate=(
                    "%{x|%Y-%m-%d}<br>구분: "
                    + label
                    + "<br>"
                    + _price_tooltip_title
                    + ": %{customdata[0]:"
                    + _price_tooltip_format
                    + "}<br>당시 누적수익률: %{y:.1%}<br>사유: %{customdata[1]}<extra></extra>"
                ),
            )
        )
        # 총수익률(수수료 미반영) 버전 — 같은 매수·매도 시점, Y값(당시 누적수익률)만
        # hybrid_equity_gross 기준으로 다름. 기본 숨김.
        sub_gross = marker_df_gross[marker_df_gross["구분"] == label]
        fig.add_trace(
            go.Scatter(
                x=sub_gross["date"],
                y=sub_gross["return"],
                mode="markers",
                name=label,
                meta="marker_strategy_gross",
                visible=False,
                marker=dict(symbol=symbol, color=color, size=11, line=dict(color="white", width=2)),
                customdata=np.stack([sub_gross["가격"].to_numpy(), sub_gross["사유"].to_numpy()], axis=-1),
                hovertemplate=(
                    "%{x|%Y-%m-%d}<br>구분: "
                    + label
                    + "<br>"
                    + _price_tooltip_title
                    + ": %{customdata[0]:"
                    + _price_tooltip_format
                    + "}<br>당시 누적수익률(수수료 미반영): %{y:.1%}<br>사유: %{customdata[1]}<extra></extra>"
                ),
            )
        )

_first_jan1 = pd.Timestamp(year=dates.min().year, month=1, day=1)
fig.update_xaxes(
    tickformat="%Y",
    # Fixed to exactly one tick per calendar year (never left to Plotly's
    # automatic tick spacing) — dtick="M12" anchored at tick0 (a real Jan 1,
    # not snapped to whatever date happens to have a data point) guarantees
    # ticks land on Jan 1 of each year and are never denser than one per
    # year, at any zoom level reachable via the buttons/box-zoom alone (the
    # rangeslider/rangeselector-driven relayout listener below additionally
    # switches this to month-level ticks when a zoom narrows past ~1 year,
    # since a fixed yearly dtick would otherwise show zero ticks there).
    dtick="M12",
    tick0=_first_jan1.isoformat(),
    rangeslider=dict(visible=True),
    rangeselector=dict(
        buttons=list(
            [
                dict(count=1, label="1년", step="year", stepmode="backward"),
                dict(count=3, label="3년", step="year", stepmode="backward"),
                dict(count=5, label="5년", step="year", stepmode="backward"),
                dict(step="all", label="전체"),
            ]
        )
    ),
)
fig.update_yaxes(title="누적수익률", tickformat=".0%")
fig.update_layout(
    height=460,
    margin=dict(t=60, b=10),
    hovermode="closest",
    legend=dict(orientation="h", yanchor="bottom", y=1.2, xanchor="left", x=0),
)

render_backtest_chart(fig, _regime_payload, height=460)
st.caption(
    "▲ 파란색 = 매수 시점, ▼ 빨간색 = 매도 시점 (거래 내역 표 참고) · "
    f"{STRAT_HYBRID_LABEL}의 점선·회색 음영 구간 = 미보유(현금) 기간에 기대수익률을 "
    "가정 적용한 부분 · 하단 슬라이더로 구간을 드래그해 확대, 상단 버튼으로 빠른 기간 이동 가능 · "
    "상단 체크박스로 국면 음영(장기/일반)과 라인 표시를, 하단 드롭다운으로 신호강도별 "
    "국면 참여율을 확인할 수 있습니다"
)

# ---- 3. 연도별 연환산수익률 막대그래프 ----
st.subheader("연도별 연환산수익률")
STRAT_HELD_LABEL = f"{STRATEGY_LABEL}(보유기간만)"
yearly_mode = st.radio(
    "신호전략 계산 방식",
    options=[STRAT_HYBRID_LABEL, STRAT_HELD_LABEL],
    horizontal=True,
    key="bt_yearly_chart_mode",
    help=f"**{STRAT_HYBRID_LABEL}**(기본): 미보유(현금) 기간에도 위에서 설정한 기대수익률"
    f"(연 {bond_yield_pct:g}%)을 적용해 연환산수익률을 계산합니다 — 요약 지표 ④ 그룹과 "
    f"동일한 방식입니다. **{STRAT_HELD_LABEL}**: 미보유 기간은 0%로 취급하고 실제 보유 "
    "기간의 등락만 반영합니다 — 요약 지표 ③ 그룹과 동일한 방식입니다.",
)
# `yearly` (from result["yearly_returns"]) is already computed from the hybrid
# curve to match ④ — only recompute from the plain equity curve when the user
# picks ③, using the same yearly_returns() function directly.
yearly_display = yearly if yearly_mode == STRAT_HYBRID_LABEL else backtest.yearly_returns(equity, bh_equity)
strategy_series_label = yearly_mode
strategy_color = STRATEGY_HYBRID_COLOR if yearly_mode == STRAT_HYBRID_LABEL else STRATEGY_COLOR

yearly_long = yearly_display.melt(
    id_vars=["year", "days_span"],
    value_vars=["strategy_return_annualized", "bh_return_annualized"],
    var_name="series",
    value_name="return",
)
yearly_long["series"] = yearly_long["series"].map(
    {"strategy_return_annualized": strategy_series_label, "bh_return_annualized": BH_LABEL}
)
# raw (non-annualized) realized return for the tooltip, aligned to the same rows
raw_map = {}
for _, row in yearly_display.iterrows():
    raw_map[(row["year"], strategy_series_label)] = row["strategy_return"]
    raw_map[(row["year"], BH_LABEL)] = row["bh_return"]
yearly_long["raw_return"] = [raw_map[(y, s)] for y, s in zip(yearly_long["year"], yearly_long["series"])]

bar_fig = go.Figure()
for label, color in ((strategy_series_label, strategy_color), (BH_LABEL, BH_COLOR)):
    sub = yearly_long[yearly_long["series"] == label]
    bar_fig.add_trace(
        go.Bar(
            x=sub["year"].astype(str),
            y=sub["return"],
            name=label,
            marker_color=color,
            customdata=np.stack([sub["raw_return"].to_numpy(), sub["days_span"].to_numpy()], axis=-1),
            hovertemplate=(
                "연도: %{x}<br>전략: "
                + label
                + "<br>연환산수익률: %{y:.1%}<br>해당 연도 실제 수익률: %{customdata[0]:.1%}"
                + "<br>해당 연도 일수: %{customdata[1]:d}<extra></extra>"
            ),
        )
    )
bar_fig.update_layout(
    height=340,
    barmode="group",
    yaxis=dict(title="연환산수익률", tickformat=".0%"),
    xaxis=dict(title=None),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    margin=dict(t=40, b=10),
)
st.plotly_chart(bar_fig, use_container_width=True)
yearly_cash_note = (
    f"미보유(현금) 기간에는 기대수익률(연 {bond_yield_pct:g}%)이 적용됩니다."
    if yearly_mode == STRAT_HYBRID_LABEL
    else "신호전략이 그 해 내내 현금(미보유) 상태였다면 0%로 표시됩니다."
)
st.caption(
    f"{int(yearly_display['year'].iloc[0])}년과 {int(yearly_display['year'].iloc[-1])}년은 분석 기간에 걸친 "
    "부분연도이며, 그 부분 기간의 실제 수익률을 연 단위로 환산한 값입니다(마우스오버 시 실제 "
    f"수익률 확인 가능). {yearly_cash_note}"
)

# ---- 4. 거래 내역 표 ----
st.subheader("거래 내역")
if trades:
    trade_rows = [
        {
            "매수일": t["entry_date"].date(),
            "매수가": _format_gold_price(t["entry_price"]),
            "매수 사유": t["entry_reason"] or "-",
            "매도일": t["exit_date"].date() if t["exit_date"] is not None else "미청산(보유 중)",
            "매도가": _format_gold_price(t["exit_price"]),
            "매도 사유": t["exit_reason"] or ("미청산" if t["open"] else "-"),
            "보유일수": t["hold_days"],
            "구간수익률(순, 수수료 반영)": f"{t['net_period_return']:.2%}" + (" (평가)" if t["open"] else ""),
            "구간수익률(총, 수수료 미반영)": f"{t['gross_period_return']:.2%}" + (" (평가)" if t["open"] else ""),
        }
        for t in trades
    ]
    st.dataframe(pd.DataFrame(trade_rows), use_container_width=True, hide_index=True)
    st.caption(
        "매수 사유/매도 사유는 신호가 발생한 날 기준이며, 모든 조건이 체결일 = 신호 발생일"
        "(지연 없음)입니다. 구간수익률(순)은 이 거래의 매수/매도/보관 수수료를 모두 반영한 "
        "실제 손익 기준이며, 위 승률도 이 기준으로 계산됩니다 — 구간수익률(총)은 수수료를 "
        "제외한 순수 가격 변동률입니다(미청산 거래는 매도수수료를 아직 반영하지 않은 값)."
    )
else:
    st.caption("이 기간 동안 매수 신호가 발생하지 않아 거래 내역이 없습니다.")

st.caption(
    "⚠️ 본 백테스트는 과거 데이터에 기반한 시뮬레이션 결과이며 미래 성과를 보장하지 않습니다. "
    "② KRX 금현물 기준일 때는 매수·매도 거래수수료와 보관수수료가 반영되지만, 세금·슬리피지는 "
    "여전히 반영되어 있지 않고, 표본 기간이 짧아 과최적화(overfitting) 위험이 있습니다. "
    "'④ 신호전략 (미보유기간 기대수익률 포함)' 그룹의 기대수익률은 사용자가 입력한 "
    "단일 연이율을 그대로 연복리 적용한 단순 가정치이며, 실제 채권 등 투자자산의 이자율 변동· "
    "재투자·신용위험은 반영되어 있지 않습니다."
)

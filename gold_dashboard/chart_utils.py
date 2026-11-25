"""Shared Plotly chart rendering helper — a trimmed-down, backtest-page-free
version of pages/1_백테스트.py's render_backtest_chart: just the generic
"zoomable time series" behavior (Y-axis autoscale on zoom + X-axis year/month
adaptive tick labels), with none of that page's own regime-shading/strategy-
toggle/signal-threshold-card logic, which belongs only to the backtest's own
domain model. Kept as a separate function (not a refactor of
render_backtest_chart) specifically so this never risks regressing that
already-working, heavily-tested chart — any other page wanting the same
"zoomable chart" look (pages/2_자산비교.py today) calls this instead."""

import json
import uuid

import plotly.graph_objects as go
import streamlit as st


def render_zoomable_chart(fig: go.Figure, height: int = 460) -> None:
    """Render `fig` (a go.Figure the caller has already populated with traces
    and called update_xaxes(dtick="M12", tick0=..., rangeslider=...,
    rangeselector=...) on) with the same zoom-driven interactivity as the
    backtest page's main chart:

    1. Y-axis autoscale on zoom: recomputes min/max over the now-visible
       points of every currently-visible trace on any x-range change
       (rangeslider drag, rangeselector button, box-zoom) and applies that as
       the new y-range (with an 8% padding), restoring full y-autorange when
       the x-range resets to "전체". If some traces are assigned to a second
       y-axis (trace.yaxis="y2", with fig.update_layout(yaxis2=dict(
       overlaying="y", side="right", ...)) — e.g. pages/2_자산비교.py putting
       a differently-scaled comparison asset on its own right-hand axis so it
       doesn't visually flatten against 반포자이's axis), each distinct axis
       found among the traces is autoscaled independently, using only the
       traces assigned to it.
    2. X-axis year/month tick switching: >6년 보이는 구간 = 연도만, 4~6년 =
       1월/7월, 2~4년 = 1/4/7/10월, ≤2년 = 매달 — 단일 dtick으로는 "1월은
       연도, 나머지는 월"처럼 두 라벨 형식을 섞을 수 없어 tickvals/ticktext를
       직접 계산해 넘긴다(pages/1_백테스트.py의 같은 로직과 동일).

    st.plotly_chart renders inside an iframe Streamlit controls with no hook
    for attaching custom JS, so this bypasses it via st.iframe (which allows
    script execution), same as the backtest page.
    """
    div_id = f"pyauto_{uuid.uuid4().hex}"
    plot_html = fig.to_html(
        include_plotlyjs="cdn",
        full_html=False,
        div_id=div_id,
        config={"responsive": True},
    )

    script = f"""
<script>
(function() {{
    var gd = document.getElementById("{div_id}");
    if (!gd) return;
    var busy = false;

    // Plotly.py's default JSON encoder writes numeric arrays in a compact
    // {{dtype, bdata}} (base64) wire format that plotly.js does NOT decode
    // back onto gd.data[i].{{x,y}} — decode it ourselves rather than depend
    // on any Plotly-internal copy (see render_backtest_chart's own copy of
    // this same function for the full rationale).
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

    // Plotly trace attribute "yaxis" is "y" (default/primary) or "y2","y3",...
    // (secondary+) — this maps that to the matching layout key ("yaxis",
    // "yaxis2", ...) used in both gd.layout and a Plotly.relayout() update.
    function axisKeyFor(tr) {{
        var ax = tr.yaxis || "y";
        return ax === "y" ? "yaxis" : "yaxis" + ax.slice(1);
    }}
    var axisKeys = Array.from(new Set((gd.data || []).map(axisKeyFor)));

    function visibleYRange(x0, x1, axisKey) {{
        var t0 = new Date(x0).getTime();
        var t1 = new Date(x1).getTime();
        var ymin = Infinity, ymax = -Infinity;
        (gd.data || []).forEach(function(tr) {{
            if (axisKeyFor(tr) !== axisKey) return;
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

    var suppressRelayout = false;
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
            axisKeys.forEach(function(k) {{ updates[k + ".autorange"] = true; }});
            busy = true;
            Plotly.relayout(gd, updates).then(function() {{ busy = false; }});
            return;
        }}
        var updates = xTickUpdates(x0, x1);
        axisKeys.forEach(function(axisKey) {{
            var yr = visibleYRange(x0, x1, axisKey);
            if (yr) {{
                updates[axisKey + ".range"] = yr;
                updates[axisKey + ".autorange"] = false;
            }}
        }});
        busy = true;
        Plotly.relayout(gd, updates).then(function() {{ busy = false; }});
    }}
    gd.on("plotly_relayout", onRelayout);

    // 페이지 로드 시점에도 전체 기간 기준으로 연/월 틱을 한 번 맞춰준다.
    var initFull = fullXExtent();
    suppressRelayout = true;
    Plotly.relayout(gd, xTickUpdates(initFull[0], initFull[1])).then(function() {{
        suppressRelayout = false;
    }});
}})();
</script>
"""
    st.iframe(plot_html + script, height=height + 60)

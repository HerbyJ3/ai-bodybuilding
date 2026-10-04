"""Server-rendered SVG weight chart (dataviz method: 2 validated categorical slots,
2px line, >=8px dots with a 2px surface ring, hairline grid, legend for 2 series,
crosshair tooltip via static/chart.js, table view in the template)."""
from __future__ import annotations

import math
from datetime import date, timedelta
from html import escape
from typing import Any

from engine.util import to_lb
from schemas.events import Event

W, H = 760, 280
PAD_L, PAD_R, PAD_T, PAD_B = 48, 64, 16, 32


def weight_series(events: list[Event], as_of: date, excluded: set[str], min_n: int,
                  days: int = 120) -> list[dict[str, Any]]:
    """Daily readings (last reading per day) + trailing 7-day average (needs min_n readings)."""
    start = as_of - timedelta(days=days)
    daily: dict[date, float] = {}
    for e in events:
        if e.type == "weigh_in" and e.event_id not in excluded and start <= e.day <= as_of:
            daily[e.day] = round(to_lb(e.payload.weight, e.payload.unit), 1)
    out = []
    for d in sorted(daily):
        window = [w for dd, w in daily.items() if d - timedelta(days=6) <= dd <= d]
        avg = round(sum(window) / len(window), 1) if len(window) >= min_n else None
        out.append({"date": d.isoformat(), "weight": daily[d], "avg": avg})
    return out


def _nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    span = max(hi - lo, 1.0)
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.floor(lo / step) * step
    ticks, t = [], first
    while t <= hi + 1e-9:
        if t >= lo - 1e-9:
            ticks.append(round(t, 2))
        t += step
    return ticks


def weight_svg(series: list[dict[str, Any]], markers: list[dict[str, str]]) -> str:
    """markers: [{"date": iso, "label": text}] drawn as hairline annotations."""
    if len(series) < 2:
        return ""
    d0 = date.fromisoformat(series[0]["date"])
    d1 = date.fromisoformat(series[-1]["date"])
    span_days = max((d1 - d0).days, 1)
    vals = [p["weight"] for p in series]
    lo, hi = min(vals), max(vals)
    pad = max((hi - lo) * 0.15, 0.5)
    lo, hi = lo - pad, hi + pad
    pw, ph = W - PAD_L - PAD_R, H - PAD_T - PAD_B

    def x(d: str) -> float:
        return PAD_L + (date.fromisoformat(d) - d0).days / span_days * pw

    def y(v: float) -> float:
        return PAD_T + (hi - v) / (hi - lo) * ph

    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
             f'aria-label="Bodyweight: daily readings and 7-day average">']
    for t in _nice_ticks(lo, hi):
        parts.append(f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                     f'<text class="tick" x="{PAD_L - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{t:g}</text>')
    # month-ish date ticks
    step = max(7, span_days // 5)
    d = d0
    while d <= d1:
        xx = x(d.isoformat())
        parts.append(f'<text class="tick" x="{xx:.1f}" y="{H - 10}" text-anchor="middle">'
                     f'{d.strftime("%b %-d")}</text>')
        d += timedelta(days=step)
    parts.append(f'<line class="axis" x1="{PAD_L}" x2="{W - PAD_R}" y1="{PAD_T + ph}" y2="{PAD_T + ph}"/>')
    # one hairline per change date; labels only where they don't collide (details live in the
    # tooltip-free macro history table and the review list)
    last_label_x = -1e9
    for when in sorted({m["date"] for m in markers if d0.isoformat() <= m["date"] <= d1.isoformat()}):
        xx = x(when)
        parts.append(f'<line class="marker" x1="{xx:.1f}" x2="{xx:.1f}" y1="{PAD_T}" y2="{PAD_T + ph}">'
                     f'<title>{escape("; ".join(m["label"] for m in markers if m["date"] == when))}</title></line>')
        if xx - last_label_x > 70 and xx + 70 < W - PAD_R + 40:
            parts.append(f'<text class="marker-label" x="{xx + 4:.1f}" y="{PAD_T + 10}">macros</text>')
            last_label_x = xx
    # 7-day average line (series 1), broken where there isn't enough data
    segs, cur = [], []
    for p in series:
        if p["avg"] is None:
            if len(cur) > 1:
                segs.append(cur)
            cur = []
        else:
            cur.append(f'{x(p["date"]):.1f},{y(p["avg"]):.1f}')
    if len(cur) > 1:
        segs.append(cur)
    for sgm in segs:
        parts.append(f'<polyline class="s1" points="{" ".join(sgm)}"/>')
    # daily readings (series 2): dots with a surface ring
    for p in series:
        parts.append(f'<circle class="s2" cx="{x(p["date"]):.1f}" cy="{y(p["weight"]):.1f}" r="4"/>')
    last_avg = next((p for p in reversed(series) if p["avg"] is not None), None)
    if last_avg:
        parts.append(f'<text class="end-label" x="{x(last_avg["date"]) + 8:.1f}" '
                     f'y="{y(last_avg["avg"]) + 4:.1f}">{last_avg["avg"]:g}</text>')
    parts.append(f'<line class="crosshair" x1="0" x2="0" y1="{PAD_T}" y2="{PAD_T + ph}" visibility="hidden"/>')
    parts.append(f'<rect class="hit" x="{PAD_L}" y="{PAD_T}" width="{pw}" height="{ph}" '
                 f'data-d0="{d0.isoformat()}" data-span="{span_days}" data-pl="{PAD_L}" data-pw="{pw}" '
                 f'data-w="{W}"/>')
    parts.append("</svg>")
    return "".join(parts)

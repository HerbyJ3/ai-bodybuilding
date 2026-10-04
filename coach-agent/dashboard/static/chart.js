// Crosshair + tooltip for the weight chart. Snaps to the nearest reading.
// Values come from a JSON script tag; inserted with textContent only.
(function () {
  const wrap = document.querySelector(".chart-wrap");
  if (!wrap) return;
  const svg = wrap.querySelector("svg.chart");
  const hit = svg && svg.querySelector("rect.hit");
  const data = JSON.parse(document.getElementById("weight-data").textContent || "[]");
  if (!hit || !data.length) return;
  const cross = svg.querySelector(".crosshair");
  const tip = wrap.querySelector(".tooltip");
  const d0 = new Date(hit.dataset.d0 + "T00:00:00");
  const span = +hit.dataset.span, pl = +hit.dataset.pl, pw = +hit.dataset.pw, vw = +hit.dataset.w;
  const xs = data.map(p => pl + (new Date(p.date + "T00:00:00") - d0) / 864e5 / span * pw);

  function show(i) {
    const p = data[i];
    cross.setAttribute("x1", xs[i]); cross.setAttribute("x2", xs[i]);
    cross.setAttribute("visibility", "visible");
    tip.replaceChildren();
    const head = document.createElement("div"); head.className = "tip-date"; head.textContent = p.date;
    tip.appendChild(head);
    [["s1", p.avg === null ? "–" : p.avg + " lb", "7-day average"], ["s2", p.weight + " lb", "Daily reading"]]
      .forEach(([cls, val, name]) => {
        const row = document.createElement("div"); row.className = "tip-row";
        const key = document.createElement("span"); key.className = "key " + cls;
        const v = document.createElement("strong"); v.textContent = val;
        const n = document.createElement("span"); n.className = "muted"; n.textContent = name;
        row.append(key, v, n); tip.appendChild(row);
      });
    const rect = svg.getBoundingClientRect();
    const px = xs[i] / vw * rect.width;
    tip.style.left = Math.min(px + 12, rect.width - 170) + "px";
    tip.hidden = false;
  }
  function nearest(clientX) {
    const rect = svg.getBoundingClientRect();
    const vx = (clientX - rect.left) / rect.width * vw;
    let best = 0;
    xs.forEach((x, i) => { if (Math.abs(x - vx) < Math.abs(xs[best] - vx)) best = i; });
    return best;
  }
  let idx = data.length - 1;
  hit.addEventListener("pointermove", e => { idx = nearest(e.clientX); show(idx); });
  hit.addEventListener("pointerleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
  svg.setAttribute("tabindex", "0");
  svg.addEventListener("focus", () => show(idx));
  svg.addEventListener("blur", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
  svg.addEventListener("keydown", e => {
    if (e.key === "ArrowLeft") { idx = Math.max(0, idx - 1); show(idx); e.preventDefault(); }
    if (e.key === "ArrowRight") { idx = Math.min(data.length - 1, idx + 1); show(idx); e.preventDefault(); }
  });
})();

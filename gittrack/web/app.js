"use strict";

/* ------------------------------------------------------------------ state */
const state = {
  window: 7, band: "", lang: "", q: "", onlyReady: true,
  sort: "heat", dir: -1, selected: null, data: null, view: "trending",
};

const COLUMNS = [
  { key: "rank",         label: "#",     sortable: false, cls: "col-rank" },
  { key: "full_name",    label: "repo",  cls: "col-repo", align: "left" },
  { key: "stars",        label: "stars" },
  { key: "delta_stars",  label: "Δ", dynamic: w => "Δ" + w + "d" },
  { key: "rel_velocity", label: "rel" },
  { key: "velocity",     label: "/day" },
  { key: "z",            label: "z",     title: "Robust z-score vs this repo's own 90-day baseline" },
  { key: "accel_ratio",  label: "accel", title: "This window's rate ÷ the previous window's" },
  { key: "fork_confirm", label: "fork✓", title: "Fork growth ÷ star growth. Near 1 = real adoption; near 0 = a star-only spike" },
  { key: "doubling_days",label: "2× in" },
  { key: "heat",         label: "heat" },
  { key: "spark",        label: "30d",   sortable: false },
];

/* ------------------------------------------------------------- formatting */
const nf = new Intl.NumberFormat("en-US");
function compact(n, signed) {
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  const s = signed && n > 0 ? "+" : n < 0 ? "−" : "";
  const a = Math.abs(n);
  if (a >= 1e6) return s + (a / 1e6).toFixed(1) + "M";
  if (a >= 1e4) return s + Math.round(a / 1e3) + "k";
  if (a >= 1e3) return s + (a / 1e3).toFixed(1) + "k";
  if (a >= 10)  return s + Math.round(a);
  return s + (Math.round(a * 10) / 10);
}
function pct(x) {
  if (x === null || x === undefined) return "-";
  const v = x * 100;
  return (v > 0 ? "+" : "") + (Math.abs(v) < 10 ? v.toFixed(1) : Math.round(v)) + "%";
}
function fdate(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toISOString().slice(0, 10);
}
function fdatetime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toISOString().slice(0, 16).replace("T", " ") + "Z";
}
function ago(ts) {
  if (!ts) return "-";
  const d = (Date.now() / 1000 - ts) / 86400;
  if (d < 1 / 24) return "just now";
  if (d < 1) return Math.round(d * 24) + "h ago";
  return Math.round(d) + "d ago";
}
/* The repo name links to GitHub. stopPropagation keeps the row's own click
   (which opens the detail panel) from firing as well, so the two are distinct:
   name -> GitHub, anywhere else on the row -> details. */
function repoLink(fullName, url) {
  const a = document.createElement("a");
  a.className = "repo-name repo-link";
  a.textContent = fullName;                 // untrusted -> textContent
  a.href = url || ("https://github.com/" + fullName.split("/").map(encodeURIComponent).join("/"));
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.title = "Open " + fullName + " on GitHub";
  a.onclick = e => e.stopPropagation();
  return a;
}

/* Value first, then its label: at a glance the reader wants the number, and the
   label only to confirm what it is. */
function metric(label, value, note, full) {
  const t = el("div", "tile");
  t.appendChild(el("span", "v", value));
  t.appendChild(el("span", "k", label));
  if (note) t.appendChild(el("span", "n", note));
  // The long form lives in the tooltip, so shortening the label loses nothing.
  t.title = [full || label, note].filter(Boolean).join(" - ");
  return t;
}

/* Every repo-supplied string goes through here - never innerHTML. */
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function svgEl(tag, attrs) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* ----------------------------------------------------------------- charts */

function niceTicks(lo, hi, count) {
  if (lo === hi) { lo = Math.min(0, lo); hi = hi || 1; }
  const raw = (hi - lo) / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

function makeTip(host) {
  const tip = el("div", "tip");
  host.appendChild(tip);
  return {
    node: tip,
    hide() { tip.style.opacity = 0; },
    show(x, y, date, rows) {
      tip.replaceChildren();
      tip.appendChild(el("div", "t-date", date));
      for (const r of rows) {
        const line = el("div", "t-row");
        const key = el("span", "t-key"); key.style.background = r.color;
        line.appendChild(key);
        line.appendChild(el("span", "t-val", r.value));   // value leads
        line.appendChild(el("span", "t-name", r.name));   // label follows
        tip.appendChild(line);
      }
      tip.style.opacity = 1;
      const w = tip.offsetWidth, hostW = host.clientWidth;
      tip.style.left = Math.max(0, Math.min(x + 14, hostW - w - 2)) + "px";
      tip.style.top = Math.max(0, y - 12) + "px";
    },
  };
}

/**
 * Line chart with a crosshair that snaps to the nearest x - the reader aims at a
 * date, never at a 2px line. One measure per chart: stars and forks never share
 * an axis.
 */
function lineChart(host, { ts, values, color, name, height = 132, fmt = compact }) {
  host.replaceChildren();
  const W = host.clientWidth || 560, H = height;
  const P = { t: 10, r: 10, b: 22, l: 46 };
  const iw = Math.max(10, W - P.l - P.r), ih = Math.max(10, H - P.t - P.b);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H,
                             role: "img", "aria-label": name });
  host.appendChild(svg);
  if (!ts.length) { host.appendChild(el("div", "chart-note", "no data yet")); return; }

  const x0 = ts[0], x1 = ts[ts.length - 1];
  const lo = Math.min(...values), hi = Math.max(...values);
  const ticks = niceTicks(lo, hi, 3);
  const yLo = Math.min(lo, ticks[0]), yHi = Math.max(hi, ticks[ticks.length - 1]);
  const X = t => P.l + (x1 === x0 ? iw / 2 : (t - x0) / (x1 - x0) * iw);
  const Y = v => P.t + ih - (yHi === yLo ? ih / 2 : (v - yLo) / (yHi - yLo) * ih);

  for (const tk of ticks) {                       // solid hairline grid, recessive
    svg.appendChild(svgEl("line", { x1: P.l, x2: P.l + iw, y1: Y(tk), y2: Y(tk),
                                    stroke: cssVar("--grid"), "stroke-width": 1 }));
    const lb = svgEl("text", { x: P.l - 7, y: Y(tk) + 3.5, "text-anchor": "end",
                               fill: cssVar("--text-muted"), "font-size": 10.5 });
    lb.textContent = fmt(tk);
    svg.appendChild(lb);
  }
  for (const t of [x0, x1]) {
    const lb = svgEl("text", { x: X(t), y: H - 6,
                               "text-anchor": t === x0 ? "start" : "end",
                               fill: cssVar("--text-muted"), "font-size": 10.5 });
    lb.textContent = fdate(t);
    svg.appendChild(lb);
  }

  const d = ts.map((t, i) => `${i ? "L" : "M"}${X(t).toFixed(1)},${Y(values[i]).toFixed(1)}`).join("");
  const area = svgEl("path", { d: `${d}L${X(x1).toFixed(1)},${P.t + ih}L${X(x0).toFixed(1)},${P.t + ih}Z`,
                               fill: color, "fill-opacity": 0.1 });
  svg.appendChild(area);
  svg.appendChild(svgEl("path", { d, fill: "none", stroke: color, "stroke-width": 2,
                                  "stroke-linejoin": "round", "stroke-linecap": "round" }));

  // Endpoint marker, with a 2px surface ring so it stays legible over the line.
  svg.appendChild(svgEl("circle", { cx: X(x1), cy: Y(values[values.length - 1]), r: 4,
                                    fill: color, stroke: cssVar("--surface-1"),
                                    "stroke-width": 2 }));

  const cross = svgEl("line", { y1: P.t, y2: P.t + ih, stroke: cssVar("--axis"),
                                "stroke-width": 1, opacity: 0 });
  const dot = svgEl("circle", { r: 4, fill: color, stroke: cssVar("--surface-1"),
                                "stroke-width": 2, opacity: 0 });
  svg.appendChild(cross); svg.appendChild(dot);
  const tip = makeTip(host);

  function move(ev) {
    const r = svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) / r.width * W;
    const target = x0 + (x1 - x0) * Math.min(1, Math.max(0, (px - P.l) / iw));
    let i = 0, best = Infinity;
    for (let k = 0; k < ts.length; k++) {
      const dd = Math.abs(ts[k] - target);
      if (dd < best) { best = dd; i = k; }
    }
    cross.setAttribute("x1", X(ts[i])); cross.setAttribute("x2", X(ts[i]));
    cross.setAttribute("opacity", 1);
    dot.setAttribute("cx", X(ts[i])); dot.setAttribute("cy", Y(values[i]));
    dot.setAttribute("opacity", 1);
    tip.show(X(ts[i]) / W * (host.clientWidth || W), Y(values[i]), fdatetime(ts[i]),
             [{ color, name, value: nf.format(Math.round(values[i])) }]);
  }
  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerleave", () => {
    cross.setAttribute("opacity", 0); dot.setAttribute("opacity", 0); tip.hide();
  });
}

/**
 * Daily gains as columns with a trailing 7-day mean overlaid. Both marks are
 * stars/day, so they legitimately share one axis - this is not a dual-axis chart.
 * Each column is its own hit target.
 */
function columnChart(host, { ts, values, mean, color, height = 168 }) {
  host.replaceChildren();
  const W = host.clientWidth || 560, H = height;
  const P = { t: 10, r: 10, b: 22, l: 46 };
  const iw = Math.max(10, W - P.l - P.r), ih = Math.max(10, H - P.t - P.b);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H,
                             role: "img", "aria-label": "New stars per day" });
  host.appendChild(svg);
  if (!ts.length) { host.appendChild(el("div", "chart-note", "no data yet")); return; }

  const hi = Math.max(1, ...values), lo = Math.min(0, ...values);
  const ticks = niceTicks(lo, hi, 3);
  const yHi = Math.max(hi, ticks[ticks.length - 1]), yLo = Math.min(lo, 0);
  const Y = v => P.t + ih - (v - yLo) / (yHi - yLo) * ih;
  const band = iw / ts.length;
  const bw = Math.max(1, Math.min(24, band - 2));   // the 2px surface gap
  const X = i => P.l + band * i + (band - bw) / 2;

  for (const tk of ticks) {
    svg.appendChild(svgEl("line", { x1: P.l, x2: P.l + iw, y1: Y(tk), y2: Y(tk),
                                    stroke: cssVar("--grid"), "stroke-width": 1 }));
    const lb = svgEl("text", { x: P.l - 7, y: Y(tk) + 3.5, "text-anchor": "end",
                               fill: cssVar("--text-muted"), "font-size": 10.5 });
    lb.textContent = compact(tk);
    svg.appendChild(lb);
  }
  const zeroY = Y(0);
  svg.appendChild(svgEl("line", { x1: P.l, x2: P.l + iw, y1: zeroY, y2: zeroY,
                                  stroke: cssVar("--axis"), "stroke-width": 1 }));

  const r = Math.min(4, bw / 2);
  values.forEach((v, i) => {
    const y = Y(v), h = Math.abs(zeroY - y);
    const top = Math.min(y, zeroY);
    // Rounded at the data end, square at the baseline.
    const path = v >= 0
      ? `M${X(i)},${top + h}V${top + r}q0,-${r} ${r},-${r}h${bw - 2 * r}q${r},0 ${r},${r}V${top + h}Z`
      : `M${X(i)},${top}V${top + h - r}q0,${r} ${r},${r}h${bw - 2 * r}q${r},0 ${r},-${r}V${top}Z`;
    svg.appendChild(svgEl("path", { d: h < 0.7 ? `M${X(i)},${zeroY}h${bw}v0.7h-${bw}Z` : path,
                                    fill: color }));
  });

  if (mean && mean.length === ts.length) {
    const d = mean.map((v, i) => v === null ? null
      : `${i && mean[i - 1] !== null ? "L" : "M"}${(X(i) + bw / 2).toFixed(1)},${Y(v).toFixed(1)}`)
      .filter(Boolean).join("");
    svg.appendChild(svgEl("path", { d, fill: "none", stroke: cssVar("--text-secondary"),
                                    "stroke-width": 2, "stroke-linecap": "round",
                                    "stroke-linejoin": "round", opacity: 0.85 }));
  }
  for (const i of [0, ts.length - 1]) {
    const lb = svgEl("text", { x: X(i) + bw / 2, y: H - 6,
                               "text-anchor": i === 0 ? "start" : "end",
                               fill: cssVar("--text-muted"), "font-size": 10.5 });
    lb.textContent = fdate(ts[i]);
    svg.appendChild(lb);
  }

  const hl = svgEl("rect", { fill: cssVar("--text-primary"), opacity: 0, rx: 3 });
  svg.appendChild(hl);
  const tip = makeTip(host);
  svg.addEventListener("pointermove", ev => {
    const rect = svg.getBoundingClientRect();
    const px = (ev.clientX - rect.left) / rect.width * W;
    const i = Math.max(0, Math.min(ts.length - 1, Math.floor((px - P.l) / band)));
    hl.setAttribute("x", P.l + band * i); hl.setAttribute("y", P.t);
    hl.setAttribute("width", band); hl.setAttribute("height", ih);
    hl.setAttribute("opacity", 0.055);
    const rows = [{ color, name: "new stars", value: nf.format(Math.round(values[i])) }];
    if (mean && mean[i] !== null && mean[i] !== undefined)
      rows.push({ color: cssVar("--text-secondary"), name: "7-day mean",
                  value: nf.format(Math.round(mean[i])) });
    tip.show(P.l + band * i + band, Y(Math.max(values[i], 0)), fdate(ts[i]), rows);
  });
  svg.addEventListener("pointerleave", () => { hl.setAttribute("opacity", 0); tip.hide(); });
}

/* Rank over the period. The scale is inverted so a line going UP means the repo
   is climbing - readers expect "up is better", and a raw rank axis inverts that.
   Single series, so no legend; the net chip beside it carries the direction. */
function rankSpark(points, w = 74, h = 20) {
  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, width: w, height: h,
                             "aria-hidden": "true" });
  if (!points || points.length < 2) return svg;
  const lo = Math.min(...points), hi = Math.max(...points);
  const X = i => (i / (points.length - 1)) * (w - 5) + 2.5;
  // Flat history draws down the middle rather than collapsing onto an edge.
  const Y = v => (hi === lo) ? h / 2 : 3 + ((v - lo) / (hi - lo)) * (h - 6);
  const c = cssVar("--series-1");
  const d = points.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  svg.appendChild(svgEl("path", { d, fill: "none", stroke: c, "stroke-width": 1.75,
                                  "stroke-linejoin": "round", "stroke-linecap": "round" }));
  svg.appendChild(svgEl("circle", { cx: X(points.length - 1), cy: Y(points[points.length - 1]),
                                    r: 2.6, fill: c, stroke: cssVar("--surface-1"),
                                    "stroke-width": 1.5 }));
  return svg;
}

function trendCell(t) {
  const td = el("td");
  if (!t || !t.readings) { td.appendChild(el("span", "dim", "-")); return td; }
  const wrap = el("div", "trendcell");
  const net = t.net;
  const chip = el("span", "netchip " + (net > 0 ? "up" : net < 0 ? "down" : "flat"),
                  net > 0 ? "↑" + net : net < 0 ? "↓" + Math.abs(net) : "=");
  wrap.appendChild(chip);
  wrap.appendChild(rankSpark(t.points));
  wrap.title = t.readings < 2
    ? "one reading so far - a line needs at least two"
    : `rank ${t.points[0]} → ${t.points[t.points.length - 1]} over `
      + `${t.readings} readings` + (t.board ? ` on the ${t.board} board` : "");
  td.appendChild(wrap);
  return td;
}

function strengthCell(share) {
  const td = el("td");
  const wrap = el("div", "strength");
  wrap.appendChild(el("span", "pctv", share == null ? "-" : (share * 100).toFixed(1) + "%"));
  const bar = el("div", "bar");
  const lvl = el("div", "lvl");
  lvl.style.width = Math.max(0, Math.min(100, (share || 0) * 100)) + "%";
  bar.appendChild(lvl);
  wrap.appendChild(bar);
  if (share != null)
    wrap.title = (share * 100).toFixed(1) + "% of this repo's total stars arrived in this period";
  td.appendChild(wrap);
  return td;
}

/* A sparkline is a stat-tile trend element: no axes, no legend, most recent
   bucket accented so the eye lands on "now". */
function sparkline(values, w = 108, h = 24) {
  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, width: w, height: h,
                             "aria-hidden": "true" });
  if (!values || values.length < 2) return svg;
  const lo = Math.min(0, ...values), hi = Math.max(1, ...values);
  const X = i => (i / (values.length - 1)) * (w - 4) + 2;
  const Y = v => h - 3 - (v - lo) / (hi - lo) * (h - 7);
  const d = values.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  const c = cssVar("--series-1");
  svg.appendChild(svgEl("path", { d: `${d}L${X(values.length - 1)},${h - 1}L${X(0)},${h - 1}Z`,
                                  fill: c, "fill-opacity": 0.12 }));
  svg.appendChild(svgEl("path", { d, fill: "none", stroke: c, "stroke-width": 1.5,
                                  "stroke-linejoin": "round", "stroke-linecap": "round" }));
  svg.appendChild(svgEl("circle", { cx: X(values.length - 1), cy: Y(values[values.length - 1]),
                                    r: 2.4, fill: c }));
  return svg;
}

/* --------------------------------------------------------------- landscape */

/* Size vs strength. Stars span three orders of magnitude (1k -> 250k), so x is
   log - on a linear axis every small repo collapses onto the left edge, which is
   precisely the band worth looking at. y is surge, already a percentage of the
   repo's own total, so it stays linear on its natural 0-100% domain.

   The interesting quadrant is top-LEFT: small and moving hard. Top-right is a
   giant having a big day, which is a different (and rarer) thing. */
function scatterChart(host, rows, onPick) {
  host.replaceChildren();
  const pts = rows.filter(r => r.stars > 0 && r.share != null);
  const W = host.clientWidth || 620, H = 300;
  const P = { t: 26, r: 16, b: 34, l: 46 };
  const iw = Math.max(10, W - P.l - P.r), ih = Math.max(10, H - P.t - P.b);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img",
                             "aria-label": "Star count versus surge" });
  host.appendChild(svg);
  if (!pts.length) { host.appendChild(el("div", "chart-note", "no data yet")); return; }

  const lx = v => Math.log10(Math.max(v, 1));
  const x0 = Math.floor(Math.min(...pts.map(p => lx(p.stars))));
  const x1 = Math.ceil(Math.max(...pts.map(p => lx(p.stars))));
  const yMax = Math.max(0.1, Math.max(...pts.map(p => p.share)));
  const X = v => P.l + (lx(v) - x0) / Math.max(0.001, x1 - x0) * iw;
  const Y = v => P.t + ih - (v / yMax) * ih;

  // The size band, shaded lightly: the region matters, its exact edges do not.
  const bandL = Math.max(P.l, X(GOLD_LO)), bandR = Math.min(P.l + iw, X(GOLD_HI));
  if (bandR > bandL) {
    svg.appendChild(svgEl("rect", { x: bandL, y: P.t, width: bandR - bandL, height: ih,
                                    fill: cssVar("--series-1"), "fill-opacity": 0.05 }));
    const lb = svgEl("text", { x: (bandL + bandR) / 2, y: P.t + ih - 5,
                               "text-anchor": "middle",
                               fill: cssVar("--text-muted"), "font-size": 10 });
    lb.textContent = "1k – 20k band";
    svg.appendChild(lb);
    // Golden zone = that band AND moving hard. Small and surging at once is the
    // only combination the whole tool is looking for.
    goldZone(svg, bandL, bandR, P.t, Math.min(P.t + ih, Y(GOLD_SURGE)),
             "golden \u00b7 small & surging");
  }

  for (let d = x0; d <= x1; d++) {
    svg.appendChild(svgEl("line", { x1: X(10 ** d), x2: X(10 ** d), y1: P.t, y2: P.t + ih,
                                    stroke: cssVar("--grid"), "stroke-width": 1 }));
    const lb = svgEl("text", { x: X(10 ** d), y: H - 18, "text-anchor": "middle",
                               fill: cssVar("--text-muted"), "font-size": 10.5 });
    lb.textContent = compact(10 ** d);
    svg.appendChild(lb);
  }
  for (const f of [0, 0.25, 0.5, 0.75, 1]) {
    const v = yMax * f;
    svg.appendChild(svgEl("line", { x1: P.l, x2: P.l + iw, y1: Y(v), y2: Y(v),
                                    stroke: cssVar("--grid"), "stroke-width": 1 }));
    const lb = svgEl("text", { x: P.l - 7, y: Y(v) + 3.5, "text-anchor": "end",
                               fill: cssVar("--text-muted"), "font-size": 10.5 });
    lb.textContent = Math.round(v * 100) + "%";
    svg.appendChild(lb);
  }
  for (const [tx, ty, txt, anchor] of [
    [P.l + iw / 2, H - 4, "total stars (log)", "middle"],
    [P.l - 40, P.t - 12, "surge - share of total stars gained", "start"]]) {
    const lb = svgEl("text", { x: tx, y: ty, "text-anchor": anchor,
                               fill: cssVar("--text-muted"), "font-size": 10.5 });
    lb.textContent = txt;
    svg.appendChild(lb);
  }

  // Radius carries how many horizons the repo spans - a second channel that does
  // not compete with position.
  const rOf = n => (n >= 3 ? 7 : n === 2 ? 5.5 : 4);
  const c = cssVar("--series-1");
  const drawn = pts.map(p => ({ ...p, cx: X(p.stars), cy: Y(p.share) }));
  const sel = tState.selected;
  const theme = activeHighlight();
  const both = tState.gold ? tState.gold.both : new Set();
  for (const p of drawn) {
    // A selection has to survive the pointer leaving: dim the rest so the chosen
    // repo stays findable while the reader looks at the other charts.
    const isSel = p.full_name === sel;
    // In BOTH golden zones: painted gold, so the same repo is recognisable at a
    // glance in this chart and in the other one without cross-referencing names.
    const isBoth = both.has(p.full_name);
    svg.appendChild(svgEl("circle", {
      cx: p.cx, cy: p.cy, r: rOf(p.board_count) + (isBoth ? 1.5 : 0),
      fill: isBoth ? cssVar("--gold") : c,
      "fill-opacity": sel ? (isSel ? 1 : 0.2)
                     : theme ? (theme.has(p.full_name) ? 0.95 : 0.12)
                     : (isBoth ? 0.95 : 0.75),
      stroke: cssVar("--surface-1"), "stroke-width": 2 }));
  }
  markSelected(svg, drawn, sel, c);
  // Direct-label only the extremes; a name on every dot is unreadable. Labels are
  // laid out in y order and nudged apart when they collide - two names printed on
  // top of each other is worse than one name.
  // Label the both-zone repos by name - they are the answer to the question the
  // page exists to ask - then fill the remaining slots with the loudest.
  const goldPts = drawn.filter(p => both.has(p.full_name));
  const rest = drawn.filter(p => !both.has(p.full_name))
    .sort((a, b) => b.share - a.share);
  const labels = goldPts.concat(rest).slice(0, Math.max(4, goldPts.length))
    .sort((a, b) => a.cy - b.cy);
  let lastY = -Infinity;
  for (const p of labels) {
    let ly = Math.max(p.cy + 3.5, lastY + 13);
    if (ly > P.t + ih) continue;              // no room left; the tooltip has it
    lastY = ly;
    const flip = p.cx > P.l + iw * 0.72;      // keep labels inside the plot
    const lb = svgEl("text", {
      x: p.cx + (flip ? -10 : 10), y: ly,
      "text-anchor": flip ? "end" : "start",
      fill: cssVar("--text-secondary"), "font-size": 10.5 });
    lb.textContent = p.full_name.split("/")[1] || p.full_name;
    svg.appendChild(lb);
    if (Math.abs(ly - (p.cy + 3.5)) > 2) {
      // Nudged off its dot, so connect the two rather than letting it float.
      svg.appendChild(svgEl("line", {
        x1: p.cx + (flip ? -3 : 3), y1: p.cy, x2: p.cx + (flip ? -8 : 8), y2: ly - 3,
        stroke: cssVar("--axis"), "stroke-width": 1 }));
    }
  }

  const halo = svgEl("circle", { r: 11, fill: "none", stroke: c, "stroke-width": 2,
                                 opacity: 0 });
  svg.appendChild(halo);
  const peer = svgEl("circle", { r: 10, fill: "none", stroke: cssVar("--gold"),
                                 "stroke-width": 2.5, opacity: 0 });
  svg.appendChild(peer);
  registerPeer("scatter", nm => {
    const q = nm && drawn.find(d2 => d2.full_name === nm);
    if (!q) { peer.setAttribute("opacity", 0); return; }
    peer.setAttribute("cx", q.cx); peer.setAttribute("cy", q.cy);
    peer.setAttribute("opacity", 1);
  });
  const tip = makeTip(host);
  let near = null;
  // Nearest-point hover: an 8px dot is a pinpoint nobody can land on reliably.
  svg.addEventListener("pointermove", ev => {
    const rect = svg.getBoundingClientRect();
    const mx = (ev.clientX - rect.left) / rect.width * W;
    const my = (ev.clientY - rect.top) / rect.height * H;
    let best = null, bd = 1e9;
    for (const p of drawn) {
      const d2 = (p.cx - mx) ** 2 + (p.cy - my) ** 2;
      if (d2 < bd) { bd = d2; best = p; }
    }
    if (best && bd < 40 ** 2) {
      near = best;
      halo.setAttribute("cx", best.cx); halo.setAttribute("cy", best.cy);
      halo.setAttribute("opacity", 0.5);
      svg.style.cursor = "pointer";
      tip.show(best.cx / W * (host.clientWidth || W), best.cy, best.full_name, [
        { color: c, name: "surge", value: (best.share * 100).toFixed(1) + "%" },
        { color: c, name: "stars", value: nf.format(best.stars) },
        { color: c, name: "boards", value: best.board_count + " of 3" },
      ]);
      setPeer(best.full_name, "scatter");
    } else {
      near = null; halo.setAttribute("opacity", 0); svg.style.cursor = "default";
      tip.hide(); setPeer(null, "scatter");
    }
  });
  svg.addEventListener("pointerleave", () => {
    near = null; halo.setAttribute("opacity", 0); tip.hide(); setPeer(null, null);
  });
  svg.addEventListener("click", () => { if (near && onPick) onPick(near.full_name); });
}

/* Is this repo gaining or losing steam?

   Two different signals can answer that, and they are not equally good, so there
   is an explicit precedence rather than a blend:

     1. Acceleration - its current rate against its own longer-horizon rate.
        Available whenever a repo sits on two boards at once, and it needs no
        stored history, which makes it the strongest thing we have today.
     2. Rank movement - only meaningful once several readings exist.

   A neutral band around 1x stops ordinary noise from flipping the arrow every
   sweep. The tooltip always says which basis was used, because an arrow that
   silently changes meaning between rows would be worse than no arrow. */
const DIR_UP = 1.15, DIR_DOWN = 0.87;

function directionOf(r) {
  const a = accelOf(r);
  if (a) {
    const slower = a.label.split(" vs ")[1] || "recent";
    if (a.ratio >= DIR_UP)
      return { dir: 1, why: `speeding up - ${a.ratio.toFixed(1)}× its ${slower} pace` };
    if (a.ratio <= DIR_DOWN)
      return { dir: -1, why: `slowing - ${(1 / a.ratio).toFixed(1)}× below its ${slower} pace` };
    return { dir: 0, why: `steady - holding its ${slower} pace` };
  }
  const t = r.trend;
  if (t && t.readings > 1 && t.net)
    return { dir: Math.sign(t.net),
             why: `rank moved ${t.net > 0 ? "up" : "down"} ${Math.abs(t.net)} `
                + `over ${t.readings} readings` };
  if (t && t.readings > 1) return { dir: 0, why: "rank unchanged so far" };
  return { dir: null, why: "on one board only, and too few readings to tell yet" };
}

function dirCell(r) {
  const td = el("td", "col-dir");
  const { dir, why } = directionOf(r);
  const glyph = dir === 1 ? "\u25b2" : dir === -1 ? "\u25bc" : dir === 0 ? "\u2013" : "\u00b7";
  const cls = dir === 1 ? "up" : dir === -1 ? "down" : dir === 0 ? "flat" : "none";
  const span = el("span", "dir " + cls, glyph);
  span.title = why;
  span.setAttribute("role", "img");
  span.setAttribute("aria-label",
    dir === 1 ? "gaining steam" : dir === -1 ? "losing steam"
    : dir === 0 ? "steady" : "not enough data");
  td.appendChild(span);
  return td;
}

/* The golden zone: the corner of a chart where you actually want to find things.
   Drawn as a wash with a hairline edge and an explicit label - it is a region
   annotation, so it must never be mistaken for a data mark or a status colour.
   The threshold is stated on the chart rather than left implicit. */
const GOLD_SURGE = 0.25;              // a quarter of the repo's whole life, this period
const GOLD_LO = 1000, GOLD_HI = 20000;

function goldZone(svg, x0, x1, yTop, yBottom, label, align = "left") {
  const w = x1 - x0, h = yBottom - yTop;
  if (!(w > 2 && h > 2)) return;
  svg.appendChild(svgEl("rect", { x: x0, y: yTop, width: w, height: h, rx: 3,
                                  fill: cssVar("--gold-wash"),
                                  stroke: cssVar("--gold"), "stroke-width": 1,
                                  "stroke-opacity": 0.5 }));
  // The zone's left edge is where a reference line tends to sit, so the label can
  // be pinned to the far corner instead when that corner is already spoken for.
  const t = svgEl("text", {
    x: align === "right" ? x1 - 6 : x0 + 6, y: yTop + 12,
    "text-anchor": align === "right" ? "end" : "start",
    fill: cssVar("--gold"), "font-size": 10, "font-weight": 700,
    "letter-spacing": "0.03em" });
  t.textContent = label;
  svg.appendChild(t);
}

/* Cross-chart peer highlighting. Hovering a mark in one chart should point at the
   same repo everywhere else, but re-rendering every chart on pointermove would be
   wasteful and would destroy the tooltip mid-hover. So each chart registers a
   function that moves one pre-built marker, and hovering just calls the others. */
const PEERS = {};

function registerPeer(id, fn) { PEERS[id] = fn; }

function setPeer(name, except) {
  for (const [id, fn] of Object.entries(PEERS)) if (id !== except) fn(name);

  for (const tr of document.querySelectorAll("#t-rows tr.peer"))
    tr.classList.remove("peer");
  // The reverse of the theme hover: hovering a repo lights the themes it belongs
  // to, so the link reads in both directions rather than only downward.
  const themeRows = document.querySelectorAll("#themes .theme");
  for (const el2 of themeRows) el2.classList.remove("peer");
  if (!name) return;

  const tr = document.querySelector(`#t-rows tr[data-repo="${CSS.escape(name)}"]`);
  if (tr) tr.classList.add("peer");

  const themes = (tState.relations && tState.relations.themes) || [];
  const terms = new Set(themes.filter(t => t.repos.includes(name)).map(t => t.term));
  for (const el2 of themeRows)
    if (terms.has(el2.dataset.term)) el2.classList.add("peer");
}

/* Draws the persistent selection marker on a scatter: a ring plus the repo name,
   so a repo picked in one chart stays identifiable in the others. */
function markSelected(svg, drawn, sel, colour) {
  if (!sel) return;
  const p = drawn.find(q => q.full_name === sel);
  if (!p) return;
  svg.appendChild(svgEl("circle", { cx: p.cx, cy: p.cy, r: 11, fill: "none",
                                    stroke: colour, "stroke-width": 2 }));
  const lb = svgEl("text", { x: p.cx, y: p.cy - 16, "text-anchor": "middle",
                             fill: cssVar("--text-primary"), "font-size": 10.5,
                             "font-weight": 600 });
  lb.textContent = p.full_name.split("/")[1] || p.full_name;
  svg.appendChild(lb);
}

/* Acceleration, straight out of GitHub's three horizons - no history required.
   The same repo appears on the daily, weekly and monthly boards with "stars
   today", "stars this week" and "stars this month". Reduce each to stars/day and
   the ratio between a short and a long horizon says whether the repo is pulling
   away from its own recent average. 6.8x means today is running nearly seven
   times the week's pace. */
function accelOf(r) {
  // Measured and stored server-side at sweep time: our own snapshot rate against
  // GitHub's longest-horizon average for that repo. Deriving it here from the
  // board counts alone needed a repo on two boards at once, which was only ever
  // six of fifty-two.
  const a = r.accel;
  if (!a || !Number.isFinite(a.ratio)) return null;
  return { ratio: a.ratio, fast: a.now_rate, slow: a.long_rate,
           label: `now vs ${a.horizon} average` };
}

/* Bars around 1x. Length is log2 of the ratio, so 4x and 1/4x sit equally far
   from the baseline - on a linear scale, speeding up would dwarf slowing down and
   the chart would misstate the symmetry.

   The domain always includes 0 (= 1x) but is otherwise driven by the data, so the
   axis adapts: with nothing cooling, 1x lands on the left edge and this reads as
   an ordinary bar chart instead of a diverging one with a dead half. Names live
   in a fixed gutter so they never collide with a bar. */
function accelChart(host, rows, onPick) {
  host.replaceChildren();
  // Top five each way rather than the top ten overall: taking ten by ratio fills
  // the chart with accelerators and hides the cooling end entirely on a day when
  // most of the board is rising, which is exactly when a decline is worth seeing.
  const ranked = rows.map(r => ({ r, a: accelOf(r) })).filter(x => x.a)
    .sort((p, q) => q.a.ratio - p.a.ratio);
  const rising = ranked.filter(x => x.a.ratio >= 1).slice(0, 5);
  const cooling = ranked.filter(x => x.a.ratio < 1).slice(-5);
  const items = rising.concat(cooling);
  if (!items.length) {
    host.appendChild(el("div", "chart-note",
      "Needs a repo on two different boards at once - none right now."));
    return 0;
  }
  const rowH = 24, W = host.clientWidth || 320, H = items.length * rowH + 24;
  const GUT = 100, PAD_R = 52;
  const barL = GUT, barW = Math.max(30, W - GUT - PAD_R);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img",
                             "aria-label": "Acceleration by repo" });
  host.appendChild(svg);

  const l2s = items.map(x => Math.log2(x.a.ratio));
  const lo = Math.min(0, ...l2s), hi = Math.max(0, ...l2s);
  const span = (hi - lo) || 1;
  const X = v => barL + (v - lo) / span * barW;
  const zero = X(0);
  const up = cssVar("--series-1"), down = cssVar("--series-2");

  const selName = tState.selected;
  const themeSet = activeHighlight();
  items.forEach((x, i) => {
    const y = 4 + i * rowH, bh = 12;
    const isSel = x.r.full_name === selName;
    if (isSel) {
      svg.appendChild(svgEl("rect", { x: 0, y: y - 5, width: W, height: rowH,
                                      rx: 4, fill: cssVar("--series-1"),
                                      "fill-opacity": 0.10 }));
    }
    const l2 = Math.log2(x.a.ratio);
    const acc = l2 >= 0;
    const xEnd = X(l2);
    const w = Math.abs(xEnd - zero);
    const bx = Math.min(zero, xEnd);
    const r = Math.min(4, bh / 2);
    const d = w <= r
      ? `M${bx},${y}h${Math.max(w, 1.5)}v${bh}h-${Math.max(w, 1.5)}Z`
      : acc ? `M${zero},${y}h${w - r}q${r},0 ${r},${r}v${bh - 2 * r}q0,${r} -${r},${r}h-${w - r}Z`
            : `M${zero},${y}h-${w - r}q-${r},0 -${r},${r}v${bh - 2 * r}q0,${r} ${r},${r}h${w - r}Z`;
    const inTheme = !themeSet || themeSet.has(x.r.full_name);
    svg.appendChild(svgEl("path", { d, fill: acc ? up : down,
                                    "fill-opacity": (selName && !isSel) ? 0.3
                                                  : !inTheme ? 0.18 : 1 }));

    const name = x.r.full_name.split("/")[1] || x.r.full_name;
    const lb = svgEl("text", { x: GUT - 8, y: y + bh / 2 + 3.5, "text-anchor": "end",
                               "fill-opacity": inTheme ? 1 : 0.35,
                               fill: cssVar("--text-secondary"), "font-size": 10.5 });
    lb.textContent = name.length > 17 ? name.slice(0, 16) + "\u2026" : name;
    svg.appendChild(lb);

    // Place the value beyond the bar's data end - but a short cooling bar would
    // push its label back into the name gutter, so flip it to the empty side of
    // the centre line instead of letting the two collide.
    const text = (acc ? x.a.ratio : 1 / x.a.ratio).toFixed(1) + "\u00d7";
    const wid = text.length * 6 + 4;               // ~6px per glyph at 10.5px
    let vx = acc ? zero + w + 5 : zero - w - 5;
    let anchor = acc ? "start" : "end";
    if (!acc && vx - wid < GUT + 4) { vx = zero + 5; anchor = "start"; }
    const val = svgEl("text", {
      x: vx, y: y + bh / 2 + 3.5, "text-anchor": anchor,
      fill: cssVar("--text-primary"), "font-size": 10.5, "font-weight": 600 });
    val.textContent = text;
    svg.appendChild(val);

    const hit = svgEl("rect", { x: 0, y: y - 4, width: W, height: rowH,
                                fill: "transparent", style: "cursor:pointer" });
    hit.addEventListener("click", () => onPick && onPick(x.r.full_name));
    hit.addEventListener("pointerenter", () => setPeer(x.r.full_name, "accel"));
    hit.addEventListener("pointerleave", () => setPeer(null, null));
    const t = svgEl("title");
    t.textContent = `${x.r.full_name} - ${Math.round(x.a.fast)}/day now vs `
                  + `${Math.round(x.a.slow)}/day over the longer window (${x.a.label})`;
    hit.appendChild(t);
    svg.appendChild(hit);
  });

  svg.appendChild(svgEl("line", { x1: zero, x2: zero, y1: 2,
                                  y2: 4 + items.length * rowH - 6,
                                  stroke: cssVar("--axis"), "stroke-width": 1 }));
  // Same visual language as a highlighted theme row: a gold wash, not an outline.
  const peerBand = svgEl("rect", { x: 0, width: W, height: rowH - 2, rx: 4,
                                   fill: cssVar("--gold-wash"), opacity: 0 });
  svg.insertBefore(peerBand, svg.firstChild);
  registerPeer("accel", nm => {
    const i = items.findIndex(x => x.r.full_name === nm);
    if (i < 0) { peerBand.setAttribute("opacity", 0); return; }
    peerBand.setAttribute("y", 4 + i * rowH - 5);
    peerBand.setAttribute("opacity", 1);
  });
  const anyDown = items.some(x => x.a.ratio < 1);
  const cap = svgEl("text", { x: zero, y: H - 6,
                              "text-anchor": anyDown ? "middle" : "start",
                              fill: cssVar("--text-muted"), "font-size": 10 });
  cap.textContent = anyDown ? "\u2190 cooling   1\u00d7   accelerating \u2192"
                            : "1\u00d7 - its own recent pace";
  svg.appendChild(cap);
  return items.length;
}

/* Adoption vs attention. Surge says how hard a repo is moving; forks-per-star
   says whether anyone is actually using it. Curated lists and hype spikes sit
   top-left - lots of stars arriving, nobody forking. Real tools climb the
   right-hand side. */
function adoptionChart(host, rows, onPick) {
  host.replaceChildren();
  const pts = rows.filter(r => r.fork_ratio != null && r.share != null);
  const W = host.clientWidth || 900, H = 260;
  const P = { t: 24, r: 16, b: 34, l: 46 };
  const iw = Math.max(10, W - P.l - P.r), ih = Math.max(10, H - P.t - P.b);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img",
                             "aria-label": "Forks per star versus surge" });
  host.appendChild(svg);
  if (!pts.length) { host.appendChild(el("div", "chart-note", "no data yet")); return; }

  const xMax = Math.max(0.05, ...pts.map(p => p.fork_ratio));
  const yMax = Math.max(0.1, ...pts.map(p => p.share));
  const X = v => P.l + (v / xMax) * iw;
  const Y = v => P.t + ih - (v / yMax) * ih;
  const sorted = [...pts].map(p => p.fork_ratio).sort((a, b) => a - b);
  const medFS = sorted[Math.floor(sorted.length / 2)];

  for (const f of [0, 0.25, 0.5, 0.75, 1]) {
    svg.appendChild(svgEl("line", { x1: P.l, x2: P.l + iw, y1: Y(yMax * f), y2: Y(yMax * f),
                                    stroke: cssVar("--grid"), "stroke-width": 1 }));
    const lb = svgEl("text", { x: P.l - 7, y: Y(yMax * f) + 3.5, "text-anchor": "end",
                               fill: cssVar("--text-muted"), "font-size": 10.5 });
    lb.textContent = Math.round(yMax * f * 100) + "%";
    svg.appendChild(lb);
    const xv = xMax * f;
    const xl = svgEl("text", { x: X(xv), y: H - 18, "text-anchor": "middle",
                               fill: cssVar("--text-muted"), "font-size": 10.5 });
    xl.textContent = xv.toFixed(2);
    svg.appendChild(xl);
  }
  // Median forks-per-star: the line that separates "used" from "bookmarked".
  svg.appendChild(svgEl("line", { x1: X(medFS), x2: X(medFS), y1: P.t, y2: P.t + ih,
                                  stroke: cssVar("--axis"), "stroke-width": 1 }));
  const ml = svgEl("text", { x: X(medFS) + 5, y: P.t + 10, fill: cssVar("--text-muted"),
                             "font-size": 10 });
  ml.textContent = "median " + medFS.toFixed(3);
  svg.appendChild(ml);

  for (const [tx, ty, txt, anchor] of [
    [P.l + iw / 2, H - 4, "forks per star - adoption", "middle"],
    [P.l - 40, P.t - 10, "surge - attention", "start"]]) {
    const lb = svgEl("text", { x: tx, y: ty, "text-anchor": anchor,
                               fill: cssVar("--text-muted"), "font-size": 10.5 });
    lb.textContent = txt;
    svg.appendChild(lb);
  }

  // Golden zone = above the cohort's fork median and surging: moving hard *and*
  // being used, rather than merely bookmarked.
  goldZone(svg, X(medFS), P.l + iw, P.t, Math.min(P.t + ih, Y(GOLD_SURGE)),
           "golden \u00b7 surging & adopted", "right");

  const c = cssVar("--series-1");
  const drawn = pts.map(p => ({ ...p, cx: X(p.fork_ratio), cy: Y(p.share) }));
  const sel = tState.selected;
  const theme = activeHighlight();
  const both = tState.gold ? tState.gold.both : new Set();
  for (const p of drawn) {
    const isSel = p.full_name === sel;
    const isBoth = both.has(p.full_name);
    svg.appendChild(svgEl("circle", { cx: p.cx, cy: p.cy, r: isBoth ? 6 : 4.5,
                                      fill: isBoth ? cssVar("--gold") : c,
                                      "fill-opacity": sel ? (isSel ? 1 : 0.2)
                                        : theme ? (theme.has(p.full_name) ? 0.95 : 0.12)
                                        : (isBoth ? 0.95 : 0.75),
                                      stroke: cssVar("--surface-1"), "stroke-width": 2 }));
  }
  markSelected(svg, drawn, sel, c);
  // Label the corner cases: the loudest with no forks, and the best-adopted mover.
  const hi = drawn.filter(p => p.share > yMax * 0.45);
  const picks = [];
  if (hi.length) {
    picks.push(hi.reduce((a, b) => (b.fork_ratio < a.fork_ratio ? b : a)));
    picks.push(hi.reduce((a, b) => (b.fork_ratio > a.fork_ratio ? b : a)));
  }
  let lastY = -Infinity;
  for (const p of [...new Set(picks)].sort((a, b) => a.cy - b.cy)) {
    const ly = Math.max(p.cy + 3.5, lastY + 13);
    lastY = ly;
    const flip = p.cx > P.l + iw * 0.75;
    const lb = svgEl("text", { x: p.cx + (flip ? -9 : 9), y: ly,
                               "text-anchor": flip ? "end" : "start",
                               fill: cssVar("--text-secondary"), "font-size": 10.5 });
    lb.textContent = p.full_name.split("/")[1] || p.full_name;
    svg.appendChild(lb);
  }

  const halo = svgEl("circle", { r: 11, fill: "none", stroke: c, "stroke-width": 2,
                                 opacity: 0 });
  svg.appendChild(halo);
  const peer = svgEl("circle", { r: 10, fill: "none", stroke: cssVar("--gold"),
                                 "stroke-width": 2.5, opacity: 0 });
  svg.appendChild(peer);
  registerPeer("adopt", nm => {
    const q = nm && drawn.find(d2 => d2.full_name === nm);
    if (!q) { peer.setAttribute("opacity", 0); return; }
    peer.setAttribute("cx", q.cx); peer.setAttribute("cy", q.cy);
    peer.setAttribute("opacity", 1);
  });
  const tip = makeTip(host);
  let near = null;
  svg.addEventListener("pointermove", ev => {
    const rect = svg.getBoundingClientRect();
    const mx = (ev.clientX - rect.left) / rect.width * W;
    const my = (ev.clientY - rect.top) / rect.height * H;
    let best = null, bd = 1e9;
    for (const p of drawn) {
      const d2 = (p.cx - mx) ** 2 + (p.cy - my) ** 2;
      if (d2 < bd) { bd = d2; best = p; }
    }
    if (best && bd < 40 ** 2) {
      near = best;
      halo.setAttribute("cx", best.cx); halo.setAttribute("cy", best.cy);
      halo.setAttribute("opacity", 0.5);
      svg.style.cursor = "pointer";
      tip.show(best.cx / W * (host.clientWidth || W), best.cy, best.full_name, [
        { color: c, name: "surge", value: (best.share * 100).toFixed(1) + "%" },
        { color: c, name: "forks per star", value: best.fork_ratio.toFixed(3) },
        { color: c, name: "forks", value: nf.format(best.forks || 0) },
      ]);
      setPeer(best.full_name, "adopt");
    } else {
      near = null; halo.setAttribute("opacity", 0); svg.style.cursor = "default";
      tip.hide(); setPeer(null, "adopt");
    }
  });
  svg.addEventListener("pointerleave", () => {
    near = null; halo.setAttribute("opacity", 0); tip.hide(); setPeer(null, null);
  });
  svg.addEventListener("click", () => { if (near && onPick) onPick(near.full_name); });
}

/* Relationships between repos. Themes are the useful one: when a third of the
   board is talking about the same thing, that is the story, not any single repo
   on it. Clicking a theme filters everything below to its members. */
async function loadRelations() {
  const host = document.getElementById("relations");
  if (!host) return;
  try {
    const qs = new URLSearchParams({ since: tState.since, language: tState.lang });
    tState.relations = await fetchJSON("/api/relations?" + qs);
  } catch (e) {
    host.replaceChildren(el("div", "chart-note", "relations unavailable: " + e.message));
    return;
  }
  renderRelations();
}

function renderRelations() {
  const d = tState.relations;
  if (!d) return;

  // Theme bars sit with the other bar chart; the text relationships sit below.
  const bars = document.getElementById("themes");
  const note = document.getElementById("themes-note");
  if (bars) {
    bars.replaceChildren();
    bars.onmouseleave = () => hoverTheme(null);
    note.textContent =
      `terms shared by three or more of the ${d.repos} repos on this board - `
      + "click one to scope everything below to it";
    const max = Math.max(1, ...d.themes.map(t => t.count));
    for (const t of d.themes.slice(0, 10)) {
      const row = el("div", "theme");
      row.setAttribute("role", "button");
      row.setAttribute("aria-pressed", String(tState.theme === t.term));
      row.dataset.term = t.term;
      row.title = t.repos.join(", ");
      row.onclick = () => pickTheme(t.term);
      // Hover previews the theme in the charts without committing to it. Only the
      // charts repaint: re-filtering the table under the pointer would make rows
      // jump away as the reader scans down the list.
      row.onmouseenter = () => hoverTheme(new Set(t.repos));
      row.onmouseleave = () => hoverTheme(null);
      row.onfocus = () => hoverTheme(new Set(t.repos));
      row.onblur = () => hoverTheme(null);
      row.tabIndex = 0;
      row.appendChild(el("span", "t", t.term));
      const bar = el("div", "bar");
      const lvl = el("div", "lvl");
      lvl.style.width = Math.round((t.count / max) * 100) + "%";
      bar.appendChild(lvl);
      row.appendChild(bar);
      row.appendChild(el("span", "n", `${t.count} · ${Math.round(t.share * 100)}%`));
      bars.appendChild(row);
    }
    if (!d.themes.length)
      bars.appendChild(el("div", "chart-note", "no term is shared by three repos yet"));
  }

  const host = document.getElementById("relations");
  if (!host) return;
  host.replaceChildren();
  host.hidden = !tState.full;      // thin until cohorts carry real signal
  if (!tState.full) return;

  const owners = el("div");
  owners.appendChild(el("div", "chart-title", "same owner"));
  if (d.owners.length) {
    for (const o of d.owners) {
      const item = el("div", "rel-item");
      item.appendChild(el("span", "h", o.owner));
      item.appendChild(el("span", "m",
        " - " + o.repos.map(r => r.split("/")[1]).join(", ")));
      owners.appendChild(item);
    }
  } else {
    owners.appendChild(el("div", "chart-note",
      "no owner has more than one repo on the board"));
  }
  host.appendChild(owners);

  const moving = el("div");
  moving.appendChild(el("div", "chart-title", "moving together"));
  const cm = d.comovement;
  if (!cm.enough) {
    moving.appendChild(el("div", "chart-note", cm.note || "not enough readings yet"));
  } else if (!cm.cohorts.length) {
    moving.appendChild(el("div", "chart-note",
      `${cm.churning} repos have churned, but none share an identical presence.`));
  } else {
    for (const c of cm.cohorts) {
      const item = el("div", "rel-item");
      item.appendChild(el("span", "h", `${c.size} repos`));
      item.appendChild(el("span", "m",
        ` present in ${c.present} of ${c.of} readings - `
        + c.repos.map(r => r.split("/")[1]).slice(0, 5).join(", ")
        + (c.repos.length > 5 ? " …" : "")));
      if (c.likely_board_refresh)
        item.appendChild(el("div", "rel-caveat",
          "large block - probably the board turning over, not affinity"));
      moving.appendChild(item);
    }
  }
  host.appendChild(moving);
}

/* Preview a theme across the charts. Cheap enough to run on pointer move: three
   SVGs of ~50 marks each. */
function hoverTheme(set) {
  const same = (a, b) => (!a && !b) || (a && b && a.size === b.size
    && [...a].every(x => b.has(x)));
  if (same(set, tState.hoverRepos)) return;
  tState.hoverRepos = set;
  const box = document.getElementById("landscape");
  if (box && box._redraw) box._redraw();
}

/* What the charts should emphasise: a hover preview if there is one, otherwise
   the pinned theme. */
function activeHighlight() {
  return tState.hoverRepos || tState.themeRepos;
}

/* A theme selection scopes the table and dims non-members in the charts. */
function pickTheme(term) {
  tState.theme = tState.theme === term ? null : term;
  tState.themeRepos = null;
  if (tState.theme) {
    const t = (tState.relations.themes || []).find(x => x.term === tState.theme);
    tState.themeRepos = t ? new Set(t.repos) : null;
  }
  renderRelations();
  tRenderRows();
  const box = document.getElementById("landscape");
  if (box && box._redraw) box._redraw();
}

function renderLandscape() {
  const d = tState.data;
  const rows = d.board || [];
  const box = document.getElementById("landscape");
  if (!rows.length) { box.hidden = true; return; }
  box.hidden = false;

  const inBand = rows.filter(r => r.stars >= GOLD_LO && r.stars <= GOLD_HI).length;
  const inGold = rows.filter(r => r.stars >= GOLD_LO && r.stars <= GOLD_HI
                              && (r.share || 0) >= GOLD_SURGE).length;
  document.getElementById("scatter-note").textContent =
    `Each dot is a repo; bigger dots span more boards. ${inBand} of ${rows.length} `
    + `sit in the 1k–20k band, and ${inGold} of those are also surging `
    + `≥${GOLD_SURGE * 100}% - the golden zone. Gold dots are in `
    + `both golden zones (also forked above the cohort median).`;

  const withAccel = rows.filter(r => accelOf(r)).length;
  document.getElementById("accel-note").textContent = withAccel
    ? `our measured rate now ÷ the repo's own weekly or monthly average from `
      + `github. top five each way, of ${withAccel} repos with enough history.`
    : "needs a few hours of snapshots and a board figure to compare against.";

  const medAdopt = (() => {
    const v = rows.map(r => r.fork_ratio).filter(x => x != null).sort((a, b) => a - b);
    return v.length ? v[Math.floor(v.length / 2)] : null;
  })();
  const inGoldAdopt = medAdopt
    ? rows.filter(r => (r.fork_ratio || 0) >= medAdopt && (r.share || 0) >= GOLD_SURGE).length
    : 0;
  document.getElementById("adopt-note").textContent =
    "Surge says how hard it is moving; forks per star says whether anyone is using "
    + "it. Top-left is attention without adoption - lists and hype spikes."
    + (medAdopt ? ` Cohort median is ${medAdopt.toFixed(3)} forks per star; `
                  + `${inGoldAdopt} repos are above it and surging.` : "");

  const draw = () => {
    scatterChart(document.getElementById("scatter"), rows, pickRepo);
    accelChart(document.getElementById("accel"), rows, pickRepo);
    adoptionChart(document.getElementById("adopt"), rows, pickRepo);
  };
  draw();
  box._redraw = draw;
}

/* ------------------------------------------------------------------- data */
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

async function load() {
  document.body.classList.add("loading");   // hold the frame, no skeleton flash
  try {
    state.data = await fetchJSON(`/api/overview?window=${state.window}`);
    renderAll();
  } catch (e) {
    const box = document.getElementById("empty");
    box.hidden = false;
    box.replaceChildren(el("div", null, "Could not load data: " + e.message));
  } finally {
    document.body.classList.remove("loading");
  }
}

function visibleRows() {
  const d = state.data;
  if (!d) return [];
  let rows = d.repos.slice();
  if (state.onlyReady) rows = rows.filter(r => r.has_window);
  if (state.band) {
    const [lo, hi] = state.band.split("-");
    rows = rows.filter(r => r.stars >= (+lo || 0) && (hi === "" || r.stars <= +hi));
  }
  if (state.lang) rows = rows.filter(r => (r.language || "") === state.lang);
  if (state.q) {
    const q = state.q.toLowerCase();
    rows = rows.filter(r => r.full_name.toLowerCase().includes(q) ||
                            (r.description || "").toLowerCase().includes(q));
  }
  const k = state.sort, dir = state.dir;
  rows.sort((a, b) => {
    let av = a[k], bv = b[k];
    if (k === "doubling_days") { av = av === null ? null : -av; bv = bv === null ? null : -bv; }
    if (k === "full_name") return dir * a.full_name.localeCompare(b.full_name);
    const an = av === null || av === undefined, bn = bv === null || bv === undefined;
    if (an && bn) return 0;
    if (an) return 1;            // unknowns always last
    if (bn) return -1;
    return dir * (av - bv);
  });
  return rows;
}

/* --------------------------------------------------------------- rendering */
function renderTiles() {
  const d = state.data, host = document.getElementById("tiles");
  host.replaceChildren();
  const s = d.stats;
  const spanDays = s.first_ts && s.last_ts ? (s.last_ts - s.first_ts) / 86400 : 0;
  const ready = d.repos.filter(r => r.has_window).length;
  const hot = d.repos.filter(r => r.heat !== null && r.heat >= d.breakout_heat &&
                                  r.z !== null && r.z >= d.breakout_min_z).length;
  const tiles = [
    ["tracked", nf.format(s.repos_tracked), `${nf.format(s.snapshots)} snapshots`,
     "repos being tracked"],
    ["history", spanDays >= 1 ? spanDays.toFixed(1) + "d" : "< 1d",
     spanDays >= state.window ? "covered" : `need ${state.window}d`,
     "span of stored snapshots"],
    ["last run", ago(s.last_ts), "", fdatetime(s.last_ts)],
    ["breakouts", String(hot), `of ${ready}`, "repos over the breakout threshold"],
  ];
  for (const [k, v, n, full] of tiles) host.appendChild(metric(k, v, n, full));
}

function renderHead() {
  const head = document.getElementById("head");
  head.replaceChildren();
  for (const c of COLUMNS) {
    const th = el("th", c.cls || null);
    if (c.align === "left") th.style.textAlign = "left";
    if (c.title) th.title = c.title;
    th.appendChild(document.createTextNode(c.dynamic ? c.dynamic(state.window) : c.label));
    if (c.sortable !== false && state.sort === c.key)
      th.appendChild(el("span", "arrow", state.dir < 0 ? " ▼" : " ▲"));
    if (c.sortable !== false) {
      th.onclick = () => {
        if (state.sort === c.key) state.dir *= -1;
        else { state.sort = c.key; state.dir = -1; }
        renderHead(); renderRows();
      };
    } else th.style.cursor = "default";
    head.appendChild(th);
  }
}

function renderRows() {
  const rows = visibleRows();
  const body = document.getElementById("rows");
  const empty = document.getElementById("empty");
  body.replaceChildren();
  empty.hidden = rows.length > 0;
  if (!rows.length) {
    empty.replaceChildren(el("div", null,
      state.onlyReady && state.data.repos.length
        ? `No repo has ${state.window} days of history yet. Keep gittrack run on a schedule, or untick “enough history”.`
        : "Nothing matches these filters."));
    return;
  }

  rows.forEach((r, i) => {
    const tr = el("tr");
    tr.setAttribute("aria-selected", state.selected === r.full_name ? "true" : "false");
    tr.onclick = () => selectRepo(r.full_name);

    tr.appendChild(el("td", "col-rank", i + 1));

    const nameTd = el("td", "col-repo");
    const line = el("div");
    line.appendChild(repoLink(r.full_name, r.url));
    if (r.hot_since) {
      const b = el("span", "badge breakout");
      b.appendChild(el("span", "dot"));
      b.appendChild(document.createTextNode("BREAKOUT"));
      b.title = "Hot since " + fdate(r.hot_since);
      line.appendChild(document.createTextNode(" "));
      line.appendChild(b);
    } else if (r.age_days !== null && r.age_days < 90) {
      const b = el("span", "badge new");
      b.appendChild(document.createTextNode("NEW"));
      b.title = Math.round(r.age_days) + " days old";
      line.appendChild(document.createTextNode(" "));
      line.appendChild(b);
    }
    nameTd.appendChild(line);
    const meta = [r.language, r.description].filter(Boolean).join(" · ");
    if (meta) nameTd.appendChild(el("div", "repo-meta", meta));
    tr.appendChild(nameTd);

    tr.appendChild(el("td", null, compact(r.stars)));
    const dt = el("td", r.delta_stars > 0 ? "up" : r.delta_stars < 0 ? "down" : "dim",
                  compact(r.delta_stars, true));
    tr.appendChild(dt);
    tr.appendChild(el("td", null, pct(r.rel_velocity)));
    tr.appendChild(el("td", null, compact(r.velocity)));
    tr.appendChild(el("td", r.z === null ? "dim" : null,
                      r.z === null ? "-" : r.z.toFixed(1)));
    tr.appendChild(el("td", r.accel_ratio === null ? "dim" : null,
                      r.accel_ratio === null ? "-" : r.accel_ratio.toFixed(1) + "×"));
    tr.appendChild(el("td", r.fork_confirm === null ? "dim" : null,
                      r.fork_confirm === null ? "-" : r.fork_confirm.toFixed(2)));
    tr.appendChild(el("td", r.doubling_days === null ? "dim" : null,
                      r.doubling_days === null || r.doubling_days > 3650
                        ? "-" : Math.round(r.doubling_days) + "d"));

    const heatTd = el("td");
    const meter = el("div", "meter");
    const track = el("div", "track");
    const fill = el("div", "fill");
    fill.style.width = Math.max(0, Math.min(100, r.heat || 0)) + "%";
    track.appendChild(fill);
    meter.appendChild(el("span", "num", r.heat === null ? "-" : Math.round(r.heat)));
    meter.appendChild(track);
    heatTd.appendChild(meter);
    tr.appendChild(heatTd);

    const sparkTd = el("td");
    sparkTd.appendChild(sparkline(r.spark));
    tr.appendChild(sparkTd);

    body.appendChild(tr);
  });
}

function renderFilters() {
  const seg = document.getElementById("window-seg");
  if (!seg.children.length) {
    for (const w of [1, 7, 14, 30]) {
      const b = el("button", null, w + "d");
      b.onclick = () => { state.window = w; renderFilters(); load(); };
      seg.appendChild(b);
    }
  }
  [...seg.children].forEach((b, i) =>
    b.setAttribute("aria-pressed", String([1, 7, 14, 30][i] === state.window)));

  const langs = [...new Set((state.data?.repos || []).map(r => r.language).filter(Boolean))].sort();
  const sel = document.getElementById("lang");
  if (sel.options.length - 1 !== langs.length) {
    const cur = sel.value;
    sel.replaceChildren(el("option", null, "any"));
    sel.firstChild.value = "";
    for (const l of langs) { const o = el("option", null, l); o.value = l; sel.appendChild(o); }
    sel.value = cur;
  }
}

/* ---------------------------------------------------------------- detail */
async function selectRepo(name) {
  state.selected = name;
  const host = document.getElementById("detail");
  host.hidden = false;
  host.replaceChildren(el("div", "card", "loading…"));
  renderRows();
  try {
    const d = await fetchJSON(
      `/api/repo?name=${encodeURIComponent(name)}&window=${state.window}&days=365`);
    renderDetail(d);
  } catch (e) {
    host.replaceChildren(el("div", "card", "Could not load " + name + ": " + e.message));
  }
  host.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderDetail(d) {
  const host = document.getElementById("detail");
  const m = d.metrics, r = d.repo;
  const card = el("div", "card");

  const head = el("div", "dhead");
  const h2 = el("h2");
  const a = el("a", null, r.full_name);
  a.href = r.url; a.target = "_blank"; a.rel = "noopener noreferrer";
  h2.appendChild(a);
  head.appendChild(h2);
  if (d.hot_since) {
    const b = el("span", "badge breakout");
    b.appendChild(el("span", "dot"));
    b.appendChild(document.createTextNode("BREAKOUT SINCE " + fdate(d.hot_since)));
    head.appendChild(b);
  }
  head.appendChild(el("span", "spacer"));
  const close = el("button", "ghost", "✕ close");
  close.onclick = () => { host.hidden = true; state.selected = null; renderRows(); };
  head.appendChild(close);
  card.appendChild(head);

  if (r.description) card.appendChild(el("div", "sub", r.description));

  const grid = el("div", "dgrid");
  const stat = (k, v, note) => {
    const s = el("div", "dstat");
    s.appendChild(el("div", "k", k));
    s.appendChild(el("div", "v", v));
    if (note) s.appendChild(el("div", "k", note));
    grid.appendChild(s);
  };
  stat("stars", nf.format(m.stars), compact(m.delta_stars, true) + ` in ${m.window_days}d`);
  stat("stars / day", compact(m.velocity),
       m.prior_velocity !== null ? "was " + compact(m.prior_velocity) : "");
  stat("relative", pct(m.rel_velocity),
       m.rel_pct !== null ? Math.round(m.rel_pct * 100) + "th pct of cohort" : "");
  stat("robust z", m.z === null ? "-" : m.z.toFixed(1),
       m.baseline_median !== null ? "baseline " + compact(m.baseline_median) + "/day" : "no baseline");
  stat("acceleration", m.accel_ratio === null ? "-" : m.accel_ratio.toFixed(2) + "×",
       "vs previous window");
  stat("fork confirm", m.fork_confirm === null ? "-" : m.fork_confirm.toFixed(2),
       m.fork_confirm === null ? "no fork data"
         : m.fork_confirm < 0.15 ? "star-only spike" : "forks tracking stars");
  stat("doubling", m.doubling_days === null || m.doubling_days > 3650
        ? "-" : Math.round(m.doubling_days) + "d", "at the current rate");
  stat("heat", m.heat === null ? "-" : Math.round(m.heat),
       m.heat_parts ? Object.entries(m.heat_parts)
         .map(([k, v]) => `${k} ${v.toFixed(2)}`).join("  ") : "");
  stat("age", r.created_at ? ((Date.now() / 1000 - r.created_at) / 31557600).toFixed(1) + "y" : "-",
       "created " + fdate(r.created_at));
  stat("observations", m.n_obs, m.span_days.toFixed(1) + " days of history");
  card.appendChild(grid);

  if (m.note) card.appendChild(el("div", "chart-note", "⚠ " + m.note));

  const charts = el("div", "charts");

  // 1) Daily gains + trailing mean. Both are stars/day: one axis, two marks.
  const gains = el("div");
  gains.appendChild(el("div", "chart-title", "New stars per day"));
  const legend = el("div", "legend");
  const i1 = el("span", "item");
  const k1 = el("span", "key-rect"); k1.style.background = cssVar("--series-1");
  i1.appendChild(k1); i1.appendChild(document.createTextNode("daily"));
  const i2 = el("span", "item");
  const k2 = el("span", "key-line"); k2.style.background = cssVar("--text-secondary");
  i2.appendChild(k2); i2.appendChild(document.createTextNode("7-day mean"));
  legend.appendChild(i1); legend.appendChild(i2);
  gains.appendChild(legend);
  const gplot = el("div", "plot");
  gains.appendChild(gplot);
  charts.appendChild(gains);

  // 2) Small multiples - never the same axis as each other.
  const smalls = el("div", "smalls");
  const cum = el("div");
  cum.appendChild(el("div", "chart-title", "Total stars"));
  const cplot = el("div", "plot"); cum.appendChild(cplot);
  const frk = el("div");
  frk.appendChild(el("div", "chart-title", "Total forks"));
  const fplot = el("div", "plot"); frk.appendChild(fplot);
  smalls.appendChild(cum); smalls.appendChild(frk);
  charts.appendChild(smalls);
  card.appendChild(charts);

  // Table twin: every plotted value is reachable without hovering.
  const twin = el("details", "table-twin");
  twin.appendChild(el("summary", null, "Show the numbers"));
  const tbl = el("table");
  const thead = el("tr");
  for (const h of ["date", "stars", "forks", "new stars"]) {
    const th = el("th", null, h); th.style.cursor = "default"; thead.appendChild(th);
  }
  tbl.appendChild(thead);
  const gainByDay = new Map(d.daily.ts.map((t, i) => [fdate(t), d.daily.gain[i]]));
  const seenDay = new Set();
  for (let i = d.series.ts.length - 1; i >= 0 && seenDay.size < 60; i--) {
    const day = fdate(d.series.ts[i]);
    if (seenDay.has(day)) continue;
    seenDay.add(day);
    const tr = el("tr");
    tr.appendChild(el("td", null, day));
    tr.appendChild(el("td", null, nf.format(d.series.stars[i])));
    tr.appendChild(el("td", null, d.series.forks[i] === null ? "-" : nf.format(d.series.forks[i])));
    const g = gainByDay.get(day);
    tr.appendChild(el("td", null, g === undefined ? "-" : nf.format(Math.round(g))));
    tbl.appendChild(tr);
  }
  twin.appendChild(tbl);
  card.appendChild(twin);

  host.replaceChildren(card);

  const mean = trailingMean(d.daily.gain, 7);
  const draw = () => {
    columnChart(gplot, { ts: d.daily.ts, values: d.daily.gain, mean,
                         color: cssVar("--series-1") });
    lineChart(cplot, { ts: d.series.ts, values: d.series.stars,
                       color: cssVar("--series-1"), name: "total stars" });
    const fTs = [], fVals = [];
    d.series.forks.forEach((v, i) => {
      if (v !== null) { fTs.push(d.series.ts[i]); fVals.push(v); }
    });
    lineChart(fplot, { ts: fTs, values: fVals, color: cssVar("--series-2"),
                       name: "total forks" });
  };
  draw();
  host._redraw = draw;
}

function trailingMean(values, n) {
  const out = [];
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= n) sum -= values[i - n];
    out.push(i >= n - 1 ? sum / n : null);
  }
  return out;
}

function renderAll() {
  renderFilters();
  renderTiles();
  renderHead();
  renderRows();
  document.getElementById("gen").textContent =
    " · data as of " + fdatetime(state.data.generated_at);
}

/* ---------------------------------------------------------------- trending */
const tState = {
  since: "combined", lang: "", period: "1w", rankBy: "surge",
  armDiff: false, flash: null, flashTimer: null, gold: null,
  relations: null, theme: null, themeRepos: null, hoverRepos: null,
  digest: null, showSeen: false,
  // Simple by default; "more" is remembered per browser.
  full: (() => { try { return localStorage.getItem("gittrack-full") === "1"; } catch (e) { return false; } })(),
  sort: "rank", dir: 1, data: null, selected: null,
};

const T_COLUMNS_COMBINED = [
  { key: "rank", label: "#", cls: "col-rank" },
  { key: "direction", label: "", cls: "col-dir",
    title: "Gaining or losing steam. Based on acceleration against the repo's own "
         + "longer-horizon rate where it is on two boards; on rank movement "
         + "otherwise. Hover a row's arrow for which one it used." },
  { key: "full_name", label: "repo", cls: "col-repo", align: "left" },
  { key: "language", label: "language", align: "left" },
  { key: "stars", label: "stars" },
  { key: "period_stars", label: "period" },
  { key: "share", label: "surge",
    title: "Share of the repo's entire star count that arrived in this period - "
         + "the strength of the move. A repo gaining 1.6k on a 31k base (5%) is "
         + "a far louder signal than one gaining 1.6k on a 247k base (0.6%)." },
  { key: "trend", label: "rank trend", sortable: false,
    title: "Rank over the selected period, inverted so up means climbing. "
         + "The chip is the net move across the window." },
  { key: "forks", label: "forks" },
  { key: "fork_ratio", label: "f/s",
    title: "Forks per star. High means the repo is being used, not just "
         + "bookmarked - available immediately, no history needed." },
  { key: "fork_rel", label: "Δforks",
    title: "Fork growth over the summary window, from our own snapshots. "
         + "GitHub publishes no forks board, so this is derived and only "
         + "appears once snapshot history spans the window." },
  { key: "board_count", label: "boards",
    title: "Daily, Weekly, Monthly - which boards the repo is on right now" },
  { key: "positions", label: "D / W / M", sortable: false,
    title: "Its rank on each board" },
  { key: "score", label: "score",
    title: "Sum of normalised positions across the three boards. Rank 1 on a "
         + "board scores 1.0, last scores ~0, absent scores 0 - so reaching the "
         + "top needs both a high placing and presence on more than one horizon. "
         + "Normalised by each board's length, because the boards differ in size." },
];


async function tLoad() {
  document.body.classList.add("loading");
  try {
    const qs = new URLSearchParams({
      since: tState.since, language: tState.lang,
      compare: tState.period, over: tState.period, rank_by: tState.rankBy,
    });
    // A fetch was requested: remember the board we are about to replace so the
    // arrivals and departures can be shown rather than silently swapped in.
    const prev = tState.armDiff && tState.data
      && tState.data.since === tState.since
      && tState.data.language === tState.lang ? tState.data.board : null;

    tState.data = await fetchJSON("/api/trending?" + qs);
    if (prev) markChanges(prev, tState.data.board);
    tState.armDiff = false;
    tRenderAll();
  } catch (e) {
    const box = document.getElementById("t-empty");
    box.hidden = false;
    box.replaceChildren(el("div", null, "Could not load trending: " + e.message));
  } finally {
    document.body.classList.remove("loading");
  }
}

function tRenderFilters() {
  const mk = (host, values, key, labels) => {
    if (!host.children.length) {
      values.forEach((v, i) => {
        const b = el("button", null, labels ? labels[i] : v);
        b.onclick = () => {
          tState[key] = v;
          if (key === "since" || key === "rankBy") {
            tState.sort = "rank"; tState.dir = 1;
          }
          if (key === "since") { tState.theme = null; tState.themeRepos = null; }
          tRenderFilters(); tLoad();
        };
        host.appendChild(b);
      });
    }
    [...host.children].forEach((b, i) =>
      b.setAttribute("aria-pressed", String(values[i] === tState[key])));
  };
  mk(document.getElementById("t-since"),
     ["combined", "daily", "weekly", "monthly"], "since",
     ["combined", "daily", "weekly", "monthly"]);
  mk(document.getElementById("t-rankby"),
     ["surge", "stars", "forks", "both"], "rankBy");
  // One control, two consumers: movement is measured against the reading one
  // period ago, and the summary window is that same period.
  mk(document.getElementById("t-period"), ["1d", "1w", "1m", "3m"], "period");
  // Movement and the summary window are per-board concepts; the combined board
  // is a single merged snapshot, so hide the controls that do not apply to it.
  // Address each group through the control it contains, not by position: an
  // index-based version broke silently the moment a group was inserted ahead of
  // them, hiding the language picker and revealing a window that does not apply.
  const combined = tState.since === "combined";
  const groupFor = id => document.getElementById(id)?.closest(".fgroup");
  const show = (id, on) => {
    const g = groupFor(id);
    if (g) g.style.display = on ? "" : "none";
  };
  show("t-rankby", combined && tState.full);   // exploration: full mode only
  show("t-period", !combined);                 // a merged board has no previous reading
  const more = document.getElementById("t-more");
  if (more) {
    more.textContent = tState.full ? "less" : "more";
    more.setAttribute("aria-pressed", String(tState.full));
    more.onclick = () => {
      tState.full = !tState.full;
      try { localStorage.setItem("gittrack-full", tState.full ? "1" : "0"); } catch (e) { /* private mode */ }
      tRenderAll();
    };
  }
  requestAnimationFrame(syncSticky);

  const sel = document.getElementById("t-lang");
  // `boards` has one row per (since, language), so every language appears once
  // per board - dedupe or the combo lists each three times.
  const boards = [...new Set((tState.data?.boards || [])
    .map(b => b.language).filter(Boolean))].sort();
  if (sel.options.length - 1 !== boards.length) {
    const cur = sel.value;
    sel.replaceChildren(Object.assign(el("option", null, "all"), { value: "" }));
    for (const l of boards) {
      const o = el("option", null, l); o.value = l; sel.appendChild(o);
    }
    sel.value = cur;
  }
}

function tRenderTiles() {
  const d = tState.data, host = document.getElementById("t-tiles");
  host.replaceChildren();
  const rows = d.board || [];
  if (d.combined) {
    const three = rows.filter(r => r.board_count === 3).length;
    const two = rows.filter(r => r.board_count >= 2).length;
    const top = rows[0];
    const loud = rows.reduce((a, b) => ((b.share || 0) > (a?.share || 0) ? b : a), null);
    // Short labels so the strip fits the header on one line; the long form and
    // the full repo name live in each tile's tooltip, so nothing is lost.
    for (const [k, v, n, full] of [
      ["repos", String(rows.length),
       `${d.board_sizes.daily}d·${d.board_sizes.weekly}w·${d.board_sizes.monthly}m`,
       "repos across all boards"],
      ["on 2+", String(two), `${three} on all 3`,
       "repos appearing on more than one horizon"],
      ["top", top ? (d.rank_by === "both" ? top.score_both
              : d.rank_by === "forks" ? (top.fork_ratio ?? 0)
              : d.rank_by === "surge" ? (top.share ?? 0)
              : top.star_component).toFixed(2) : "-",
       top ? (top.full_name.split("/")[1] || top.full_name) : "",
       top ? `highest by ${d.rank_by} - ${top.full_name}` : "highest"],
      ["★ both zones", String(computeGold(rows).both.size), "small, surging, forked",
     "repos inside both golden zones at once"],
    ["loudest", loud && loud.share ? (loud.share * 100).toFixed(0) + "%" : "-",
       loud ? (loud.full_name.split("/")[1] || loud.full_name) : "",
       loud ? `largest share of total stars gained - ${loud.full_name} `
              + `(${loud.share_board})` : "largest share gained"],
    ]) host.appendChild(metric(k, v, n, full));
    return;
  }
  const fresh = rows.filter(r => r.is_new).length;
  const durable = rows.filter(r => (r.hold || 0) >= 0.8).length;
  const loudest = rows.reduce((a, b) => ((b.share || 0) > (a?.share || 0) ? b : a), null);
  const allThree = rows.filter(r => r.board_count === 3).length;
  const twoPlus = rows.filter(r => r.board_count >= 2).length;
  const tiles = [
    ["repos", String(rows.length),
     d.readings ? `${d.readings} reading${d.readings === 1 ? "" : "s"}` : "-",
     "repos on this board"],
    ["on 2+", String(twoPlus), `${allThree} on all 3`,
     "also present on another horizon"],
    ["new", String(fresh),
     d.compare_label ? `vs ${d.compare_label}` : "vs previous",
     "entered since the comparison reading"],
    ["held", String(durable), `\u226580% of ${d.over_label}`,
     "repos holding a place in at least 80% of readings"],
    ["loudest", loudest ? pct(loudest.share).replace("+", "") : "-",
     loudest ? (loudest.full_name.split("/")[1] || loudest.full_name) : "",
     loudest ? `largest share of total stars gained - ${loudest.full_name}`
             : "largest share gained"],
  ];
  for (const [k, v, n, full] of tiles) host.appendChild(metric(k, v, n, full));
}

const ESSENTIAL_COLUMNS = new Set([
  "rank", "direction", "full_name", "stars", "period_stars", "share",
  "board_count", "score",
]);

function tColumns() {
  let cols = T_COLUMNS_COMBINED;   // one layout for every board
  const d = tState.data;
  // Until any repo has fork history, the dforks column is a row of dashes.
  if (d && d.fork_ready === 0) cols = cols.filter(c => c.key !== "fork_rel");
  // Simple mode: the seven columns that carry the answer.
  if (!tState.full) cols = cols.filter(c => ESSENTIAL_COLUMNS.has(c.key));
  return cols;
}

function tRenderHead() {
  const head = document.getElementById("t-head");
  head.replaceChildren();
  for (const c of tColumns()) {
    const th = el("th", c.cls || null);
    if (c.align === "left") th.style.textAlign = "left";
    if (c.title) th.title = c.title;
    th.appendChild(document.createTextNode(c.label));
    if (tState.sort === c.key)
      th.appendChild(el("span", "arrow", tState.dir < 0 ? " ▼" : " ▲"));
    if (c.sortable === false) th.style.cursor = "default";
    else th.onclick = () => {
      if (tState.sort === c.key) tState.dir *= -1;
      else { tState.sort = c.key; tState.dir = c.key === "rank" ? 1 : -1; }
      tRenderHead(); tRenderRows();
    };
    head.appendChild(th);
  }
}

function tRenderRows() {
  const d = tState.data;
  const body = document.getElementById("t-rows");
  const empty = document.getElementById("t-empty");
  body.replaceChildren();
  // A selected theme scopes the table to its members.
  let rows = (d.board || []).slice();
  if (tState.themeRepos) rows = rows.filter(r => tState.themeRepos.has(r.full_name));

  empty.hidden = rows.length > 0;
  if (!rows.length) {
    empty.replaceChildren(el("div", null,
      tState.theme ? `No repo on this board matches the theme “${tState.theme}”.`
        : (d.empty || "No readings stored for this board yet. Run: gittrack trending")));
    return;
  }

  const k = tState.sort, dir = tState.dir;
  rows.sort((a, b) => {
    if (k === "positions") return 0;
    if (k === "direction") {
      // Rank by how hard it is moving, not just the arrow: within "up", a 6.8x
      // belongs above a 1.2x. Unknowns sort last in either direction.
      const score = r => {
        const a2 = accelOf(r);
        if (a2) return Math.log2(a2.ratio);
        const t = r.trend;
        if (t && t.readings > 1) return (t.net || 0) * 0.01;
        return null;
      };
      const av = score(a), bv = score(b);
      if (av === null && bv === null) return 0;
      if (av === null) return 1;
      if (bv === null) return -1;
      return dir * (av - bv);
    }
    if (k === "full_name" || k === "language")
      return dir * String(a[k] || "").localeCompare(String(b[k] || ""));
    const av = a[k], bv = b[k];
    const an = av === null || av === undefined, bn = bv === null || bv === undefined;
    if (an && bn) return 0;
    if (an) return 1;
    if (bn) return -1;
    return dir * (av - bv);
  });

  const boardChips = r => {
    const chips = el("span", "boards");
    const labels = { daily: "D", weekly: "W", monthly: "M" };
    const parts = [];
    for (const b of (d.all_boards || ["daily", "weekly", "monthly"])) {
      const at = (r.also_on || {})[b];
      chips.appendChild(el("span",
        "chip" + (at ? " on" : "") + (b === d.since ? " here" : ""), labels[b]));
      parts.push(at ? `${b} #${at}` : `not on ${b}`);
    }
    chips.title = parts.join(" · ");
    return chips;
  };

  // One layout for every board. The per-board variant carried `move`,
  // `streak` and `hold` - all of which the direction glyph and the
  // rank-trend column now express - so two renderers only bought drift.
  const flash = tState.flash;
  // Departed repos are appended as ghosts so the diff is visible in one place.
  const ghosts = flash ? flash.left : [];
  const cols = tColumns();
  const cell = {
    rank: (r, gone) => el("td", "col-rank", gone ? "-" : r.rank),
    direction: (r, gone) => gone ? el("td", "col-dir") : dirCell(r),
    full_name: (r, gone, tr_) => {
      const td = el("td", "col-repo");
      const inBoth = tState.gold && tState.gold.both.has(r.full_name);
      if (inBoth && !gone) {
        tr_.className = (tr_.className ? tr_.className + " " : "") + "in-both";
        const star = el("span", "gold-star", "\u2605 ");
        star.title = "in both golden zones: small, surging, and being forked";
        star.setAttribute("role", "img");
        star.setAttribute("aria-label", "in both golden zones");
        td.appendChild(star);
      }
      td.appendChild(repoLink(r.full_name, r.url));
      if (tr_.className.includes("flash-")) {
        const tag = el("span", "flash-tag " + (gone ? "out" : "in"),
                       gone ? " dropped off" : " new");
        tag.title = gone ? "was on this board before the last fetch"
                         : "appeared in the last fetch";
        td.appendChild(tag);
      }
      return td;
    },
    language: r => { const td = el("td", "dim"); td.style.textAlign = "left";
                     td.textContent = r.language || "-"; return td; },
    stars: r => el("td", null, compact(r.stars)),
    period_stars: r => {
      const td = el("td", "up", compact(r.period_stars, true));
      td.title = Object.entries(r.period_by_board || {})
        .map(([b, v]) => `${b}: +${nf.format(v || 0)}`).join(" \u00b7 ");
      return td;
    },
    share: r => { const td = strengthCell(r.share);
                  if (r.share_board) td.title = "strongest on the " + r.share_board + " board";
                  return td; },
    trend: r => trendCell(r.trend),
    forks: r => el("td", r.forks == null ? "dim" : null, compact(r.forks)),
    fork_ratio: r => el("td", r.fork_ratio == null ? "dim" : null,
                        r.fork_ratio == null ? "-" : r.fork_ratio.toFixed(3)),
    fork_rel: r => el("td", r.fork_rel == null ? "dim" : "up",
                      r.fork_rel == null ? "-" : pct(r.fork_rel)),
    board_count: r => { const td = el("td"); td.appendChild(boardChips(r)); return td; },
    positions: r => el("td", "dim", (d.all_boards || []).map(b =>
      (r.also_on || {})[b] ? String((r.also_on || {})[b]) : "-").join(" / ")),
    score: r => {
      const td = el("td");
      const meter = el("div", "meter");
      const track = el("div", "track");
      const fill = el("div", "fill");
      const raw = d.rank_by === "both" ? r.score_both
                : d.rank_by === "forks" ? r.fork_ratio
                : d.rank_by === "surge" ? r.share
                : (r.star_component ?? r.score);
      const shown = Number.isFinite(raw) ? raw : 0;
      fill.style.width = Math.round(Math.min(1, shown) * 100) + "%";
      track.appendChild(fill);
      meter.appendChild(el("span", "num", shown.toFixed(2)));
      meter.appendChild(track);
      td.appendChild(meter);
      return td;
    },
  };

  for (const r of rows.concat(ghosts)) {
    const gone = flash && ghosts.includes(r);
    const tr_ = el("tr");
    if (gone) tr_.className = "flash-out";
    else if (flash && flash.entered.has(r.full_name)) tr_.className = "flash-in";
    tr_.dataset.repo = r.full_name;
    tr_.setAttribute("aria-selected", tState.selected === r.full_name ? "true" : "false");
    tr_.onclick = () => pickRepo(r.full_name);
    tr_.onpointerenter = () => setPeer(r.full_name, null);
    tr_.onpointerleave = () => setPeer(null, null);
    for (const c of cols) {
      const make = cell[c.key];
      tr_.appendChild(make ? make(r, gone, tr_) : el("td"));
    }
    body.appendChild(tr_);
  }
}

/* Rank over time. The y-axis is inverted because rank 1 is the top of the
   board - a line going UP means the repo is climbing, which is what a reader
   expects to see. */
function rankChart(host, { ts, rank, height = 168 }) {
  host.replaceChildren();
  const W = host.clientWidth || 560, H = height;
  const P = { t: 12, r: 12, b: 22, l: 40 };
  const iw = Math.max(10, W - P.l - P.r), ih = Math.max(10, H - P.t - P.b);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H,
                             role: "img", "aria-label": "Rank over time" });
  host.appendChild(svg);
  if (ts.length < 2) {
    host.appendChild(el("div", "chart-note",
      "only one reading so far - a rank line needs at least two"));
    return;
  }

  const x0 = ts[0], x1 = ts[ts.length - 1];
  const best = Math.min(...rank), worst = Math.max(...rank);
  const lo = Math.max(1, best - 1), hi = worst + 1;
  const X = t => P.l + (x1 === x0 ? iw / 2 : (t - x0) / (x1 - x0) * iw);
  const Y = v => P.t + (v - lo) / (hi - lo) * ih;   // inverted: rank 1 on top

  const ticks = [...new Set([lo, Math.round((lo + hi) / 2), hi])];
  for (const tk of ticks) {
    svg.appendChild(svgEl("line", { x1: P.l, x2: P.l + iw, y1: Y(tk), y2: Y(tk),
                                    stroke: cssVar("--grid"), "stroke-width": 1 }));
    const lb = svgEl("text", { x: P.l - 7, y: Y(tk) + 3.5, "text-anchor": "end",
                               fill: cssVar("--text-muted"), "font-size": 10.5 });
    lb.textContent = "#" + tk;
    svg.appendChild(lb);
  }
  for (const t of [x0, x1]) {
    const lb = svgEl("text", { x: X(t), y: H - 6,
                               "text-anchor": t === x0 ? "start" : "end",
                               fill: cssVar("--text-muted"), "font-size": 10.5 });
    lb.textContent = fdate(t);
    svg.appendChild(lb);
  }

  const c = cssVar("--series-1");
  const d = ts.map((t, i) => `${i ? "L" : "M"}${X(t).toFixed(1)},${Y(rank[i]).toFixed(1)}`).join("");
  svg.appendChild(svgEl("path", { d, fill: "none", stroke: c, "stroke-width": 2,
                                  "stroke-linejoin": "round", "stroke-linecap": "round" }));
  for (let i = 0; i < ts.length; i++) {
    svg.appendChild(svgEl("circle", { cx: X(ts[i]), cy: Y(rank[i]), r: 3.5, fill: c,
                                      stroke: cssVar("--surface-1"), "stroke-width": 2 }));
  }

  const cross = svgEl("line", { y1: P.t, y2: P.t + ih, stroke: cssVar("--axis"),
                                "stroke-width": 1, opacity: 0 });
  svg.appendChild(cross);
  const tip = makeTip(host);
  svg.addEventListener("pointermove", ev => {
    const r = svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) / r.width * W;
    const target = x0 + (x1 - x0) * Math.min(1, Math.max(0, (px - P.l) / iw));
    let i = 0, bestd = Infinity;
    for (let k = 0; k < ts.length; k++) {
      const dd = Math.abs(ts[k] - target);
      if (dd < bestd) { bestd = dd; i = k; }
    }
    cross.setAttribute("x1", X(ts[i])); cross.setAttribute("x2", X(ts[i]));
    cross.setAttribute("opacity", 1);
    tip.show(X(ts[i]) / W * (host.clientWidth || W), Y(rank[i]), fdatetime(ts[i]),
             [{ color: c, name: "rank", value: "#" + rank[i] }]);
  });
  svg.addEventListener("pointerleave", () => {
    cross.setAttribute("opacity", 0); tip.hide();
  });
}

/* One entry point for "the user picked this repo", wherever the click came from.
   Clicking the same repo again clears the selection, so a chart click is
   reversible without hunting for the close button. */
function pickRepo(name) {
  if (tState.selected === name) { clearPick(); return; }
  tSelect(name);
}

function clearPick() {
  tState.selected = null;
  document.getElementById("t-detail").hidden = true;
  tRenderRows();
  if (tState.digest) renderDigest();
  const box = document.getElementById("landscape");
  if (box && box._redraw) box._redraw();
}

/* Bring the selected row into view and flag it, so a click in a chart lands
   somewhere the reader can actually read the numbers. */
function revealRow(name) {
  const tr = document.querySelector(`#t-rows tr[data-repo="${CSS.escape(name)}"]`);
  if (!tr) return;
  const r = tr.getBoundingClientRect();
  if (r.top < 90 || r.bottom > window.innerHeight - 20)
    tr.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function tSelect(name) {
  tState.selected = name;
  const host = document.getElementById("t-detail");
  host.hidden = false;
  host.replaceChildren(el("div", "card", "loading…"));
  tRenderRows();
  if (tState.digest) renderDigest();
  // Repaint the charts so the pick is visible in all three, then find the row.
  const lbox = document.getElementById("landscape");
  if (lbox && lbox._redraw) lbox._redraw();
  revealRow(name);
  try {
    const qs = new URLSearchParams({ name, since: tState.since,
                                     language: tState.lang, days: "120" });
    const d = await fetchJSON("/api/trending/repo?" + qs);
    const row = (tState.data.board || []).find(r => r.full_name === name) || {};

    const card = el("div", "card");
    const head = el("div", "dhead");
    const h2 = el("h2");
    const a = el("a", null, d.full_name);
    a.href = d.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    h2.appendChild(a);
    head.appendChild(h2);
    head.appendChild(el("span", "spacer"));
    const close = el("button", "ghost", "✕ close");
    close.onclick = clearPick;
    head.appendChild(close);
    card.appendChild(head);
    if (d.description) card.appendChild(el("div", "sub", d.description));

    const grid = el("div", "dgrid");
    const stat = (k, v, n) => {
      const x = el("div", "dstat");
      x.appendChild(el("div", "k", k));
      x.appendChild(el("div", "v", v));
      if (n) x.appendChild(el("div", "k", n));
      grid.appendChild(x);
    };
    stat("current rank", "#" + (row.rank ?? "-"),
         row.best_rank ? "best #" + row.best_rank : "");
    stat("streak", String(row.streak ?? 0),
         row.streak_from ? "since " + fdate(row.streak_from) : "not on the board");
    stat("hold", row.hold != null ? Math.round(row.hold * 100) + "%" : "-",
         row.hold != null
           ? `${row.appearances ?? 0} of ${row.board_readings ?? tState.data.readings}`
             + ` readings` + (row.hold_board ? ` · ${row.hold_board}` : "")
           : "not enough readings yet");
    stat("this period", compact(row.period_stars, true),
         row.share != null ? (row.share * 100).toFixed(1) + "% of total" : "");
    stat("stars", compact(row.stars),
         row.stars_gained ? compact(row.stars_gained, true) + " in window" : "");
    const on = Object.entries(row.also_on || {})
      .filter(([, v]) => v).map(([b, v]) => `${b} #${v}`);
    stat("on boards", `${row.board_count || 0} of 3`,
         on.length ? on.join(" · ") : "not on any board right now");
    card.appendChild(grid);

    const chart = el("div");
    chart.appendChild(el("div", "chart-title", "Rank over time"));
    chart.appendChild(el("div", "chart-note",
      "Higher is better - the axis is inverted so #1 sits at the top."
      + (d.board ? `  Plotted on the ${d.board} board.` : "")));
    const plot = el("div", "plot");
    chart.appendChild(plot);
    card.appendChild(chart);

    const twin = el("details", "table-twin");
    twin.appendChild(el("summary", null, "Show the numbers"));
    const tbl = el("table");
    const hr = el("tr");
    for (const h of ["reading", "rank", "stars", "period stars"]) {
      const th = el("th", null, h); th.style.cursor = "default"; hr.appendChild(th);
    }
    tbl.appendChild(hr);
    for (let i = d.ts.length - 1; i >= 0; i--) {
      const r = el("tr");
      r.appendChild(el("td", null, fdatetime(d.ts[i])));
      r.appendChild(el("td", null, "#" + d.rank[i]));
      r.appendChild(el("td", null, d.stars[i] == null ? "-" : nf.format(d.stars[i])));
      r.appendChild(el("td", null, d.period_stars[i] == null ? "-"
                                    : nf.format(d.period_stars[i])));
      tbl.appendChild(r);
    }
    twin.appendChild(tbl);
    card.appendChild(twin);

    host.replaceChildren(card);
    const draw = () => rankChart(plot, { ts: d.ts, rank: d.rank });
    draw();
    host._redraw = draw;
  } catch (e) {
    host.replaceChildren(el("div", "card", "Could not load " + name + ": " + e.message));
  }
  host.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function setNote(box, text) {
  box.hidden = false;
  const body = box.querySelector(".body");
  body.textContent = text;
  const toggle = box.querySelector(".toggle");
  // Only offer "more" when there is actually more; a dead control is noise.
  requestAnimationFrame(() => {
    const clipped = body.scrollHeight > body.clientHeight + 2;
    toggle.hidden = !clipped && !box.classList.contains("open");
  });
}

function tRenderNote() {
  const d = tState.data, box = document.getElementById("t-note");
  const rows = d.board || [];
  if (!rows.length) { box.hidden = true; return; }
  if (d.combined) {
    const how = {
      stars: "Ranked by star position across daily, weekly and monthly. Each board "
           + "contributes a normalised position (rank 1 = 1.0, absent = 0), so the "
           + "top needs both a high placing and presence on more than one horizon.",
      forks: "Ranked by fork growth over the window, falling back to forks-per-star. "
           + "GitHub publishes no forks board - this is derived from our snapshots.",
      surge: "Ranked by surge - the share of each repo's entire star count that "
           + "arrived in this period. This is strength rather than size: a repo "
           + "gaining 10k stars on a 12k base is a different event from one "
           + "gaining 10k on a 240k base, and only this ordering separates them.",
      both: "Ranked 60/40 on star position and fork growth. Stars lead because that "
          + "is what the boards actually rank; forks adjust, because they are the "
          + "adoption signal rather than the attention one.",
    }[d.rank_by] || "";
    let msg = how + " No time comparison applies to a merged board.";
    // Same rule as the fork column: say why a column is empty rather than
    // leaving the reader to guess whether it is broken.
    const moved = rows.filter(r => r.trend && r.trend.net).length;
    const multi = rows.filter(r => r.trend && r.trend.readings > 1).length;
    if (multi === 0)
      msg += "  ⚠ Only one reading stored per board, so rank trend has nothing to "
           + "plot yet - it fills in from the second hourly sweep onward.";
    else if (moved === 0)
      msg += `  Rank trend is flat across all ${multi} readings so far: the boards `
           + "genuinely have not reordered yet.";
    if (d.fork_ready === 0)
      msg += "  ⚠ No repo has snapshot history spanning the window yet, so Δforks "
           + "is blank everywhere and fork ranking falls back to forks-per-star. "
           + "It fills in as gittrack run accumulates readings.";
    setNote(box, msg);
    return;
  }
  if (d.base_ts) {
    setNote(box, `Movement compared against the reading of ${fdatetime(d.base_ts)}`
      + (d.compare_label ? ` (~${d.compare_label} ago).` : " (the previous reading)."));
    return;
  }
  // No comparable baseline: say which, because the two cases need different action.
  setNote(box, d.readings > 1
    ? `No reading near ${d.compare_label} ago yet - only ${d.readings} readings stored, `
      + `all recent. Movement appears once readings span that period; `
      + `try a shorter window.`
    : `Only ${d.readings} reading stored. Movement appears from the second one onward.`);
}

function tRenderAll() {
  tState.gold = computeGold(tState.data.board || []);
  tRenderFilters();
  tRenderNote();
  renderLandscape();
  loadRelations();
  loadDigest();
  tRenderTiles();
  tRenderHead();
  tRenderRows();
}

function setView(v) {
  state.view = v;
  // Relocate the single switcher into the active view's filter row.
  const seg = document.getElementById("view-seg");
  const slot = document.getElementById(
    v === "trending" ? "view-slot-trending" : "view-slot-velocity");
  if (slot && seg.parentElement !== slot) slot.appendChild(seg);
  seg.hidden = false;
  requestAnimationFrame(syncSticky);
  // Same trick for the metric strip: one element per view, hoisted into the
  // header so the numbers sit beside the title rather than on their own band.
  // Both strips live in the slot permanently and we toggle which is shown.
  // replaceChildren() would evict the inactive one from the document, and
  // switching back would then find nothing to render into.
  const mslot = document.getElementById("metrics-slot");
  for (const id of ["t-tiles", "tiles"]) {
    const strip = document.getElementById(id);
    if (!strip) continue;
    if (strip.parentElement !== mslot) mslot.appendChild(strip);
    strip.hidden = (id === "t-tiles") !== (v === "trending");
  }
  document.getElementById("view-velocity").hidden = v !== "velocity";
  document.getElementById("view-trending").hidden = v !== "trending";
  [...document.getElementById("view-seg").children].forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.view === v)));
  if (v === "trending") {
    if (tState.data) tRenderAll(); else tLoad();
  } else {
    if (state.data) renderAll(); else load();
  }
}
document.getElementById("view-seg").onclick = e => {
  const b = e.target.closest("button");
  if (b) setView(b.dataset.view);
};
document.getElementById("t-note-toggle").onclick = () => {
  const box = document.getElementById("t-note");
  const open = box.classList.toggle("open");
  box.querySelector(".toggle").textContent = open ? "less" : "more";
};
document.getElementById("t-lang").onchange = e => {
  tState.lang = e.target.value; tLoad();
};

/* What changed across a fetch. Departures are kept as ghost rows: a repo that
   drops off the board is at least as interesting as one that arrives, and it
   cannot be seen at all if it simply vanishes on refresh. */
const FLASH_MS = 20000;

/* Which repos sit in each golden zone, and - the point of this - in both.
   Computed once per render so the charts and the table cannot disagree about
   membership, which is exactly the kind of thing that drifts when two places
   each work it out for themselves. */
function computeGold(rows) {
  const fr = rows.map(r => r.fork_ratio).filter(x => x != null).sort((a, b) => a - b);
  const med = fr.length ? fr[Math.floor(fr.length / 2)] : null;
  const size = new Set(), adopt = new Set(), both = new Set();
  for (const r of rows) {
    const surging = (r.share || 0) >= GOLD_SURGE;
    if (!surging) continue;
    if (r.stars >= GOLD_LO && r.stars <= GOLD_HI) size.add(r.full_name);
    if (med != null && (r.fork_ratio || 0) >= med) adopt.add(r.full_name);
  }
  for (const n of size) if (adopt.has(n)) both.add(n);
  return { size, adopt, both, med };
}

function markChanges(before, after) {
  const had = new Set(before.map(r => r.full_name));
  const has = new Set(after.map(r => r.full_name));
  const entered = new Set(after.filter(r => !had.has(r.full_name)).map(r => r.full_name));
  const left = before.filter(r => !has.has(r.full_name));
  clearTimeout(tState.flashTimer);
  if (!entered.size && !left.length) { tState.flash = null; return; }
  tState.flash = { entered, left };
  // The highlight is a transient cue, not a state: clear it so a board left open
  // does not keep claiming these are new an hour later.
  tState.flashTimer = setTimeout(() => {
    tState.flash = null;
    if (tState.data) tRenderRows();
  }, FLASH_MS);
}

/* The velocity view needs a week of snapshots before it has anything to say.
   Label its tab with how far along that is, so nobody clicks into an empty
   table wondering whether something is broken. */
async function labelVelocityTab() {
  const btn = document.querySelector('#view-seg button[data-view="velocity"]');
  if (!btn) return;
  try {
    const d = await fetchJSON("/api/overview?window=7");
    const s = d.stats;
    const ready = d.repos.filter(r => r.has_window).length;
    if (ready > 0) { btn.textContent = "velocity"; btn.title = `${ready} repos have a 7-day window`; return; }
    const have = s.first_ts ? (Date.now() / 1000 - s.first_ts) / 86400 : 0;
    const left = Math.max(0, 7 - have);
    btn.textContent = `velocity \u00b7 in ${left.toFixed(1)}d`;
    btn.title = `Needs 7 days of snapshots; ${have.toFixed(1)}d collected so far. `
      + "Trending works from day one - this is the part that needs history.";
  } catch (e) { /* the label is a nicety; never block on it */ }
}

/* ------------------------------------------------------------------ digest */
async function loadDigest() {
  const host = document.getElementById("digest");
  try {
    const qs = new URLSearchParams({ language: tState.lang,
                                     include_seen: tState.showSeen ? "1" : "0" });
    tState.digest = await fetchJSON("/api/digest?" + qs);
  } catch (e) {
    host.hidden = false;
    host.replaceChildren(el("div", "chart-note", "digest unavailable: " + e.message));
    return;
  }
  renderDigest();
}

function coverageChip(cov) {
  if (!cov || cov.expected == null) return null;
  const pct = Math.round(cov.pct * 100);
  const chip = el("span", "cov " + (pct >= 90 ? "ok" : pct >= 60 ? "warn" : "bad"),
                  `${pct}% of hourly sweeps landed`);
  chip.title = `${cov.actual} of ~${cov.expected} scheduled sweeps in the last week`
    + ` \u00b7 largest gap ${cov.largest_gap_h}h`
    + (cov.largest_gap_h >= 3 ? " \u00b7 the machine sleeps; see README" : "");
  return chip;
}

function renderDigest() {
  const host = document.getElementById("digest");
  const d = tState.digest;
  if (!d) return;
  host.hidden = false;
  host.replaceChildren();

  const head = el("div", "dh");
  const h2 = el("h2", null, d.items.length
    ? `${d.items.length} worth a look` : "nothing stands out right now");
  head.appendChild(h2);
  head.appendChild(el("span", "sub",
    `of ${d.considered} on the boards \u00b7 ${d.skipped_giants} giants`
    + (d.skipped_seen ? ` and ${d.skipped_seen} seen` : "") + " skipped"));
  const chip = coverageChip(d.coverage);
  if (chip) head.appendChild(chip);
  head.appendChild(el("span", "spacer"));
  const toggle = el("button", "ghost seen-btn",
                    tState.showSeen ? "hide seen" : "show seen");
  toggle.onclick = () => { tState.showSeen = !tState.showSeen; loadDigest(); };
  head.appendChild(toggle);
  host.appendChild(head);

  if (!d.items.length) return;
  const ol = el("ol");
  d.items.forEach((it, i) => {
    const li = el("li");
    li.setAttribute("aria-selected", tState.selected === it.full_name ? "true" : "false");
    li.onclick = () => pickRepo(it.full_name);
    li.appendChild(el("span", "num", String(i + 1)));

    const body = el("div");
    const line = el("div");
    line.appendChild(repoLink(it.full_name, it.url));
    line.appendChild(el("span", "meta",
      compact(it.stars) + "\u2605" + (it.language ? " \u00b7 " + it.language : "")));
    body.appendChild(line);
    const why = el("div", "why");
    for (const r of it.reasons) {
      const gold = /^(small, surging|small and|surging and)/.test(r);
      why.appendChild(el("span", gold ? "gold" : null, r));
    }
    body.appendChild(why);
    li.appendChild(body);

    const isSeen = d.seen && d.seen[it.full_name];
    const btn = el("button", "ghost seen-btn", isSeen ? "unsee" : "seen");
    btn.title = isSeen ? "put it back in the digest"
                       : "mark reviewed; the digest moves on to something new";
    btn.onclick = async ev => {
      ev.stopPropagation();
      const qs = new URLSearchParams({ name: it.full_name, undo: isSeen ? "1" : "0" });
      try { await fetch("/api/seen?" + qs, { method: "POST" }); } catch (e) { /* refetch shows truth */ }
      loadDigest();
    };
    li.appendChild(btn);
    ol.appendChild(li);
  });
  host.appendChild(ol);
}

/* ------------------------------------------------------------- fetch button */
let fetchPoll = null;

function fetchBar(kind, text, steps) {
  const bar = document.getElementById("fetchbar");
  bar.className = "fetchbar show" + (kind ? " " + kind : "");
  bar.replaceChildren();
  if (!kind) bar.appendChild(el("span", "spinner"));
  bar.appendChild(el("span", null, text));
  if (steps && steps.length)
    bar.appendChild(el("span", "steps", "· " + steps.join(" · ")));
  if (kind) {
    const x = el("button", "ghost", "✕");
    x.onclick = () => { bar.className = "fetchbar"; };
    bar.appendChild(el("span", "spacer"));
    bar.appendChild(x);
  }
}

function applyJob(j) {
  const btn = document.getElementById("fetch");
  if (j.running) {
    btn.disabled = true;
    btn.textContent = "⤓ Fetching…";
    fetchBar(null, j.kind === "snapshot"
      ? "Snapshotting tracked repos via the GitHub API…"
      : "Reading GitHub Trending…", j.steps);
    return false;
  }
  btn.disabled = false;
  btn.textContent = "⤓ Fetch now";
  if (j.error) {
    fetchBar("bad", "Fetch failed: " + j.error, j.steps);
  } else if (j.result) {
    const r = j.result;
    const msg = j.kind === "snapshot"
      ? `Snapshotted ${r.repos_seen} repos (${r.api_requests} API calls)`
      : `Stored ${r.rows} rows across ${r.boards} board(s)`
        + (r.repos_added ? `, +${r.repos_added} new repos tracked` : "");
    if (j.kind === "trending" && tState.flash) {
      const f = tState.flash;
      fetchBar("ok", msg + ` · ${f.entered.size} entered, ${f.left.length} dropped off`,
               j.steps);
      return true;
    }
    fetchBar("ok", msg, j.steps);
  }
  return true;
}

async function startFetch() {
  const kind = state.view === "trending" ? "trending" : "snapshot";
  const qs = new URLSearchParams({
    kind, since: tState.since, language: tState.lang,
  });
  document.getElementById("fetch").disabled = true;
  tState.armDiff = true;
  try {
    const r = await fetch("/api/fetch?" + qs, { method: "POST" });
    const j = await r.json();
    // 409 means a fetch is already in flight - adopt it rather than erroring.
    if (!r.ok && r.status !== 409 && !j.running) {
      fetchBar("bad", "Could not start: " + (j.error || r.statusText));
      document.getElementById("fetch").disabled = false;
      return;
    }
    applyJob(j);
    pollFetch();
  } catch (e) {
    fetchBar("bad", "Could not start: " + e.message);
    document.getElementById("fetch").disabled = false;
  }
}

function pollFetch() {
  clearInterval(fetchPoll);
  fetchPoll = setInterval(async () => {
    let j;
    try {
      j = await fetchJSON("/api/fetch/status");
    } catch (e) {
      return;   // transient; keep polling
    }
    if (applyJob(j)) {
      clearInterval(fetchPoll);
      fetchPoll = null;
      // The store just changed underneath us - re-read the active view.
      if (state.view === "trending") tLoad(); else load();
    }
  }, 1000);
}

document.getElementById("fetch").onclick = startFetch;
// A fetch started elsewhere (or before a reload) should still be reflected here.
fetchJSON("/api/fetch/status").then(j => { if (j.running) { applyJob(j); pollFetch(); } })
  .catch(() => {});

/* Keep the sticky table header parked directly below the sticky filter bar, and
   flag the bar once it is actually pinned so the shadow only appears then. */
function syncSticky() {
  const bar = document.querySelector(
    (state.view === "trending" ? "#view-trending" : "#view-velocity") + " .filters");
  if (!bar) return;
  const h = Math.round(bar.getBoundingClientRect().height);
  document.documentElement.style.setProperty("--sticky-top", h + "px");
  bar.classList.toggle("stuck", bar.getBoundingClientRect().top <= 1);
}
window.addEventListener("scroll", syncSticky, { passive: true });
window.addEventListener("resize", syncSticky);

/* ------------------------------------------------------------------ wiring */
document.getElementById("band").onchange = e => { state.band = e.target.value; renderRows(); };
document.getElementById("lang").onchange = e => { state.lang = e.target.value; renderRows(); };
document.getElementById("only-ready").onchange = e => {
  state.onlyReady = e.target.checked; renderRows();
};
let qt;
document.getElementById("search").oninput = e => {
  clearTimeout(qt);
  qt = setTimeout(() => { state.q = e.target.value.trim(); renderRows(); }, 120);
};
document.getElementById("refresh").onclick = () =>
  (state.view === "trending" ? tLoad() : load());
document.getElementById("theme").onclick = () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur === "dark" ? "light" : cur === "light" ? "" : "dark";
  if (next) document.documentElement.setAttribute("data-theme", next);
  else document.documentElement.removeAttribute("data-theme");
  try { localStorage.setItem("gittrack-theme", next); } catch (e) { /* private mode */ }
  if (state.data) renderRows();
  if (tState.data) tRenderRows();
  for (const id of ["detail", "t-detail", "landscape"]) {
    const host = document.getElementById(id);
    if (host && !host.hidden && host._redraw) host._redraw();
  }
};
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    document.getElementById("detail").hidden = true;
    document.getElementById("t-detail").hidden = true;
    state.selected = null;
    if (state.data) renderRows();
    if (tState.data) clearPick();
  }
});
let rt;
window.addEventListener("resize", () => {
  clearTimeout(rt);
  rt = setTimeout(() => {
    for (const id of ["detail", "t-detail", "landscape"]) {
      const host = document.getElementById(id);
      if (host && !host.hidden && host._redraw) host._redraw();
    }
  }, 140);
});
try {
  const t = localStorage.getItem("gittrack-theme");
  if (t) document.documentElement.setAttribute("data-theme", t);
} catch (e) { /* storage can throw in private mode */ }

setView("trending");   // trends are the point of this page; velocity is the drill-down
labelVelocityTab();
setInterval(() => (state.view === "trending" ? tLoad() : load()), 5 * 60 * 1000);

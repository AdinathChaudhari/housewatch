// housewatch dashboard client. Vanilla JS, no build step, no CDN — this
// page has to render with the WAN down (that's the whole premise of the
// project it's reporting on). Every renderer below treats `null` as its
// own state (never coerces it to 0) per the project's central honesty
// invariant: unknown must never look like zero.

(() => {
  "use strict";

  const REFRESH_MS = Math.max(
    5000,
    Math.min(30000, (window.HW_POLL_INTERVAL_S || 10) * 1000)
  );

  let daysRangeDays = 30;
  let rateRangeMinutes = 1440; // 24h default
  let lastDays = [];
  let lastRate = [];

  // ---- formatting -----------------------------------------------------

  function fmtBytes(n) {
    if (n === null || n === undefined) return "unknown";
    n = Number(n);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = Math.abs(n);
    while (v >= 1000 && i < units.length - 1) {
      v /= 1000;
      i++;
    }
    const sign = n < 0 ? "-" : "";
    return `${sign}${v.toFixed(v < 10 && i > 0 ? 2 : 1)} ${units[i]}`;
  }

  function fmtBps(n) {
    if (n === null || n === undefined) return "—";
    const units = ["bps", "Kbps", "Mbps", "Gbps"];
    let i = 0;
    let v = Math.abs(n);
    while (v >= 1000 && i < units.length - 1) {
      v /= 1000;
      i++;
    }
    return `${v.toFixed(1)} ${units[i]}`;
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return "unknown";
    seconds = Math.max(0, Math.round(seconds));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
    if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
    return `${s}s`;
  }

  function fmtTs(ts) {
    if (ts === null || ts === undefined) return "—";
    return new Date(ts * 1000).toLocaleString();
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  async function getJSON(url) {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
    return res.json();
  }

  // ---- health -----------------------------------------------------------

  async function refreshHealth() {
    const dot = document.getElementById("hw-health-dot");
    const label = document.getElementById("hw-health-label");
    try {
      const h = await getJSON("/healthz");
      if (h.ok) {
        dot.className = "hw-dot ok";
        label.textContent = `collector healthy — last sample ${fmtDuration(h.last_sample_age_s)} ago`;
      } else {
        dot.className = "hw-dot bad";
        label.textContent = h.reason
          ? `collector issue — ${h.reason}`
          : "collector issue";
      }
    } catch (err) {
      dot.className = "hw-dot bad";
      label.textContent = "dashboard cannot reach its own API";
    }
  }

  // ---- summary: cycle + today/yesterday ---------------------------------

  function renderCycle(cycle, cap) {
    const el = document.getElementById("hw-cycle-body");
    if (!cycle) {
      el.innerHTML = `<div class="hw-empty">No usage recorded yet this cycle.</div>`;
      return;
    }
    const capBytes = cap && cap.cap_bytes;
    const used = cycle.used_bytes;
    const unknownPrefix = cycle.unknown_s > 60 ? "≥ " : "";
    let barHtml = "";
    if (capBytes) {
      const pct = Math.max(0, Math.min(100, (used / capBytes) * 100));
      const projPct = Math.max(0, Math.min(140, (cycle.projected_bytes / capBytes) * 100));
      const over = cycle.over_cap_projected === true;
      barHtml = `
        <div class="hw-bar-track">
          <div class="hw-bar-fill${over ? " over" : ""}" style="width:${pct}%"></div>
        </div>
        <div class="hw-bar-caption">
          <span>${unknownPrefix}${fmtBytes(used)} used</span>
          <span>${fmtBytes(capBytes)} cap</span>
        </div>
        ${over ? `<div class="hw-bar-caption" style="color:var(--bad);margin-top:4px;">projected ${fmtBytes(cycle.projected_bytes)} (estimate) exceeds cap</div>` : `<div class="hw-bar-caption" style="margin-top:4px;">projected ${fmtBytes(cycle.projected_bytes)} (estimate), ${projPct.toFixed(0)}% of cap</div>`}
      `;
    } else {
      barHtml = `
        <div class="hw-empty">No monthly cap configured — usage is still tracked. Set <code>monthly_cap_gb</code> in the config to see a cap bar.</div>
      `;
    }
    const daysLeft = Math.max(0, cycle.days_total - cycle.days_elapsed);
    el.innerHTML = `
      <div class="hw-row">
        <div class="hw-stat">
          <div class="hw-stat-label">used this cycle</div>
          <div class="hw-stat-value">${unknownPrefix}${fmtBytes(used)}</div>
          <div class="hw-stat-sub">${cycle.cycle_start} → ${cycle.cycle_end}</div>
        </div>
        <div class="hw-stat">
          <div class="hw-stat-label">days remaining</div>
          <div class="hw-stat-value">${daysLeft.toFixed(1)}</div>
          <div class="hw-stat-sub">${cycle.days_elapsed.toFixed(1)} / ${cycle.days_total.toFixed(1)} elapsed</div>
        </div>
        <div class="hw-stat">
          <div class="hw-stat-label">projected total (estimate)</div>
          <div class="hw-stat-value">${fmtBytes(cycle.projected_bytes)}</div>
          <div class="hw-stat-sub">unknown time: ${fmtDuration(cycle.unknown_s)}</div>
        </div>
      </div>
      ${barHtml}
    `;
  }

  function dayCard(label, du) {
    if (!du) {
      return `<div class="hw-stat"><div class="hw-stat-label">${label}</div><div class="hw-empty">no data</div></div>`;
    }
    const unknownS = du.gap_s + du.uncovered_s;
    const prefix = unknownS > 60 ? "≥ " : "";
    // "nothing was measured", not "a whole calendar day was unmeasured".
    // The old test required a FULL calendar day of unknown time, which
    // today can never reach, so today-with-no-data rendered a number
    // instead of "no data" -- and a number reads as "you used nothing"
    // rather than "we have no idea".
    // ok_s + recovered_s + suspect_s is the measured portion of the day;
    // when that is zero and unknown time is non-trivial, nothing is known.
    const measuredS = du.ok_s + du.recovered_s + du.suspect_s;
    const allUnknown =
      unknownS > 60 && measuredS <= 0 && du.down_bytes === 0 && du.up_bytes === 0;
    const downLine = allUnknown
      ? `<div class="hw-stat-value">no data</div>`
      : `<div class="hw-stat-value">${prefix}${fmtBytes(du.down_bytes)} <span style="font-size:14px;color:var(--text-dim);font-weight:500;">↓</span></div>`;
    const upLine = allUnknown
      ? ""
      : `<div class="hw-stat-sub">${prefix}${fmtBytes(du.up_bytes)} ↑ upload</div>`;
    const suspectLine =
      du.suspect_down || du.suspect_up
        ? `<div class="hw-stat-sub" style="color:var(--suspect);">includes ${fmtBytes(du.suspect_down)} / ${fmtBytes(du.suspect_up)} suspect</div>`
        : "";
    return `
      <div class="hw-stat">
        <div class="hw-stat-label">${label}</div>
        ${downLine}
        ${upLine}
        ${suspectLine}
        <div class="hw-stat-sub">no data: ${fmtDuration(unknownS)}</div>
      </div>
    `;
  }

  function renderTodayYesterday(today, yesterday) {
    const el = document.getElementById("hw-today-body");
    el.innerHTML = `
      <div class="hw-row">
        ${dayCard("today", today)}
        ${dayCard("yesterday", yesterday)}
      </div>
    `;
  }

  async function refreshSummary() {
    const cycleBody = document.getElementById("hw-cycle-body");
    const todayBody = document.getElementById("hw-today-body");
    try {
      const s = await getJSON("/api/summary");
      if (!s.has_data) {
        const msg = `<div class="hw-empty">${s.message || "No data yet."}</div>`;
        cycleBody.innerHTML = msg;
        todayBody.innerHTML = msg;
        return;
      }
      renderCycle(s.cycle, s.cap);
      renderTodayYesterday(s.today, s.yesterday);
    } catch (err) {
      cycleBody.innerHTML = `<div class="hw-empty">Could not load summary: ${err.message}</div>`;
      todayBody.innerHTML = "";
    }
  }

  // ---- days bar chart -----------------------------------------------------

  function setupHiDPICanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width));
    const h = Math.max(1, Math.round(rect.height || 160));
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  function drawDaysChart() {
    const canvas = document.getElementById("hw-days-canvas");
    if (!canvas) return;
    const { ctx, w, h } = setupHiDPICanvas(canvas);
    ctx.clearRect(0, 0, w, h);

    const days = lastDays;
    if (!days.length) {
      ctx.fillStyle = cssVar("--text-dim");
      ctx.font = "13px -apple-system, sans-serif";
      ctx.fillText("no data yet", 8, h / 2);
      return;
    }

    const padLeft = 4, padRight = 4, padTop = 8, padBottom = 20;
    const plotW = w - padLeft - padRight;
    const plotH = h - padTop - padBottom;
    const n = days.length;
    const gap = 4;
    const barW = Math.max(2, (plotW - gap * (n - 1)) / n);

    const maxTotal = Math.max(
      1,
      ...days.map((d) => (d.down_bytes || 0) + (d.up_bytes || 0))
    );

    const downColor = cssVar("--accent");
    const upColor = cssVar("--accent-dim");
    const suspectColor = cssVar("--suspect");
    const gapColor = cssVar("--gap");

    days.forEach((d, i) => {
      const x = padLeft + i * (barW + gap);
      const down = d.down_bytes || 0;
      const up = d.up_bytes || 0;
      const total = down + up;
      const totalH = (total / maxTotal) * plotH;
      const downH = total > 0 ? (down / total) * totalH : 0;
      const upH = totalH - downH;
      let y = padTop + (plotH - totalH);

      // upload segment (top), then download segment (bottom) — download
      // is usually the dominant number, so it anchors the baseline.
      if (upH > 0) {
        ctx.fillStyle = upColor;
        ctx.fillRect(x, y, barW, upH);
        if (d.suspect_up) {
          const susH = (Math.min(d.suspect_up, up) / (up || 1)) * upH;
          ctx.fillStyle = suspectColor;
          ctx.fillRect(x, y, barW, susH);
        }
        y += upH;
      }
      if (downH > 0) {
        ctx.fillStyle = downColor;
        ctx.fillRect(x, y, barW, downH);
        if (d.suspect_down) {
          const susH = (Math.min(d.suspect_down, down) / (down || 1)) * downH;
          ctx.fillStyle = suspectColor;
          ctx.fillRect(x, y + downH - susH, barW, susH);
        }
      }

      // unknown-time strip along the bottom axis: width proportional to bar,
      // opacity/height proportional to how much of the day is unknown. This
      // is the "hatched grey segment" honesty requirement — unknown time is
      // a first-class visual element here, not a footnote.
      const dayLenS = 86400;
      const unknownS = (d.gap_s || 0) + (d.uncovered_s || 0);
      const unknownFrac = Math.max(0, Math.min(1, unknownS / dayLenS));
      if (unknownFrac > 0.01) {
        const stripH = Math.max(3, unknownFrac * 10);
        ctx.fillStyle = gapColor;
        ctx.globalAlpha = 0.55;
        ctx.fillRect(x, padTop + plotH - stripH, barW, stripH);
        ctx.globalAlpha = 1;
      }

      // day-of-month label, sparsely, so labels don't collide at n=30
      const showLabel = n <= 10 || i % Math.ceil(n / 10) === 0 || i === n - 1;
      if (showLabel) {
        ctx.fillStyle = cssVar("--text-dim");
        ctx.font = "10px -apple-system, sans-serif";
        ctx.textAlign = "center";
        const dayNum = d.day ? d.day.slice(-2) : "";
        ctx.fillText(dayNum, x + barW / 2, h - 6);
      }
    });
  }

  async function refreshDays() {
    try {
      lastDays = await getJSON(`/api/days?n=${daysRangeDays}`);
    } catch (err) {
      lastDays = [];
    }
    drawDaysChart();
  }

  // ---- rate line chart ----------------------------------------------------

  function drawRateChart() {
    const canvas = document.getElementById("hw-rate-canvas");
    if (!canvas) return;
    const { ctx, w, h } = setupHiDPICanvas(canvas);
    ctx.clearRect(0, 0, w, h);

    const points = lastRate;
    if (!points.length) {
      ctx.fillStyle = cssVar("--text-dim");
      ctx.font = "13px -apple-system, sans-serif";
      ctx.fillText("no data in this window", 8, h / 2);
      return;
    }

    const padLeft = 4, padRight = 4, padTop = 8, padBottom = 8;
    const plotW = w - padLeft - padRight;
    const plotH = h - padTop - padBottom;

    const tsMin = points[0].ts;
    const tsMax = points[points.length - 1].ts;
    const tsSpan = Math.max(1, tsMax - tsMin);

    const maxBps = Math.max(
      1,
      ...points.map((p) => Math.max(p.down_bps || 0, p.up_bps || 0))
    );

    // Unknown-time background: shade any x-range where both bps are null.
    const gapColor = cssVar("--gap");
    ctx.globalAlpha = 0.25;
    ctx.fillStyle = gapColor;
    let runStart = null;
    for (let i = 0; i < points.length; i++) {
      const isGap = points[i].down_bps === null && points[i].up_bps === null;
      const x = padLeft + ((points[i].ts - tsMin) / tsSpan) * plotW;
      if (isGap && runStart === null) runStart = x;
      if (!isGap && runStart !== null) {
        ctx.fillRect(runStart, padTop, Math.max(1, x - runStart), plotH);
        runStart = null;
      }
    }
    if (runStart !== null) {
      ctx.fillRect(runStart, padTop, Math.max(1, padLeft + plotW - runStart), plotH);
    }
    ctx.globalAlpha = 1;

    function drawSeries(key, color) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      let drawing = false;
      for (const p of points) {
        const v = p[key];
        const x = padLeft + ((p.ts - tsMin) / tsSpan) * plotW;
        if (v === null || v === undefined) {
          drawing = false; // a hole — never draw through a None sample
          continue;
        }
        const y = padTop + plotH - (v / maxBps) * plotH;
        if (!drawing) {
          ctx.moveTo(x, y);
          drawing = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
    }

    drawSeries("down_bps", cssVar("--accent"));
    drawSeries("up_bps", cssVar("--accent-dim"));
  }

  async function refreshRate() {
    try {
      lastRate = await getJSON(`/api/rate?minutes=${rateRangeMinutes}`);
    } catch (err) {
      lastRate = [];
    }
    drawRateChart();
  }

  // ---- unknown-windows table ------------------------------------------

  async function refreshUnknown() {
    const el = document.getElementById("hw-unknown-body");
    try {
      const rows = await getJSON("/api/unknown?days=30");
      if (!rows.length) {
        el.innerHTML = `<div class="hw-empty">No gap, suspect, reset, or anomaly windows in the last 30 days.</div>`;
        return;
      }
      const trs = rows
        .slice(0, 50)
        .map(
          (r) => `
        <tr>
          <td>${fmtTs(r.start_ts)}</td>
          <td>${fmtDuration(r.duration_s)}</td>
          <td><span class="hw-badge ${r.kind}">${r.kind}</span></td>
          <td>${r.down_bytes === null ? "UNKNOWN" : fmtBytes(r.down_bytes)}</td>
          <td>${r.up_bytes === null ? "UNKNOWN" : fmtBytes(r.up_bytes)}</td>
          <td style="color:var(--text-dim);">${r.note || ""}</td>
        </tr>`
        )
        .join("");
      el.innerHTML = `
        <div class="hw-scroll-x">
        <table class="hw-table">
          <thead><tr><th>started</th><th>duration</th><th>kind</th><th>down</th><th>up</th><th>note</th></tr></thead>
          <tbody>${trs}</tbody>
        </table>
        </div>
      `;
    } catch (err) {
      el.innerHTML = `<div class="hw-empty">Could not load: ${err.message}</div>`;
    }
  }

  // ---- controls -----------------------------------------------------------

  function wireControls() {
    document.querySelectorAll("[data-days-range]").forEach((btn) => {
      btn.addEventListener("click", () => {
        daysRangeDays = parseInt(btn.getAttribute("data-days-range"), 10);
        document
          .querySelectorAll("[data-days-range]")
          .forEach((b) => b.classList.toggle("active", b === btn));
        refreshDays();
      });
    });
    document.querySelectorAll("[data-rate-range]").forEach((btn) => {
      btn.addEventListener("click", () => {
        rateRangeMinutes = parseInt(btn.getAttribute("data-rate-range"), 10);
        document
          .querySelectorAll("[data-rate-range]")
          .forEach((b) => b.classList.toggle("active", b === btn));
        refreshRate();
      });
    });
    window.addEventListener("resize", () => {
      drawDaysChart();
      drawRateChart();
    });
  }

  function refreshAll() {
    refreshHealth();
    refreshSummary();
    refreshDays();
    refreshRate();
    refreshUnknown();
  }

  document.addEventListener("DOMContentLoaded", () => {
    wireControls();
    refreshAll();
    setInterval(refreshAll, REFRESH_MS);
  });
})();

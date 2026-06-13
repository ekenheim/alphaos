// Overview / RISK page — risk strip + NAV-index and drawdown charts.
(function () {
  const A = window.alphaos;
  const $ = (id) => document.getElementById(id);
  let journalByDate = {};

  function setColored(el, value, opts = {}) {
    if (!el) return;
    el.classList.remove("pos", "neg", "warn");
    if (value == null || isNaN(value)) return;
    const eps = opts.eps || 0;
    if (value > eps) el.classList.add(opts.posClass || "pos");
    else if (value < -eps) el.classList.add(opts.negClass || "neg");
  }

  function renderTiles(risk) {
    const cfg = {
      acct: risk.account_label || "Avanza ISK",
      ccy: risk.base_currency || "SEK",
    };
    $("account-label").textContent = `${cfg.acct} · ${cfg.ccy}`;

    // Money-terms P&L headline — the intuitive "am I up or down vs what I paid".
    const pnl = risk.pnl || {};
    const pnlEl = $("t-pnl");
    if (pnl.unrealized_pnl != null) {
      pnlEl.textContent = A.fmtSEKSigned(pnl.unrealized_pnl);
      setColored(pnlEl, pnl.unrealized_pnl);
      const pct = pnl.return_pct != null ? A.fmtPct(pnl.return_pct, 2) : "—";
      const atCost = pnl.at_cost ? ` · ${pnl.at_cost} at cost` : "";
      $("t-pnl-foot").textContent = `${pct} vs cost${atCost}`;
    } else {
      pnlEl.textContent = "—";
      pnlEl.classList.remove("pos", "neg");
      $("t-pnl-foot").textContent = "no holdings";
    }

    // NAV index / TWR / drawdown — gated when the time-weighted index can't be
    // trusted (incomplete contributions or too little real history), so it never
    // shows a misleading number. The reason is surfaced in the foot.
    const th = risk.thresholds || {};
    const twr = $("t-twr");
    const dd = $("t-dd");
    if (risk.nav_index_reliable === false) {
      $("t-nav").textContent = "—";
      $("t-nav-foot").textContent = risk.nav_index_note || "insufficient data";
      twr.textContent = "—";
      twr.classList.remove("pos", "neg");
      dd.textContent = "—";
      dd.classList.remove("pos", "neg");
      $("t-dd-foot").textContent = "needs complete contributions";
    } else {
      $("t-nav").textContent = A.fmtNum(risk.nav_index, 3);
      $("t-nav-foot").textContent = "base 1.000";
      twr.textContent = A.fmtPct(risk.twr_period, 2);
      setColored(twr, risk.twr_period);
      dd.textContent = A.fmtPct(risk.drawdown, 1);
      setColored(dd, risk.drawdown);
      $("t-dd-foot").textContent = risk.headroom_to_half != null
        ? `headroom to −35%: ${A.fmtPct(Math.abs(Math.min(risk.headroom_to_half, 0)), 1)}`
        : "off NAV-index peak";
    }

    const lev = $("t-lev");
    lev.textContent = risk.effective_leverage != null ? A.fmtNum(risk.effective_leverage, 2) + "×" : "—";
    $("t-lev-foot").textContent = "target " + (risk.target_leverage != null ? A.fmtNum(risk.target_leverage, 2) + "×" : "—") +
      (th.delever_floor_leverage != null ? ` · de-lever floor ${A.fmtNum(th.delever_floor_leverage, 2)}×` : "");
    // Warn when running hotter than the glide-path target.
    if (risk.effective_leverage != null && risk.target_leverage != null) {
      lev.classList.toggle("warn", risk.effective_leverage > risk.target_leverage + 0.02);
    }

    const belan = $("t-belan");
    belan.textContent = A.fmtPct(risk.belaningsgrad, 1);
    const cliff = th.belaningsgrad_cliff;
    const head = risk.belaningsgrad_headroom;
    $("t-belan-foot").textContent = `cliff ${A.fmtPct(cliff, 0)}` +
      (head != null ? ` · headroom ${A.fmtPct(head, 1)}` : "");
    if (head != null) belan.classList.toggle("warn", head < 0.05);

    $("t-equity").textContent = A.fmtSEK(risk.equity);
    $("t-loan").textContent = A.fmtSEK(risk.loan_balance);
    $("t-gross-foot").textContent = "gross " + A.fmtSEK(risk.gross_asset_value);

    // Growth since inception = NAV index (baselined at 1.0) expressed as a percent.
    const growth = risk.nav_index != null ? risk.nav_index - 1 : null;
    $("t-growth").textContent = growth != null ? A.fmtPct(growth, 1) : "—";
    if (growth != null) $("t-growth").classList.toggle("neg", growth < 0);

    // CAGR since inception (annualized TWR), shown only when the index is reliable.
    $("t-cagr").textContent = risk.cagr_since_inception != null
      ? A.fmtPct(risk.cagr_since_inception, 1) : "—";
    $("t-cagr-foot").textContent = risk.inception_date
      ? `since ${risk.inception_date}` : "annualized TWR";
  }

  function renderStatus(risk) {
    const status = risk.delever_status || "normal";
    const chip = $("status-chip");
    chip.textContent = status.toUpperCase();
    chip.className = "pill status-" + status;
    $("status-asof").textContent = risk.as_of ? `as of ${risk.as_of}` : "no NAV snapshots yet";
    $("status-action").textContent = risk.action || "—";
    const banner = $("status-banner");
    banner.className = "card status-banner-" + status;
  }

  // Account value vs cost basis (sleeve book). The gap between the lines is your
  // unrealized P&L — deposits/buys raise BOTH lines together, so only market
  // moves change the gap. `j` is the daily journal (date / value / cost_basis).
  function valueChart(j) {
    const x = j.map((r) => r.date);
    const last = j.length ? j[j.length - 1] : null;
    if (last) {
      $("nav-last").textContent = "Value " + A.fmtSEK(last.value);
      $("nav-peak").textContent = "P&L " + A.fmtSEKSigned(last.pnl);
    }
    const traces = [{
      x, y: j.map((r) => r.value), type: "scatter", mode: "lines", name: "value",
      line: { color: "#4dd0e1", width: 2 },
      hovertemplate: "%{x}<br>value %{y:,.0f} kr<extra></extra>",
    }, {
      x, y: j.map((r) => r.cost_basis), type: "scatter", mode: "lines", name: "cost basis",
      line: { color: "#6e7681", width: 1, dash: "dot" },
      hovertemplate: "%{x}<br>cost %{y:,.0f} kr<extra></extra>",
    }];
    Plotly.newPlot("nav-chart", traces, A.plotlyLayout({
      yaxis: { gridcolor: "#1f2733", zerolinecolor: "#1f2733", title: "SEK" },
    }), A.plotlyConfig);
  }

  // Drawdown off the running peak of account value (no contribution data needed).
  function drawdownChart(j) {
    const x = j.map((r) => r.date);
    let peak = -Infinity;
    const y = j.map((r) => {
      if (r.value > peak) peak = r.value;
      return peak > 0 ? (r.value / peak - 1) * 100 : 0;
    });
    const minDD = Math.min(0, ...y);
    $("dd-max").textContent = "max " + minDD.toFixed(1) + "%";
    const trace = {
      x, y, type: "scatter", mode: "lines", name: "drawdown",
      fill: "tozeroy", line: { color: "#f87171", width: 2 },
      fillcolor: "rgba(248,113,113,0.15)",
      hovertemplate: "%{x}<br>%{y:.1f}%<extra></extra>",
    };
    Plotly.newPlot("dd-chart", [trace], A.plotlyLayout({
      yaxis: { gridcolor: "#1f2733", zerolinecolor: "#1f2733", title: "drawdown %",
        range: [Math.min(minDD - 2, -5), 2], ticksuffix: "%" },
    }), A.plotlyConfig);
  }

  // --- Money calendar (GitHub-style daily-P&L heatmap) ---

  function hmColor(dp, maxAbs) {
    if (!dp || !maxAbs) return "#1b1f24";
    const a = 0.18 + 0.82 * Math.min(1, Math.abs(dp) / maxAbs);
    return dp > 0 ? `rgba(63,185,80,${a})` : `rgba(248,81,73,${a})`;
  }

  function renderHeatmap(j) {
    const el = $("cal-heatmap");
    journalByDate = {};
    let maxAbs = 0, total = 0;
    j.forEach((r) => { journalByDate[r.date] = r; maxAbs = Math.max(maxAbs, Math.abs(r.day_pnl)); total += r.day_pnl; });
    // Monday-align the first day (UTC noon avoids any timezone date-shift).
    const last = new Date(j[j.length - 1].date + "T12:00:00Z");
    const start = new Date(j[0].date + "T12:00:00Z");
    start.setUTCDate(start.getUTCDate() - ((start.getUTCDay() + 6) % 7));
    const grid = document.createElement("div");
    grid.className = "heatmap";
    for (let d = new Date(start); d <= last; d.setUTCDate(d.getUTCDate() + 1)) {
      const iso = d.toISOString().slice(0, 10);
      const r = journalByDate[iso];
      const cell = document.createElement("div");
      cell.className = "hm-cell";
      if (r) {
        cell.style.background = hmColor(r.day_pnl, maxAbs);
        cell.title = `${iso}: ${A.fmtSEKSigned(r.day_pnl)} · value ${A.fmtSEK(r.value)}`;
        cell.dataset.date = iso;
      } else {
        cell.title = iso;
      }
      grid.appendChild(cell);
    }
    el.innerHTML = "";
    el.appendChild(grid);
    grid.addEventListener("click", (e) => {
      const c = e.target.closest(".hm-cell");
      if (c && c.dataset.date) showHoldings(c.dataset.date);
    });
    $("cal-sum").textContent = "net " + A.fmtSEKSigned(total);
  }

  // Clicking a calendar day shows what was held that day, with its valuation.
  async function showHoldings(date) {
    const box = $("cal-holdings");
    const r = journalByDate[date];
    const cls = r && r.day_pnl >= 0 ? "feed-pos" : "feed-neg";
    const head = `<div class="cal-h-head">${date}` +
      (r ? ` · <span class="${cls}">${A.fmtSEKSigned(r.day_pnl)}</span> · value ${A.fmtSEK(r.value)}` +
        (r.event ? ` · <span class="feed-ev">${r.event}</span>` : "") : "") + "</div>";
    box.innerHTML = head + '<div class="muted" style="padding:6px 8px">loading…</div>';
    try {
      const hs = (await A.getJSON(`/api/nav/holdings-on?date=${date}`)).holdings || [];
      if (!hs.length) { box.innerHTML = head + '<div class="muted" style="padding:6px 8px">nothing held</div>'; return; }
      box.innerHTML = head + "<table><thead><tr><th>Holding</th><th>Qty</th><th>Price</th>" +
        "<th>Value</th><th>P&amp;L</th></tr></thead><tbody>" +
        hs.map((h) => `<tr><td>${h.symbol || h.isin}${h.priced ? "" : " <span class='muted'>(cost)</span>"}</td>` +
          `<td>${A.fmtNum(h.quantity, 2)}</td><td>${A.fmtNum(h.price, 2)}</td>` +
          `<td>${A.fmtSEK(h.value)}</td>` +
          `<td class="${h.pnl >= 0 ? "feed-pos" : "feed-neg"}">${A.fmtSEKSigned(h.pnl)}</td></tr>`).join("") +
        "</tbody></table>";
    } catch (e) {
      box.innerHTML = head + `<div class="feed-neg" style="padding:6px 8px">${e.message}</div>`;
    }
  }

  async function load() {
    A.dbChip($("db-chip"));
    try {
      const risk = (await A.getJSON("/api/nav")).risk || {};
      renderTiles(risk);
      renderStatus(risk);
    } catch (e) {
      A.showNotice($("notice"), e);
    }
    try {
      const j = (await A.getJSON("/api/nav/journal")).journal || [];
      if (j.length) {
        valueChart(j);
        drawdownChart(j);
        renderHeatmap(j);
      } else {
        const msg = '<div class="muted" style="padding:24px">No history yet — import transactions and assign holdings to sleeves.</div>';
        ["nav-chart", "dd-chart", "cal-heatmap"].forEach((id) => { $(id).innerHTML = msg; });
      }
    } catch (e) {
      A.showNotice($("notice"), e);
    }
  }

  document.addEventListener("DOMContentLoaded", load);
})();

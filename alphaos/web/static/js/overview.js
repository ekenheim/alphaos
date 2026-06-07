// Overview / RISK page — risk strip + NAV-index and drawdown charts.
(function () {
  const A = window.alphaos;
  const $ = (id) => document.getElementById(id);

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
    $("t-lev-foot").textContent = "target " + (risk.target_leverage != null ? A.fmtNum(risk.target_leverage, 2) + "×" : "—");
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
    $("t-reserve").textContent = A.fmtSEK(risk.external_reserve);

    const cagr = risk.planning_cagr || [];
    $("t-cagr").textContent = (cagr[0] != null && cagr[1] != null)
      ? `${A.fmtPct(cagr[0], 0)} – ${A.fmtPct(cagr[1], 0)}` : "—";
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

  // Account value vs cost basis. The gap between the lines is unrealized P&L —
  // deposits/buys raise BOTH lines together, so only market moves change the gap.
  function valueChart(snaps) {
    const x = snaps.map((s) => s.as_of);
    const value = snaps.map((s) => s.gross_asset_value);
    const cost = snaps.map((s) => s.cost_basis);
    const last = snaps.length ? snaps[snaps.length - 1] : null;
    if (last) {
      $("nav-last").textContent = "Value " + A.fmtSEK(last.gross_asset_value);
      const pnl = (last.gross_asset_value != null && last.cost_basis != null)
        ? last.gross_asset_value - last.cost_basis : null;
      $("nav-peak").textContent = pnl != null ? "P&L " + A.fmtSEKSigned(pnl) : "P&L —";
    }
    const traces = [{
      x, y: value, type: "scatter", mode: "lines", name: "value",
      line: { color: "#4dd0e1", width: 2 },
      hovertemplate: "%{x}<br>value %{y:,.0f} kr<extra></extra>",
    }, {
      x, y: cost, type: "scatter", mode: "lines", name: "cost basis",
      line: { color: "#6e7681", width: 1, dash: "dot" },
      hovertemplate: "%{x}<br>cost %{y:,.0f} kr<extra></extra>",
    }];
    Plotly.newPlot("nav-chart", traces, A.plotlyLayout({
      yaxis: { gridcolor: "#1f2733", zerolinecolor: "#1f2733", title: "SEK" },
    }), A.plotlyConfig);
  }

  // Drawdown off the running peak of account value (no contribution data needed).
  function drawdownChart(snaps) {
    const x = snaps.map((s) => s.as_of);
    let peak = -Infinity;
    const y = snaps.map((s) => {
      const v = s.equity != null ? s.equity : s.gross_asset_value;
      if (v == null) return null;
      if (v > peak) peak = v;
      return peak > 0 ? (v / peak - 1) * 100 : 0;
    });
    const minDD = Math.min(0, ...y.filter((v) => v != null));
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

  async function load() {
    A.dbChip($("db-chip"));
    try {
      const navData = await A.getJSON("/api/nav");
      const risk = navData.risk || {};
      const snaps = navData.snapshots || [];
      renderTiles(risk);
      renderStatus(risk);
      if (snaps.length) {
        valueChart(snaps);
        drawdownChart(snaps);
      } else {
        $("nav-chart").innerHTML = '<div class="muted" style="padding:24px">No history yet — import transactions and let pricing/daily snapshots build it.</div>';
        $("dd-chart").innerHTML = '<div class="muted" style="padding:24px">No drawdown data yet.</div>';
      }
    } catch (e) {
      A.showNotice($("notice"), e);
    }
  }

  document.addEventListener("DOMContentLoaded", load);
})();

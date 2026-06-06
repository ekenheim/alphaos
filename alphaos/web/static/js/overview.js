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

    $("t-nav").textContent = A.fmtNum(risk.nav_index, 3);

    const twr = $("t-twr");
    twr.textContent = A.fmtPct(risk.twr_period, 2);
    setColored(twr, risk.twr_period);

    const dd = $("t-dd");
    dd.textContent = A.fmtPct(risk.drawdown, 1);
    setColored(dd, risk.drawdown);
    const th = risk.thresholds || {};
    $("t-dd-foot").textContent = risk.headroom_to_half != null
      ? `headroom to −35%: ${A.fmtPct(Math.abs(Math.min(risk.headroom_to_half, 0)), 1)}`
      : "off NAV-index peak";

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

  function navChart(snaps) {
    const x = snaps.map((s) => s.as_of);
    const y = snaps.map((s) => s.nav_index);
    const last = snaps.length ? snaps[snaps.length - 1] : null;
    if (last) {
      $("nav-last").textContent = "Last " + A.fmtNum(last.nav_index, 3);
      $("nav-peak").textContent = "Peak " + A.fmtNum(last.peak_nav_index, 3);
    }
    const traces = [{
      x, y, type: "scatter", mode: "lines", name: "NAV index",
      line: { color: "#4dd0e1", width: 2 },
      hovertemplate: "%{x}<br>NAV %{y:.3f}<extra></extra>",
    }, {
      x, y: snaps.map((s) => s.peak_nav_index), type: "scatter", mode: "lines",
      name: "peak", line: { color: "#6e7681", width: 1, dash: "dot" },
      hoverinfo: "skip",
    }];
    Plotly.newPlot("nav-chart", traces, A.plotlyLayout({
      yaxis: { gridcolor: "#1f2733", zerolinecolor: "#1f2733", title: "index" },
    }), A.plotlyConfig);
  }

  function ddChart(snaps, thresholds) {
    const x = snaps.map((s) => s.as_of);
    const y = snaps.map((s) => (s.drawdown != null ? s.drawdown * 100 : null));
    const trace = {
      x, y, type: "scatter", mode: "lines", name: "drawdown",
      fill: "tozeroy", line: { color: "#f87171", width: 2 },
      fillcolor: "rgba(248,113,113,0.15)",
      hovertemplate: "%{x}<br>DD %{y:.1f}%<extra></extra>",
    };
    const lines = [
      { v: (thresholds.delever_half_dd ?? -0.35) * 100, c: "#fbbf24", t: "half −35%" },
      { v: (thresholds.delever_full_dd ?? -0.45) * 100, c: "#f87171", t: "full −45%" },
      { v: (thresholds.forced_sale_dd ?? -0.57) * 100, c: "#b91c1c", t: "forced −57%" },
    ];
    const shapes = lines.map((l) => ({
      type: "line", xref: "paper", x0: 0, x1: 1, yref: "y", y0: l.v, y1: l.v,
      line: { color: l.c, width: 1, dash: "dash" },
    }));
    const annotations = lines.map((l) => ({
      xref: "paper", x: 1, y: l.v, xanchor: "right", yanchor: "bottom",
      text: l.t, showarrow: false, font: { size: 10, color: l.c },
    }));
    const minThresh = Math.min(...lines.map((l) => l.v)) - 5;
    Plotly.newPlot("dd-chart", [trace], A.plotlyLayout({
      shapes, annotations,
      yaxis: { gridcolor: "#1f2733", zerolinecolor: "#1f2733", title: "drawdown %",
        range: [minThresh, 2], ticksuffix: "%" },
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
        navChart(snaps);
        ddChart(snaps, risk.thresholds || {});
      } else {
        $("nav-chart").innerHTML = '<div class="muted" style="padding:24px">No NAV snapshots yet. Add one on the NAV ledger page.</div>';
        $("dd-chart").innerHTML = '<div class="muted" style="padding:24px">No drawdown data yet.</div>';
      }
    } catch (e) {
      A.showNotice($("notice"), e);
    }
  }

  document.addEventListener("DOMContentLoaded", load);
})();

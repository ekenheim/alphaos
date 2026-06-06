// Overview page — META-EA hero + equity curve + monthly heatmap + per-symbol table.

(async function () {
  const Z = window.alphaos;
  try {
    const [pf, strats] = await Promise.all([
      Z.getJSON("/api/portfolio"),
      Z.getJSON("/api/strategies"),
    ]);

    renderHero(pf);
    renderEquity(pf);
    renderMonthly(pf);
    renderDistribution(pf);
    renderStratTable(strats);
  } catch (e) {
    document.querySelector("#hero-period").textContent = "Failed to load: " + e.message;
    console.error(e);
  }

  // Data-source + DB diagnostics chip (independent of the dashboard load).
  renderStatus();
  async function renderStatus() {
    const el = document.querySelector("#ds-status");
    if (!el) return;
    try {
      const s = await Z.getJSON("/api/status");
      const ds = s.data_source || {};
      const db = s.database || {};
      const src = ds.active === "minio"
        ? (ds.minio_reachable === false ? "MinIO ✗" : "MinIO")
        : "yfinance";
      const dbtxt = !db.configured ? "DB off" : (db.reachable ? "DB ✓" : "DB ✗");
      el.textContent = `src: ${src} · ${dbtxt}`;
      el.title = `data source: ${ds.active} (${ds.minio_endpoint || ""} / ${ds.minio_bucket || ""}) · `
        + `db: ${db.configured ? (db.host || "configured") : "not configured"}`;
      el.classList.toggle("pill-neg", ds.minio_reachable === false || db.reachable === false);
    } catch (e) {
      el.textContent = "status n/a";
    }
  }

  function renderHero(pf) {
    document.querySelector("#hero-net-profit").textContent = Z.fmtMoney(pf.net_profit);
    const start = pf.equity_ts.length ? pf.equity_ts[0].slice(0, 10) : "—";
    const end   = pf.equity_ts.length ? pf.equity_ts[pf.equity_ts.length - 1].slice(0, 10) : "—";
    document.querySelector("#hero-period").textContent =
      `${start} to ${end}  ·  ${pf.total_trades.toLocaleString()} trades`;

    document.querySelector("#kpi-cagr").textContent   = Z.fmtPct(pf.cagr);
    document.querySelector("#kpi-dd").textContent     = Z.fmtPct(pf.max_dd_pct);
    document.querySelector("#kpi-sharpe").textContent = pf.sharpe.toFixed(2);
    document.querySelector("#kpi-pf").textContent     = pf.profit_factor.toFixed(2);
    document.querySelector("#kpi-wr").textContent     = Z.fmtPct(pf.win_rate);
    document.querySelector("#kpi-trades").textContent = pf.total_trades.toLocaleString();
  }

  function renderEquity(pf) {
    const x = pf.equity_ts;
    const y = pf.equity_val;
    if (!x.length) { document.querySelector("#equity-chart").innerHTML =
        '<div style="color:var(--muted);padding:80px 0;text-align:center">No trades yet — pull more history or relax the setup filter.</div>';
      return;
    }
    const high = Math.max(...y);
    const final_ = y[y.length - 1];
    const peak = [];
    let running = -Infinity;
    for (const v of y) { running = Math.max(running, v); peak.push(running); }
    const dd = y.map((v, i) => (v / peak[i] - 1));
    const minDD = Math.min(...dd);

    document.querySelector("#equity-final").textContent = "Final " + Z.fmtMoneyShort(final_);
    document.querySelector("#equity-high").textContent  = "High "  + Z.fmtMoneyShort(high);
    document.querySelector("#equity-dd").textContent    = "DD "    + (minDD * 100).toFixed(1) + "%";

    Plotly.newPlot("equity-chart", [
      {
        type: "scatter", mode: "lines", name: "Equity",
        x, y,
        line: { color: "#34d399", width: 2 },
        fill: "tozeroy",
        fillcolor: "rgba(52,211,153,0.08)",
      },
    ], Z.plotlyLayout({
      showlegend: false,
      yaxis: { gridcolor: "#1f2733", tickprefix: "$", tickformat: ",.0f" },
    }), Z.plotlyConfig);
  }

  function renderMonthly(pf) {
    const tb = document.querySelector("#monthly-table tbody");
    tb.innerHTML = "";
    if (!pf.monthly.length) return;
    const months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];
    for (const row of pf.monthly) {
      const tr = document.createElement("tr");
      const yearCell = `<td><b>${row.year}</b></td>`;
      const cells = months.map(m => {
        const v = row[m] ?? 0;
        const cls = v > 0 ? "cell-pos" : (v < 0 ? "cell-neg" : "cell-zero");
        return `<td class="${cls}">${v === 0 ? "—" : Z.fmtMoney(v)}</td>`;
      }).join("");
      const t = row.TOTAL ?? 0;
      const totCls = t > 0 ? "cell-pos cell-total" : (t < 0 ? "cell-neg cell-total" : "cell-zero cell-total");
      const totCell = `<td class="${totCls}">${Z.fmtMoney(t)}</td>`;
      tr.innerHTML = yearCell + cells + totCell;
      tb.appendChild(tr);
    }
  }

  function renderDistribution(pf) {
    const entries = Object.entries(pf.trades_by_symbol);
    if (!entries.length) return;
    const labels = entries.map(([k]) => k);
    const values = entries.map(([, v]) => v);
    Plotly.newPlot("dist-chart", [{
      type: "pie",
      labels, values,
      hole: 0.55,
      marker: { colors: ["#4dd0e1", "#34d399", "#f87171", "#fbbf24", "#a78bfa", "#fb923c"] },
      textinfo: "label+percent",
      hoverinfo: "label+value",
    }], Z.plotlyLayout({ margin: { t: 8, b: 8, l: 8, r: 8 } }), Z.plotlyConfig);
  }

  function renderStratTable(strats) {
    const tb = document.querySelector("#strats-table tbody");
    tb.innerHTML = "";
    for (const s of strats.strategies) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${s.symbol}</td>
        <td>${s.trades}</td>
        <td>${Z.fmtPct(s.win_rate)}</td>
        <td class="${s.avg_r > 0 ? 'cell-pos' : 'cell-neg'}">${s.avg_r.toFixed(2)}</td>
        <td>${s.sharpe.toFixed(2)}</td>
        <td class="cell-neg">${Z.fmtPct(s.max_dd)}</td>
      `;
      tb.appendChild(tr);
    }
  }
})();

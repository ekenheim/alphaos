// Strategies page — comparison KPIs + stand-alone vs prop-firm + table.

(async function () {
  const Z = window.alphaos;
  try {
    const [pf, strats, prop] = await Promise.all([
      Z.getJSON("/api/portfolio"),
      Z.getJSON("/api/strategies"),
      Z.getJSON("/api/propfirm"),
    ]);
    renderKPIs(pf, strats);
    renderComparison(pf, prop);
    renderTable(strats);
  } catch (e) {
    console.error(e);
  }

  function renderKPIs(pf, strats) {
    const tiles = [
      ["Total trades", pf.total_trades.toLocaleString(), ""],
      ["Net profit", Z.fmtMoneyShort(pf.net_profit), pf.net_profit > 0 ? "pos" : "neg"],
      ["Sharpe", pf.sharpe.toFixed(2), ""],
      ["Profit factor", pf.profit_factor.toFixed(2), ""],
      ["Win rate", Z.fmtPct(pf.win_rate), ""],
      ["Max drawdown", Z.fmtPct(pf.max_dd_pct), "neg"],
      ["Instruments", strats.strategies.length, ""],
      ["CAGR", Z.fmtPct(pf.cagr), pf.cagr > 0 ? "pos" : "neg"],
    ];
    const grid = document.querySelector("#strat-kpis");
    grid.classList.add("kpi-7");
    grid.innerHTML = tiles.map(([label, val, cls]) => `
      <div class="kpi"><div class="kpi-label">${label}</div>
        <div class="kpi-value ${cls}">${val}</div></div>
    `).join("");
  }

  function renderComparison(pf, prop) {
    const cap = pf.starting_capital;
    const totalP = pf.net_profit;
    document.querySelector("#sa-profit").textContent = Z.fmtMoneyShort(totalP);
    document.querySelector("#sa-cap").textContent    = Z.fmtMoneyShort(cap);
    document.querySelector("#sa-roc").textContent    = Z.fmtPct(totalP / cap);
    document.querySelector("#sa-sharpe").textContent = pf.sharpe.toFixed(2);

    document.querySelector("#pf-profit").textContent = Z.fmtMoneyShort(prop.net_profit);
    document.querySelector("#pf-cap").textContent    = Z.fmtMoneyShort(prop.total_cost);
    document.querySelector("#pf-roi").textContent    = Z.fmtPct(prop.roi);
    const effLev = (prop.total_paid_out / Math.max(prop.total_cost, 1)).toFixed(1) + "x";
    document.querySelector("#pf-lev").textContent    = effLev;
    document.querySelector("#pf-sharpe").textContent = pf.sharpe.toFixed(2);
  }

  function renderTable(strats) {
    const tb = document.querySelector("#strat-table tbody");
    tb.innerHTML = "";
    for (const s of strats.strategies) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${s.symbol}</td>
        <td>${s.setup}</td>
        <td>${s.trades}</td>
        <td>${Z.fmtPct(s.win_rate)}</td>
        <td class="${s.avg_r > 0 ? 'cell-pos' : 'cell-neg'}">${s.avg_r.toFixed(2)}</td>
        <td>${s.sharpe.toFixed(2)}</td>
        <td class="${s.cagr > 0 ? 'cell-pos' : 'cell-neg'}">${Z.fmtPct(s.cagr)}</td>
        <td class="cell-neg">${Z.fmtPct(s.max_dd)}</td>
      `;
      tb.appendChild(tr);
    }
  }
})();

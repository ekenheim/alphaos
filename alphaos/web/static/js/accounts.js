// Prop accounts page — KPI grid + monthly cashflow chart + account table.

(async function () {
  const Z = window.alphaos;
  try {
    const prop = await Z.getJSON("/api/propfirm");
    renderKPIs(prop);
    renderCashflow(prop);
    renderTable(prop);
  } catch (e) {
    console.error(e);
  }

  function renderKPIs(p) {
    document.querySelector("#k-accounts").textContent = p.accounts;
    document.querySelector("#k-blowups").textContent  = p.blow_ups;
    document.querySelector("#k-attempts").textContent = p.attempts;
    document.querySelector("#k-cost").textContent     = Z.fmtMoneyShort(p.total_cost);
    document.querySelector("#k-payouts").textContent  = p.payouts.toLocaleString();
    document.querySelector("#k-paidout").textContent  = Z.fmtMoneyShort(p.total_paid_out);
    document.querySelector("#k-net").textContent      = Z.fmtMoneyShort(p.net_profit);
    document.querySelector("#k-roi").textContent      = Z.fmtPct(p.roi);
    document.querySelector("#k-phase").textContent    = p.final_phase;
  }

  function renderCashflow(p) {
    const el = document.querySelector("#cashflow-chart");
    if (!p.cashflow.length) {
      el.innerHTML = '<div style="color:var(--muted);padding:80px 0;text-align:center">No payouts yet in simulated window.</div>';
      return;
    }
    const x = p.cashflow.map(c => c.month);
    const pos = p.cashflow.map(c => c.payout);
    const neg = p.cashflow.map(c => c.cost);

    Plotly.newPlot(el, [
      { type: "bar", x, y: pos, name: "Payouts",
        marker: { color: "#4dd0e1" } },
      { type: "bar", x, y: neg, name: "Challenge fees",
        marker: { color: "#f87171" } },
    ], Z.plotlyLayout({
      barmode: "relative",
      yaxis: { gridcolor: "#1f2733", tickprefix: "$", tickformat: ",.0f", zerolinecolor: "#3a4452" },
    }), Z.plotlyConfig);
  }

  function renderTable(p) {
    const tb = document.querySelector("#accounts-table tbody");
    tb.innerHTML = "";
    for (const a of p.account_list) {
      const status = a.blown_up ? '<span class="status-blew">blown</span>'
                                : '<span class="status-funded">funded</span>';
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${a.firm}</td>
        <td>${a.symbol}</td>
        <td>${Z.fmtMoneyShort(a.starting_balance)}</td>
        <td class="${a.final_equity >= a.starting_balance ? 'cell-pos' : 'cell-neg'}">${Z.fmtMoneyShort(a.final_equity)}</td>
        <td class="cell-pos">${Z.fmtMoneyShort(a.payouts)}</td>
        <td>${a.payout_count}</td>
        <td>${status}</td>
      `;
      tb.appendChild(tr);
    }
  }
})();

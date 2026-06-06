// Paper trade page — load /api/paper, render KPI strip + tables.

(async function () {
  const Z = window.alphaos;
  try {
    const data = await Z.getJSON("/api/paper");
    renderKPIs(data.summary);
    renderBySymbol(data.summary);
    renderBySetup(data.summary);
    renderLedger(data.ledger);
  } catch (e) {
    console.error(e);
  }

  function renderKPIs(s) {
    const eqPct = (s.cum_equity - 1) * 100;
    const tiles = [
      ["CLOSED", s.closed.toLocaleString(), ""],
      ["OPEN", s.open.toLocaleString(), ""],
      ["WIN RATE", (s.win_rate * 100).toFixed(1) + "%", ""],
      ["PF", s.pf.toFixed(2), s.pf > 1 ? "pos" : "neg"],
      ["AVG R", (s.avg_r >= 0 ? "+" : "") + s.avg_r.toFixed(2), s.avg_r > 0 ? "pos" : "neg"],
      ["TOTAL R", (s.total_r >= 0 ? "+" : "") + s.total_r.toFixed(2), s.total_r > 0 ? "pos" : "neg"],
      ["EQUITY", (eqPct >= 0 ? "+" : "") + eqPct.toFixed(2) + "%", eqPct > 0 ? "pos" : "neg"],
    ];
    document.querySelector("#kpi-strip").innerHTML = tiles.map(([l, v, cls]) => `
      <div class="kpi"><div class="kpi-label">${l}</div>
        <div class="kpi-value ${cls}">${v}</div></div>
    `).join("");
  }

  function renderBySymbol(s) {
    const tb = document.querySelector("#by-symbol-table tbody");
    tb.innerHTML = "";
    for (const [sym, st] of Object.entries(s.by_symbol || {})) {
      tb.innerHTML += `<tr>
        <td>${sym}</td><td>${st.count.toFixed(0)}</td>
        <td class="${st.mean > 0 ? 'cell-pos' : 'cell-neg'}">${(st.mean >= 0 ? '+' : '') + st.mean.toFixed(2)}</td>
        <td class="${st.sum > 0 ? 'cell-pos' : 'cell-neg'}">${(st.sum >= 0 ? '+' : '') + st.sum.toFixed(2)}</td>
      </tr>`;
    }
    if (!Object.keys(s.by_symbol || {}).length) {
      tb.innerHTML = `<tr><td colspan="4" class="muted">No closed trades yet.</td></tr>`;
    }
  }

  function renderBySetup(s) {
    const tb = document.querySelector("#by-setup-table tbody");
    tb.innerHTML = "";
    for (const [setup, st] of Object.entries(s.by_setup || {})) {
      tb.innerHTML += `<tr>
        <td>${setup}</td><td>${st.count.toFixed(0)}</td>
        <td class="${st.mean > 0 ? 'cell-pos' : 'cell-neg'}">${(st.mean >= 0 ? '+' : '') + st.mean.toFixed(2)}</td>
        <td class="${st.sum > 0 ? 'cell-pos' : 'cell-neg'}">${(st.sum >= 0 ? '+' : '') + st.sum.toFixed(2)}</td>
      </tr>`;
    }
    if (!Object.keys(s.by_setup || {}).length) {
      tb.innerHTML = `<tr><td colspan="4" class="muted">No closed trades yet.</td></tr>`;
    }
  }

  function renderLedger(rows) {
    const tb = document.querySelector("#ledger-table tbody");
    tb.innerHTML = "";
    if (!rows || !rows.length) {
      tb.innerHTML = `<tr><td colspan="9" class="muted">Empty ledger. Run <code>python -m alphaos.cli paper scan</code>.</td></tr>`;
      return;
    }
    for (const r of rows) {
      const rClass = r.r_multiple > 0 ? "cell-pos" : (r.r_multiple < 0 ? "cell-neg" : "");
      tb.innerHTML += `<tr>
        <td>${(r.signal_ts || "").replace("T", " ").slice(0, 16)}</td>
        <td>${r.symbol || ""}</td>
        <td>${r.setup || ""}</td>
        <td>${r.entry_px ? r.entry_px.toFixed(2) : "—"}</td>
        <td>${r.stop_px ? r.stop_px.toFixed(2) : "—"}</td>
        <td>${r.target_px ? r.target_px.toFixed(2) : "—"}</td>
        <td>${r.exit_px ? r.exit_px.toFixed(2) + ' (' + (r.exit_reason || "") + ')' : "—"}</td>
        <td class="${rClass}">${r.r_multiple != null ? (r.r_multiple >= 0 ? '+' : '') + r.r_multiple.toFixed(2) : "—"}</td>
        <td>${r.status}</td>
      </tr>`;
    }
  }
})();

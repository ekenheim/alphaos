// Allocation page — target vs current per sleeve + edit target weight.
(function () {
  const A = window.alphaos;
  const $ = (id) => document.getElementById(id);

  let SLEEVES = [];

  function renderSummary(data) {
    $("k-total").textContent = A.fmtSEK(data.total_gross_value);
    const tws = data.target_weight_sum;
    const k = $("k-tws");
    k.textContent = A.fmtPct(tws, 1);
    const off = Math.abs((tws || 0) - 1) > 1e-6;
    k.classList.toggle("warn", off);
    $("k-tws-foot").textContent = off
      ? `⚠ weights sum to ${A.fmtPct(tws, 1)}, not 100%`
      : "weights sum to 100% ✓";
  }

  function renderTable(rows) {
    const tb = $("alloc-table").querySelector("tbody");
    tb.innerHTML = "";
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      const driftCls = r.drift > 1e-6 ? "pos" : (r.drift < -1e-6 ? "neg" : "");
      const dCls = r.rebalance_delta > 1e-6 ? "pos" : (r.rebalance_delta < -1e-6 ? "neg" : "");
      tr.innerHTML =
        `<td><b>${r.code}</b><div class="muted" style="font-size:11px">${r.name || ""}</div></td>` +
        `<td>${(r.kind || "").replace(/_/g, " ")}</td>` +
        `<td>${A.fmtPct(r.target_weight, 1)}</td>` +
        `<td>${A.fmtPct(r.current_weight, 1)}</td>` +
        `<td class="${driftCls}">${A.fmtPct(r.drift, 1)}</td>` +
        `<td>${A.fmtSEK(r.current_value)}</td>` +
        `<td class="${dCls}">${A.fmtSEKSigned(r.rebalance_delta)}</td>`;
      tb.appendChild(tr);
    });
  }

  function renderChart(rows) {
    const codes = rows.map((r) => r.code);
    const tmpl = "<b>%{x}</b><br>%{fullData.name}: %{y:.1f}%<extra></extra>";
    const traces = [
      { x: codes, y: rows.map((r) => r.target_weight * 100), type: "bar", name: "target %",
        marker: { color: "#4dd0e1" }, hovertemplate: tmpl },
      { x: codes, y: rows.map((r) => r.current_weight * 100), type: "bar", name: "current %",
        marker: { color: "#34d399" }, hovertemplate: tmpl },
    ];
    Plotly.newPlot("alloc-chart", traces, A.plotlyLayout({
      barmode: "group",
      yaxis: { gridcolor: "#1f2733", zerolinecolor: "#1f2733", ticksuffix: "%", title: "weight" },
    }), A.plotlyConfig);
  }

  function fillSleeveSelect(rows) {
    const sel = $("f-code");
    sel.innerHTML = "";
    rows.forEach((r) => {
      const o = document.createElement("option");
      o.value = r.code;
      o.textContent = `${r.code} — ${r.name || ""} (${A.fmtPct(r.target_weight, 1)})`;
      o.dataset.weight = r.target_weight;
      sel.appendChild(o);
    });
    syncWeight();
  }

  function syncWeight() {
    const opt = $("f-code").selectedOptions[0];
    if (opt) $("f-weight").value = opt.dataset.weight;
  }

  async function load() {
    A.dbChip($("db-chip"));
    try {
      const data = await A.getJSON("/api/allocation");
      SLEEVES = data.sleeves || [];
      renderSummary(data);
      renderTable(SLEEVES);
      renderChart(SLEEVES);
      fillSleeveSelect(SLEEVES);
    } catch (e) {
      A.showNotice($("notice"), e);
    }
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    const msg = $("form-msg");
    msg.className = "form-msg";
    msg.textContent = "saving…";
    let w = parseFloat($("f-weight").value);
    if (isNaN(w)) { msg.className = "form-msg err"; msg.textContent = "enter a number"; return; }
    if (w > 1.5) w = w / 100; // accept percent entry (24 -> 0.24)
    try {
      await A.postJSON("/api/sleeves", { code: $("f-code").value, target_weight: w });
      msg.className = "form-msg ok";
      msg.textContent = "saved ✓";
      await load();
    } catch (e) {
      msg.className = "form-msg err";
      msg.textContent = e.message;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    load();
    $("sleeve-form").addEventListener("submit", onSubmit);
    $("f-code").addEventListener("change", syncWeight);
  });
})();

// Shared utilities for the AlphaOS dashboard.

window.alphaos = {
  async getJSON(path) {
    const r = await fetch(path);
    if (!r.ok) {
      let data = {};
      try { data = await r.json(); } catch (e) { /* non-JSON body */ }
      const err = new Error(data.error || `${path}: HTTP ${r.status}`);
      err.status = r.status;
      err.data = data;
      throw err;
    }
    return r.json();
  },

  // POST JSON body, parse JSON response. Throws Error with .status/.data on !ok
  // (e.g. 400 ValueError or 503 database not configured).
  async postJSON(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await r.json(); } catch (e) { /* non-JSON body */ }
    if (!r.ok) {
      const err = new Error(data.error || `${path}: HTTP ${r.status}`);
      err.status = r.status;
      err.data = data;
      throw err;
    }
    return data;
  },

  fmtMoney(v, decimals = 0) {
    if (v == null || isNaN(v)) return "—";
    const sign = v < 0 ? "-" : (v > 0 ? "+" : "");
    return sign + "$" + Math.abs(v).toLocaleString("en-US", {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals,
    });
  },

  fmtMoneyShort(v) {
    if (v == null || isNaN(v)) return "—";
    const sign = v < 0 ? "-" : "";
    const a = Math.abs(v);
    if (a >= 1e6)  return sign + "$" + (a / 1e6).toFixed(2) + "M";
    if (a >= 1e3)  return sign + "$" + (a / 1e3).toFixed(1) + "k";
    return sign + "$" + a.toFixed(0);
  },

  fmtPct(v, decimals = 1) {
    if (v == null || isNaN(v)) return "—";
    return (v * 100).toFixed(decimals) + "%";
  },

  fmtNum(v, decimals = 2) {
    if (v == null || isNaN(v)) return "—";
    return v.toLocaleString("en-US", {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals,
    });
  },

  // Plotly defaults — dark theme matching CSS
  plotlyLayout(extra = {}) {
    return Object.assign({
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor:  "rgba(0,0,0,0)",
      font: { color: "#e6edf3", family: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif" },
      margin: { l: 50, r: 20, t: 16, b: 40 },
      xaxis: { gridcolor: "#1f2733", zerolinecolor: "#1f2733" },
      yaxis: { gridcolor: "#1f2733", zerolinecolor: "#1f2733" },
      hoverlabel: { bgcolor: "#121821", bordercolor: "#1f2733" },
      legend: { orientation: "h", y: 1.08 },
    }, extra);
  },

  plotlyConfig: {
    displayModeBar: false,
    responsive: true,
  },
};

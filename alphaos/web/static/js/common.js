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

  // DELETE request, parse JSON response. Throws Error with .status/.data on !ok
  // (e.g. 503 database not configured, 404 not found).
  async deleteJSON(path) {
    const r = await fetch(path, { method: "DELETE" });
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

  // Base-currency (SEK) money formatter — e.g. 1 234 567 kr. Signed only when negative.
  fmtSEK(v, decimals = 0) {
    if (v == null || isNaN(v)) return "—";
    const sign = v < 0 ? "-" : "";
    return sign + Math.abs(v).toLocaleString("sv-SE", {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals,
    }) + " kr";
  },

  // SEK with an explicit + for positive deltas (rebalance Δ, contributions).
  fmtSEKSigned(v, decimals = 0) {
    if (v == null || isNaN(v)) return "—";
    const sign = v < 0 ? "-" : (v > 0 ? "+" : "");
    return sign + Math.abs(v).toLocaleString("sv-SE", {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals,
    }) + " kr";
  },

  isDbError(err) {
    return !!err && err.status === 503;
  },

  // Render a friendly notice into a container element. Handles the 503
  // "database not configured" contract specially.
  showNotice(el, err) {
    if (!el) return;
    el.classList.remove("hidden");
    if (this.isDbError(err)) {
      el.innerHTML =
        "<b>Database not configured.</b> This page needs a Postgres connection. " +
        "Set <code>DATABASE_URL</code> (or <code>PGHOST/PGUSER/…</code>) from the " +
        "Crunchy Postgres secret, then reload.";
    } else {
      const msg = (err && err.message) ? err.message : String(err);
      el.innerHTML = "<b>Could not load data.</b> " +
        msg.replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
    }
  },

  // Wire the topbar DB chip from /api/status -> "DB ✓" / "DB ✗".
  async dbChip(el) {
    if (!el) return;
    try {
      const s = await this.getJSON("/api/status");
      const db = s.database || {};
      const ok = db.configured && db.reachable;
      const configured = !!db.configured;
      el.textContent = ok ? "DB ✓" : "DB ✗";
      el.classList.toggle("ok", !!ok);
      el.classList.toggle("pill-neg", !ok);
      const where = db.host ? `${db.host}/${db.database || ""}` : "unconfigured";
      el.title = `database: ${configured ? where : "not configured"}` +
        (configured ? ` · reachable: ${db.reachable ? "yes" : "no"}` : "") +
        (s.version ? ` · v${s.version}` : "");
    } catch (e) {
      el.textContent = "DB ✗";
      el.classList.add("pill-neg");
      el.title = "status unavailable";
    }
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

// Strategy archive page — performance table, per-strategy backtests, and a
// register-strategy form. Backed by /api/archive/*.

(function () {
  const Z = window.alphaos;
  const $ = (s) => document.querySelector(s);

  function esc(v) {
    if (v == null) return "";
    return String(v).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
  function fmtTs(t) {
    if (!t) return "—";
    return String(t).replace("T", " ").slice(0, 16);
  }
  // Performance shape isn't pinned by the contract, so pull metrics defensively:
  // accept flat (row.sharpe), latest_-prefixed (row.latest_sharpe), or nested (row.latest.sharpe).
  function metric(row, key) {
    if (row[key] != null) return row[key];
    if (row["latest_" + key] != null) return row["latest_" + key];
    if (row.latest && row.latest[key] != null) return row.latest[key];
    return null;
  }
  function pct(v) {
    if (v == null || isNaN(v)) return "—";
    const n = Number(v);
    return Math.abs(n) <= 1 ? Z.fmtPct(n) : n.toFixed(1) + "%";
  }
  function num(v, d = 2) { return v == null || isNaN(v) ? "—" : Z.fmtNum(Number(v), d); }

  let stratIndex = {}; // slug/name -> id, fallback for resolving strategy_id

  async function load() {
    try {
      const [perf, strats] = await Promise.all([
        Z.getJSON("/api/archive/performance"),
        Z.getJSON("/api/archive/strategies"),
      ]);
      $("#db-notice").classList.add("hidden");
      $("#content").classList.remove("hidden");
      indexStrategies(strats.strategies || []);
      renderPerf(perf.performance);
    } catch (e) {
      if (e.status === 503) {
        $("#content").classList.add("hidden");
        $("#db-notice").classList.remove("hidden");
      } else {
        console.error(e);
      }
    }
  }

  function indexStrategies(list) {
    stratIndex = {};
    for (const s of list) {
      if (s.slug) stratIndex[s.slug] = s.id;
      if (s.name) stratIndex[s.name] = s.id;
    }
  }

  function rowId(r) {
    if (r.id != null) return r.id;
    if (r.strategy_id != null) return r.strategy_id;
    if (r.slug != null && stratIndex[r.slug] != null) return stratIndex[r.slug];
    if (r.name != null && stratIndex[r.name] != null) return stratIndex[r.name];
    return null;
  }

  function renderPerf(performance) {
    const tb = $("#perf-table tbody");
    tb.innerHTML = "";
    const rows = Array.isArray(performance)
      ? performance
      : (performance && Array.isArray(performance.strategies) ? performance.strategies : []);
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="8" class="muted">No strategies yet. Register one below.</td></tr>`;
      return;
    }
    for (const r of rows) {
      const id = rowId(r);
      const name = r.name || r.strategy_name || r.slug || "—";
      const tr = document.createElement("tr");
      if (id != null) tr.className = "clickable";
      const dd = metric(r, "max_dd");
      tr.innerHTML = `
        <td>${esc(name)}</td>
        <td>${esc(r.status || "—")}</td>
        <td>${r.n_backtests != null ? r.n_backtests : "—"}</td>
        <td>${num(metric(r, "sharpe"))}</td>
        <td>${pct(metric(r, "win_rate"))}</td>
        <td>${num(metric(r, "avg_r"))}</td>
        <td>${pct(metric(r, "cagr"))}</td>
        <td class="${dd != null ? "cell-neg" : ""}">${pct(dd)}</td>`;
      if (id != null) {
        tr.addEventListener("click", () => {
          document.querySelectorAll("#perf-table tbody tr").forEach((x) => x.style.background = "");
          tr.style.background = "var(--panel-2)";
          loadBacktests(id, name);
        });
      }
      tb.appendChild(tr);
    }
  }

  async function loadBacktests(id, name) {
    $("#bt-sub").textContent = `Backtests for ${name}`;
    const tb = $("#bt-table tbody");
    tb.innerHTML = `<tr><td colspan="13" class="muted">Loading…</td></tr>`;
    try {
      const data = await Z.getJSON(`/api/archive/backtests?strategy_id=${encodeURIComponent(id)}`);
      renderBacktests(data.backtests || []);
    } catch (e) {
      tb.innerHTML = `<tr><td colspan="13" class="form-msg err">${esc(e.message)}</td></tr>`;
    }
  }

  function renderBacktests(list) {
    const tb = $("#bt-table tbody");
    tb.innerHTML = "";
    if (!list.length) {
      tb.innerHTML = `<tr><td colspan="13" class="muted">No backtests for this strategy.</td></tr>`;
      return;
    }
    for (const b of list) {
      const placebo = b.placebo_pass
        ? '<span class="pill ok">pass</span>'
        : '<span class="pill pill-neg">fail</span>';
      const ddCls = b.max_dd != null ? "cell-neg" : "";
      tb.innerHTML += `<tr>
        <td>${esc(b.symbol)}</td>
        <td>${esc(b.interval)}</td>
        <td>${esc(b.start_date || "—")}</td>
        <td>${esc(b.end_date || "—")}</td>
        <td>${b.n_trades != null ? b.n_trades : "—"}</td>
        <td>${pct(b.win_rate)}</td>
        <td>${num(b.avg_r)}</td>
        <td>${num(b.sharpe)}</td>
        <td class="${ddCls}">${pct(b.max_dd)}</td>
        <td>${pct(b.cagr)}</td>
        <td>${num(b.total_r)}</td>
        <td>${placebo}</td>
        <td>${fmtTs(b.created_at)}</td>
      </tr>`;
    }
  }

  function setupForm() {
    $("#strat-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.target;
      const msg = $("#strat-msg");
      msg.className = "form-msg";
      msg.textContent = "";
      const body = { slug: form.slug.value.trim() };
      const name = form.name.value.trim();
      if (name) body.name = name;
      const desc = form.description.value.trim();
      if (desc) body.description = desc;
      const status = form.status.value;
      if (status) body.status = status;
      const paramsRaw = form.params.value.trim();
      if (paramsRaw) {
        try {
          body.params = JSON.parse(paramsRaw);
        } catch (err) {
          msg.className = "form-msg err";
          msg.textContent = "Params must be valid JSON.";
          return;
        }
      }
      try {
        await Z.postJSON("/api/archive/strategies", body);
        msg.className = "form-msg ok";
        msg.textContent = "Strategy registered.";
        form.reset();
        load();
      } catch (err) {
        msg.className = "form-msg err";
        msg.textContent = err.status === 503 ? "Database not configured." : (err.message || "Failed.");
      }
    });
  }

  setupForm();
  load();
})();

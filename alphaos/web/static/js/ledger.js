// Live ledger page — summary strip, open positions w/ expandable events,
// execute form, and batch rebalance form. Backed by /api/ledger/*.

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
  function sideTag(side) {
    const s = (side || "long").toLowerCase();
    return `<span class="side-${s}">${esc(s)}</span>`;
  }

  // ---------- load + render ----------
  async function load() {
    try {
      const data = await Z.getJSON("/api/ledger/positions");
      $("#db-notice").classList.add("hidden");
      $("#content").classList.remove("hidden");
      renderSummary(data.summary || {});
      renderPositions(data.positions || []);
    } catch (e) {
      if (e.status === 503) {
        $("#content").classList.add("hidden");
        $("#db-notice").classList.remove("hidden");
      } else {
        console.error(e);
      }
    }
  }

  function renderSummary(s) {
    const open = s.open_count != null ? s.open_count : (s.open || 0);
    const pnl = s.total_realized_pnl != null ? s.total_realized_pnl : 0;
    const exp = s.open_exposure != null ? s.open_exposure : 0;
    $("#s-open").textContent = Number(open).toLocaleString();
    const pnlEl = $("#s-pnl");
    pnlEl.textContent = Z.fmtMoney(pnl, 2);
    pnlEl.className = "kpi-value " + (pnl > 0 ? "pos" : (pnl < 0 ? "neg" : ""));
    $("#s-exp").textContent = Z.fmtMoney(exp, 2);
  }

  function renderPositions(positions) {
    const tb = $("#positions-table tbody");
    tb.innerHTML = "";
    if (!positions.length) {
      tb.innerHTML = `<tr><td colspan="7" class="muted">No open positions. Record an execution below.</td></tr>`;
      return;
    }
    for (const p of positions) {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      tr.dataset.id = p.id;
      const pnlCls = p.realized_pnl > 0 ? "cell-pos" : (p.realized_pnl < 0 ? "cell-neg" : "");
      tr.innerHTML = `
        <td>${esc(p.symbol)}</td>
        <td>${sideTag(p.side)}</td>
        <td>${Z.fmtNum(p.qty, 2)}</td>
        <td>${Z.fmtNum(p.avg_entry_px, 4)}</td>
        <td class="${pnlCls}">${Z.fmtMoney(p.realized_pnl, 2)}</td>
        <td>${esc(p.strategy_slug || p.strategy_id || "—")}</td>
        <td>${fmtTs(p.opened_at)}</td>`;
      tr.addEventListener("click", () => toggleDetail(tr, p.id));
      tb.appendChild(tr);
    }
  }

  async function toggleDetail(tr, id) {
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("expand-row")) {
      next.remove();
      return;
    }
    const row = document.createElement("tr");
    row.className = "expand-row";
    row.innerHTML = `<td colspan="7"><div class="expand-inner muted">Loading events…</div></td>`;
    tr.after(row);
    try {
      const data = await Z.getJSON(`/api/ledger/positions/${id}`);
      row.querySelector(".expand-inner").innerHTML = renderEvents(data.position, data.events || []);
    } catch (e) {
      const msg = e.status === 404 ? "Position not found." : esc(e.message);
      row.querySelector(".expand-inner").innerHTML = `<span class="form-msg err">${msg}</span>`;
    }
  }

  function renderEvents(pos, events) {
    let head = "";
    if (pos) {
      head = `<div class="subcard-title">${esc(pos.symbol)} · ${esc(pos.side)} · #${esc(pos.id)} · ${esc(pos.status)}</div>`;
    }
    let rows = events.map((ev) => {
      const pnlCls = ev.realized_pnl > 0 ? "cell-pos" : (ev.realized_pnl < 0 ? "cell-neg" : "");
      return `<tr>
        <td>${fmtTs(ev.ts)}</td>
        <td>${esc(ev.action)}</td>
        <td>${Z.fmtNum(ev.qty, 2)}</td>
        <td>${Z.fmtNum(ev.price, 4)}</td>
        <td>${Z.fmtNum(ev.fees, 2)}</td>
        <td class="${pnlCls}">${Z.fmtMoney(ev.realized_pnl, 2)}</td>
        <td>${esc(ev.batch_id || "—")}</td>
        <td>${esc(ev.notes || "")}</td>
      </tr>`;
    }).join("");
    if (!events.length) rows = `<tr><td colspan="8" class="muted">No events.</td></tr>`;
    return head + `
      <div class="table-wrap">
        <table class="strats">
          <thead><tr><th>Time</th><th>Action</th><th>Qty</th><th>Price</th>
                <th>Fees</th><th>Realized P&L</th><th>Batch</th><th>Notes</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  // ---------- execute form ----------
  function numOrNull(v) {
    if (v == null || v === "") return null;
    const n = Number(v);
    return isNaN(n) ? null : n;
  }

  function setupExec() {
    const form = $("#exec-form");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const msg = $("#exec-msg");
      msg.className = "form-msg";
      msg.textContent = "";
      const body = {
        symbol: form.symbol.value.trim(),
        action: form.action.value,
        side: form.side.value,
        qty: numOrNull(form.qty.value),
        price: numOrNull(form.price.value),
        fees: numOrNull(form.fees.value) || 0,
      };
      const sid = numOrNull(form.strategy_id.value);
      if (sid != null) body.strategy_id = sid;
      const pid = numOrNull(form.position_id.value);
      if (pid != null) body.position_id = pid;
      const notes = form.notes.value.trim();
      if (notes) body.notes = notes;

      try {
        await Z.postJSON("/api/ledger/execute", body);
        msg.className = "form-msg ok";
        msg.textContent = "Recorded.";
        form.qty.value = "";
        form.price.value = "";
        form.notes.value = "";
        load();
      } catch (err) {
        msg.className = "form-msg err";
        msg.textContent = err.status === 503 ? "Database not configured." : (err.message || "Failed.");
      }
    });
  }

  // ---------- rebalance form ----------
  function legTemplate() {
    const div = document.createElement("div");
    div.className = "leg-row";
    div.innerHTML = `
      <div class="field"><label>Symbol</label><input class="lg-symbol" placeholder="EURUSD" /></div>
      <div class="field"><label>Action</label>
        <select class="lg-action">
          <option value="open">open</option><option value="add">add</option>
          <option value="trim">trim</option><option value="close">close</option>
        </select></div>
      <div class="field"><label>Side</label>
        <select class="lg-side"><option value="long">long</option><option value="short">short</option></select></div>
      <div class="field"><label>Qty</label><input class="lg-qty" type="number" step="any" /></div>
      <div class="field"><label>Price</label><input class="lg-price" type="number" step="any" /></div>
      <div class="field"><label>Fees</label><input class="lg-fees" type="number" step="any" placeholder="0" /></div>
      <button type="button" class="leg-x" title="Remove leg">×</button>`;
    div.querySelector(".leg-x").addEventListener("click", () => div.remove());
    return div;
  }

  function setupRebal() {
    const legs = $("#legs");
    legs.appendChild(legTemplate());
    legs.appendChild(legTemplate());
    $("#add-leg").addEventListener("click", () => legs.appendChild(legTemplate()));

    $("#rebal-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const msg = $("#rebal-msg");
      msg.className = "form-msg";
      msg.textContent = "";
      const collected = [];
      legs.querySelectorAll(".leg-row").forEach((row) => {
        const symbol = row.querySelector(".lg-symbol").value.trim();
        const qty = numOrNull(row.querySelector(".lg-qty").value);
        const price = numOrNull(row.querySelector(".lg-price").value);
        if (!symbol || qty == null || price == null) return; // skip blank/incomplete legs
        collected.push({
          symbol,
          action: row.querySelector(".lg-action").value,
          side: row.querySelector(".lg-side").value,
          qty, price,
          fees: numOrNull(row.querySelector(".lg-fees").value) || 0,
        });
      });
      if (!collected.length) {
        msg.className = "form-msg err";
        msg.textContent = "Add at least one complete leg (symbol, qty, price).";
        return;
      }
      const body = { legs: collected };
      const note = $("#rb-note").value.trim();
      if (note) body.note = note;
      try {
        const res = await Z.postJSON("/api/ledger/rebalance", body);
        msg.className = "form-msg ok";
        msg.textContent = `Batch ${res.batch_id || ""} · ${(res.events || []).length} events.`;
        load();
      } catch (err) {
        msg.className = "form-msg err";
        msg.textContent = err.status === 503 ? "Database not configured." : (err.message || "Failed.");
      }
    });
  }

  // ---------- init ----------
  setupExec();
  setupRebal();
  load();
})();

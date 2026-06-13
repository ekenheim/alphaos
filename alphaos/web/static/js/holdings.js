// Holdings page — valued view grouped by sleeve (GET /api/allocation),
// add/update/delete, Avanza CSV import (preview → apply), MinIO price refresh.
(function () {
  const A = window.alphaos;
  const $ = (id) => document.getElementById(id);

  let SLEEVES = [];        // from /api/sleeves (for the form select)
  let ALLOC = null;        // from /api/allocation (valued table)
  let PENDING_FILE = null;  // staged CSV awaiting "Apply import"

  // --- Form select ---

  function fillSleeveSelect() {
    const sel = $("f-sleeve");
    sel.innerHTML = '<option value="">(unassigned)</option>';
    SLEEVES.forEach((s) => {
      const o = document.createElement("option");
      o.value = s.code;
      o.textContent = `${s.code} — ${s.name || ""}`;
      sel.appendChild(o);
    });
  }

  // --- Valued table (from /api/allocation) ---

  function pnlCls(v) {
    return v > 1e-9 ? "pos" : (v < -1e-9 ? "neg" : "");
  }

  function holdingRow(h, code) {
    const pc = pnlCls(h.unrealized_pnl);
    const canExpand = !!h.isin;
    return `<tr class="${canExpand ? "clickable" : ""}" data-row="${h.id}" ${canExpand ? `data-isin="${h.isin}"` : ""}>
      <td><b>${h.symbol || "—"}</b><div class="muted" style="font-size:11px">${h.name || ""}</div></td>
      <td>${h.isin || ""}</td>
      <td>${(h.asset_class || "").replace(/_/g, " ")}</td>
      <td>${h.portfolio || "A"}</td>
      <td>${h.currency || ""}</td>
      <td>${A.fmtNum(h.quantity, 2)}</td>
      <td>${A.fmtNum(h.avg_price, 2)}</td>
      <td>${h.last_price != null ? A.fmtNum(h.last_price, 2) : "—"}</td>
      <td>${h.price_source || "—"}</td>
      <td>${A.fmtSEK(h.cost_basis)}</td>
      <td>${A.fmtSEK(h.market_value)}</td>
      <td class="${pc}">${A.fmtSEKSigned(h.unrealized_pnl)}</td>
      <td>${A.fmtPct(h.weight, 1)}</td>
      <td>${h.acquired_at || "—"}</td>
      <td>
        <button class="btn btn-ghost btn-sm" data-edit="${h.id}">edit</button>
        <button class="leg-x" data-del="${h.id}" title="delete">×</button>
      </td>
    </tr>`;
  }

  function groupCard(title, members, subVal, subWeight, code) {
    const card = document.createElement("div");
    card.className = "subcard";
    card.style.marginBottom = "14px";
    const rows = members.map((h) => holdingRow(h, code)).join("");
    card.innerHTML =
      `<div class="subcard-title">${title} · ${A.fmtSEK(subVal)} · ${A.fmtPct(subWeight, 1)}</div>` +
      `<div class="table-wrap"><table>
        <thead><tr>
          <th>Symbol</th><th>ISIN</th><th>Class</th><th>Portfolio</th><th>Ccy</th>
          <th>Qty</th><th>Avg price</th><th>Last price</th><th>Source</th>
          <th>Cost (SEK)</th><th>Market value (SEK)</th><th>Unrealized P/L (SEK)</th>
          <th>Weight</th><th>Acquired</th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody></table></div>`;
    return card;
  }

  function render() {
    if (!ALLOC) return;
    const bp = ALLOC.by_portfolio || {};
    const aw = bp.A ? ` · A ${A.fmtPct(bp.A.current_weight, 1)}` : "";
    const bw = bp.B ? ` · B ${A.fmtPct(bp.B.current_weight, 1)}` : "";
    $("h-total").textContent = "Total " + A.fmtSEK(ALLOC.total_gross_value) + aw + bw;

    const wrap = $("holdings-groups");
    wrap.innerHTML = "";

    const sleeves = ALLOC.sleeves || [];
    const unassigned = ALLOC.unassigned || { holdings: [] };
    const anyHoldings =
      sleeves.some((s) => (s.holdings || []).length) || (unassigned.holdings || []).length;

    if (!anyHoldings) {
      wrap.innerHTML = '<div class="muted" style="padding:8px">No holdings yet — add one above, or import a CSV.</div>';
      return;
    }

    sleeves.forEach((s) => {
      const members = s.holdings || [];
      if (!members.length) return;
      const title = `${s.code} — ${s.name || ""}`;
      wrap.appendChild(groupCard(title, members, s.current_value, s.current_weight, s.code));
    });

    if ((unassigned.holdings || []).length) {
      wrap.appendChild(groupCard(
        "Unassigned", unassigned.holdings,
        unassigned.current_value, unassigned.current_weight, "",
      ));
    }

    wrap.querySelectorAll("[data-edit]").forEach((b) =>
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        editHolding(parseInt(b.dataset.edit, 10));
      }));
    wrap.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        delHolding(parseInt(b.dataset.del, 10));
      }));
    wrap.querySelectorAll("tr.clickable[data-isin]").forEach((tr) =>
      tr.addEventListener("click", () => toggleHistory(tr)));
  }

  // --- Per-position transaction history (editable: add / edit / delete) ---

  // Toggle an inline expand-row beneath a holding row, lazily loading its
  // editable transaction ledger. Add a buy/sell, edit a row in place, or delete.
  async function toggleHistory(tr) {
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("expand-row")) {
      next.remove();
      tr.classList.remove("expanded");
      return;
    }
    const isin = tr.dataset.isin;
    const cols = (tr.children || []).length || 14;
    const exp = document.createElement("tr");
    exp.className = "expand-row";
    exp.innerHTML = `<td colspan="${cols}"><div class="expand-inner">
      <div class="muted">Loading history…</div></div></td>`;
    tr.after(exp);
    tr.classList.add("expanded");
    await renderHistory(exp.querySelector(".expand-inner"), isin);
  }

  async function renderHistory(inner, isin) {
    try {
      const res = await A.getJSON(`/api/history?isin=${encodeURIComponent(isin)}`);
      inner.innerHTML =
        `<div class="subcard-title">History · ${isin}</div>` +
        historyTable(res.history || []) + addTxnForm(isin);
      wireHistory(inner, isin);
    } catch (e) {
      inner.innerHTML =
        `<div class="subcard-title">History · ${isin}</div>` +
        `<div class="form-msg err">${A.isDbError(e) ? "database not configured" : e.message}</div>`;
    }
  }

  function historyTable(hist) {
    if (!hist.length) return '<div class="muted">No transactions yet — add one below.</div>';
    const rows = hist.map((t) => `<tr data-tx="${t.id}">
      <td><input type="date" class="tx-date" value="${t.date || ""}" disabled></td>
      <td><select class="tx-kind" disabled>
        <option value="buy"${t.kind === "buy" ? " selected" : ""}>buy</option>
        <option value="sell"${t.kind === "sell" ? " selected" : ""}>sell</option></select></td>
      <td><input type="number" step="any" class="tx-qty" value="${t.quantity}" disabled></td>
      <td><input type="number" step="any" class="tx-price" value="${t.price}" disabled></td>
      <td>${A.fmtNum(t.running_qty, 2)}</td>
      <td>${t.source || ""}</td>
      <td><input type="text" class="tx-note" value="${(t.note || "").replace(/"/g, "&quot;")}" disabled></td>
      <td>
        <button class="btn btn-ghost btn-sm" data-tx-edit="${t.id}">edit</button>
        <button class="btn btn-sm hidden" data-tx-save="${t.id}">save</button>
        <button class="btn btn-ghost btn-sm hidden" data-tx-cancel="${t.id}">cancel</button>
        <button class="leg-x" data-tx-del="${t.id}" data-src="${t.source || ""}" title="delete">×</button>
      </td>
    </tr>`).join("");
    return `<div class="table-wrap"><table>
      <thead><tr><th>Date</th><th>Kind</th><th>Qty</th><th>Price</th>
        <th>Running qty</th><th>Source</th><th style="text-align:left">Note</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  function addTxnForm(isin) {
    const today = new Date().toISOString().slice(0, 10);
    return `<div class="subcard" style="margin-top:10px;padding:10px">
      <div class="subcard-title">Add buy / sell</div>
      <div class="form-grid">
        <div class="field"><label>Date</label><input type="date" id="atx-date-${isin}" value="${today}"></div>
        <div class="field"><label>Kind</label><select id="atx-kind-${isin}"><option value="buy">buy</option><option value="sell">sell</option></select></div>
        <div class="field"><label>Quantity</label><input type="number" step="any" id="atx-qty-${isin}"></div>
        <div class="field"><label>Price</label><input type="number" step="any" id="atx-price-${isin}"></div>
        <div class="field field-wide"><label>Note</label><input type="text" id="atx-note-${isin}"></div>
      </div>
      <div class="form-actions">
        <button class="btn btn-sm" data-tx-add="${isin}">Add transaction</button>
        <span class="form-msg" data-tx-msg></span>
      </div></div>`;
  }

  // Wire add/edit/delete on a freshly-rendered history block. Mutations call
  // load() to refresh the derived holding values (qty/avg) in the parent table.
  function wireHistory(inner, isin) {
    inner.querySelectorAll("[data-tx-edit]").forEach((b) => b.addEventListener("click", () => {
      const row = b.closest("tr");
      row.querySelectorAll("input,select").forEach((el) => (el.disabled = false));
      b.classList.add("hidden");
      row.querySelector("[data-tx-save]").classList.remove("hidden");
      row.querySelector("[data-tx-cancel]").classList.remove("hidden");
    }));
    inner.querySelectorAll("[data-tx-cancel]").forEach((b) =>
      b.addEventListener("click", () => renderHistory(inner, isin)));
    inner.querySelectorAll("[data-tx-save]").forEach((b) => b.addEventListener("click", async () => {
      const row = b.closest("tr");
      const body = {
        date: row.querySelector(".tx-date").value,
        kind: row.querySelector(".tx-kind").value,
        quantity: parseFloat(row.querySelector(".tx-qty").value),
        price: parseFloat(row.querySelector(".tx-price").value),
        note: row.querySelector(".tx-note").value,
      };
      try { await A.putJSON(`/api/transactions/${b.dataset.txSave}`, body); await load(); }
      catch (e) { A.showNotice($("notice"), e); }
    }));
    inner.querySelectorAll("[data-tx-del]").forEach((b) => b.addEventListener("click", async () => {
      const warn = b.dataset.src === "avanza"
        ? "\n\nThis is an 'avanza' row — it will be re-created on the next CSV import." : "";
      if (!confirm(`Delete this transaction?${warn}`)) return;
      try { await A.deleteJSON(`/api/transactions/${b.dataset.txDel}`); await load(); }
      catch (e) { A.showNotice($("notice"), e); }
    }));
    const addBtn = inner.querySelector("[data-tx-add]");
    if (addBtn) addBtn.addEventListener("click", async () => {
      const msg = inner.querySelector("[data-tx-msg]");
      const g = (p) => inner.querySelector(`#atx-${p}-${isin}`);
      const qty = parseFloat(g("qty").value);
      const price = parseFloat(g("price").value);
      if (!g("date").value || isNaN(qty) || isNaN(price)) {
        msg.className = "form-msg err"; msg.textContent = "date, quantity and price are required"; return;
      }
      const body = {
        isin, date: g("date").value, kind: g("kind").value,
        quantity: qty, price: price, note: g("note").value.trim() || undefined,
      };
      try { await A.postJSON("/api/transactions", body); await load(); }
      catch (e) { msg.className = "form-msg err"; msg.textContent = e.message; }
    });
  }

  // Look up a holding (and its sleeve code) across the allocation payload.
  function findHolding(id) {
    if (!ALLOC) return null;
    for (const s of (ALLOC.sleeves || [])) {
      const h = (s.holdings || []).find((x) => x.id === id);
      if (h) return { h, code: s.code };
    }
    const u = ((ALLOC.unassigned || {}).holdings || []).find((x) => x.id === id);
    if (u) return { h: u, code: "" };
    return null;
  }

  // --- Form ---

  function editHolding(id) {
    const found = findHolding(id);
    if (!found) return;
    const h = found.h;
    $("f-id").value = h.id;
    $("f-sleeve").value = found.code || "";
    $("f-portfolio").value = h.portfolio || "A";
    $("f-symbol").value = h.symbol || "";
    $("f-isin").value = h.isin || "";
    $("f-name").value = h.name || "";
    $("f-asset").value = h.asset_class || "equity";
    $("f-currency").value = h.currency || "SEK";
    $("f-lastprice").value = h.last_price != null ? h.last_price : "";
    $("f-acquired").value = h.acquired_at || "";
    $("form-title").textContent = `Editing ${h.symbol || h.isin || h.id}`;
    const pill = $("edit-pill");
    pill.textContent = `id ${h.id}`;
    pill.classList.remove("hidden");
    $("f-submit").textContent = "Update holding";
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function resetForm() {
    $("holding-form").reset();
    $("f-id").value = "";
    $("f-portfolio").value = "A";
    $("f-currency").value = "SEK";
    $("form-title").textContent = "Add / update holding";
    $("edit-pill").classList.add("hidden");
    $("f-submit").textContent = "Save holding";
    $("form-msg").className = "form-msg";
    $("form-msg").textContent = "";
  }

  async function delHolding(id) {
    const found = findHolding(id);
    const label = found ? (found.h.symbol || found.h.isin || id) : id;
    if (!confirm(`Delete holding ${label}?`)) return;
    try {
      await A.deleteJSON(`/api/holdings/${id}`);
      await load();
    } catch (e) {
      A.showNotice($("notice"), e);
    }
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    const msg = $("form-msg");
    msg.className = "form-msg";
    msg.textContent = "saving…";
    const num = (v) => (v === "" || v == null ? undefined : parseFloat(v));
    const idVal = $("f-id").value;
    const body = {
      symbol: $("f-symbol").value.trim(),
      sleeve_code: $("f-sleeve").value || undefined,
      portfolio: $("f-portfolio").value || undefined,
      isin: $("f-isin").value.trim() || undefined,
      name: $("f-name").value.trim() || undefined,
      asset_class: $("f-asset").value,
      currency: $("f-currency").value.trim() || "SEK",
      last_price: num($("f-lastprice").value),
    };
    if (idVal) body.id = parseInt(idVal, 10);
    try {
      await A.postJSON("/api/holdings", body);
      resetForm();
      msg.className = "form-msg ok";
      msg.textContent = "saved ✓";
      await load();
    } catch (e) {
      msg.className = "form-msg err";
      msg.textContent = e.message;
    }
  }

  // --- CSV import (preview → apply) ---

  function previewEl() { return $("import-preview"); }

  async function onFilePicked(ev) {
    const file = ev.target.files && ev.target.files[0];
    PENDING_FILE = file || null;
    const box = previewEl();
    if (!file) { box.classList.add("hidden"); box.innerHTML = ""; return; }
    box.classList.remove("hidden");
    box.innerHTML = '<div class="muted">Parsing preview…</div>';
    try {
      const res = await A.uploadFile(
        "/api/import/transactions?preview=true", file, "file");
      renderPreview(res);
    } catch (e) {
      box.innerHTML = "";
      A.showNotice($("notice"), e);
    }
  }

  function renderPreview(res) {
    const s = res.summary || {};
    const holdings = res.holdings || [];
    const rows = holdings.map((h) => `<tr>
      <td><b>${h.isin || ""}</b></td>
      <td style="text-align:left">${h.name || ""}</td>
      <td>${h.currency || ""}</td>
      <td>${A.fmtNum(h.quantity, 2)}</td>
      <td>${A.fmtNum(h.avg_price, 2)}</td>
      <td>${A.fmtSEK(h.cost_basis_sek)}</td>
    </tr>`).join("");

    previewEl().innerHTML =
      `<div class="subcard-title">Import preview (nothing saved yet)</div>` +
      `<div class="form-grid" style="margin-bottom:12px">
         <div class="kpi"><div class="kpi-label">TRANSACTIONS</div><div class="kpi-value">${s.transactions ?? "—"}</div></div>
         <div class="kpi"><div class="kpi-label">HOLDINGS</div><div class="kpi-value">${s.holdings_count ?? holdings.length}</div></div>
         <div class="kpi"><div class="kpi-label">DATE RANGE</div><div class="kpi-value" style="font-size:16px">${s.date_min || "—"} → ${s.date_max || "—"}</div></div>
         <div class="kpi"><div class="kpi-label">DEPOSITS TOTAL</div><div class="kpi-value" style="font-size:18px">${A.fmtSEK(s.deposits_total)}</div></div>
       </div>` +
      (holdings.length
        ? `<div class="table-wrap"><table>
             <thead><tr><th>ISIN</th><th style="text-align:left">Name</th><th>Ccy</th>
               <th>Qty</th><th>Avg price</th><th>Cost basis (SEK)</th></tr></thead>
             <tbody>${rows}</tbody></table></div>`
        : `<div class="muted">No open holdings detected in this export.</div>`) +
      `<div class="form-actions">
         <button type="button" class="btn" id="import-apply">Apply import</button>
         <button type="button" class="btn btn-ghost" id="import-cancel">Cancel</button>
         <span id="import-msg" class="form-msg"></span>
         <span class="muted">Matched on ISIN — re-importing the same export is safe (idempotent); existing sleeve &amp; symbol are kept.</span>
       </div>`;

    $("import-apply").addEventListener("click", applyImport);
    $("import-cancel").addEventListener("click", () => {
      PENDING_FILE = null;
      $("import-file").value = "";
      previewEl().classList.add("hidden");
      previewEl().innerHTML = "";
    });
  }

  async function applyImport() {
    if (!PENDING_FILE) return;
    const msg = $("import-msg");
    const btn = $("import-apply");
    msg.className = "form-msg";
    msg.textContent = "importing…";
    btn.disabled = true;
    try {
      const res = await A.uploadFile("/api/import/transactions", PENDING_FILE, "file");
      const s = res.summary || {};
      msg.className = "form-msg ok";
      msg.textContent =
        `imported ✓ — ${s.transactions_imported ?? 0} transactions ` +
        `(${s.holdings_count ?? 0} holdings)`;
      PENDING_FILE = null;
      $("import-file").value = "";
      await load();
    } catch (e) {
      msg.className = "form-msg err";
      msg.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  }

  // --- MinIO price refresh ---

  async function refreshPrices() {
    const msg = $("prices-msg");
    const btn = $("prices-refresh");
    msg.className = "form-msg";
    msg.textContent = "refreshing prices…";
    btn.disabled = true;
    try {
      const res = await A.postJSON("/api/prices/refresh", {});
      const p = res.prices || {};
      if (p.ok) {
        const skipped = Array.isArray(p.skipped) ? p.skipped.length : 0;
        msg.className = "form-msg ok";
        msg.textContent =
          `prices ✓ — ${p.updated ?? 0} updated, ${skipped} skipped` +
          (p.as_of ? ` · as of ${p.as_of}` : "") +
          (p.bucket ? ` · ${p.bucket}` : "");
        await load();
      } else {
        msg.className = "form-msg err";
        msg.textContent = p.error || "price refresh unavailable";
      }
    } catch (e) {
      msg.className = "form-msg err";
      msg.textContent = A.isDbError(e) ? "database not configured" : e.message;
    } finally {
      btn.disabled = false;
    }
  }

  // --- Load ---

  async function load() {
    A.dbChip($("db-chip"));
    try {
      const [sl, al] = await Promise.all([
        A.getJSON("/api/sleeves"),
        A.getJSON("/api/allocation"),
      ]);
      SLEEVES = sl.sleeves || [];
      ALLOC = al;
      fillSleeveSelect();
      render();
    } catch (e) {
      A.showNotice($("notice"), e);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    load();
    $("holding-form").addEventListener("submit", onSubmit);
    $("f-reset").addEventListener("click", resetForm);
    $("import-file").addEventListener("change", onFilePicked);
    $("prices-refresh").addEventListener("click", refreshPrices);
  });
})();

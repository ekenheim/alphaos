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
          <th>Symbol</th><th>ISIN</th><th>Class</th><th>Ccy</th>
          <th>Qty</th><th>Avg price</th><th>Last price</th><th>Source</th>
          <th>Cost (SEK)</th><th>Market value (SEK)</th><th>Unrealized P/L (SEK)</th>
          <th>Weight</th><th>Acquired</th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody></table></div>`;
    return card;
  }

  function render() {
    if (!ALLOC) return;
    $("h-total").textContent = "Total " + A.fmtSEK(ALLOC.total_gross_value);

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

  // --- Per-position transaction history (GET /api/history) ---

  // Toggle an inline expand-row beneath a holding row, lazily loading its
  // transaction ledger (the 10→12→8 running-quantity timeline).
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
      <div class="subcard-title">History · ${isin}</div>
      <div class="muted">Loading history…</div></div></td>`;
    tr.after(exp);
    tr.classList.add("expanded");
    try {
      const res = await A.getJSON(`/api/history?isin=${encodeURIComponent(isin)}`);
      exp.querySelector(".expand-inner").innerHTML =
        `<div class="subcard-title">History · ${isin}</div>` + historyTable(res.history || []);
    } catch (e) {
      exp.querySelector(".expand-inner").innerHTML =
        `<div class="subcard-title">History · ${isin}</div>` +
        `<div class="form-msg err">${A.isDbError(e) ? "database not configured" : e.message}</div>`;
    }
  }

  function historyTable(hist) {
    if (!hist.length) return '<div class="muted">No transactions for this ISIN.</div>';
    const rows = hist.map((t) => `<tr>
      <td>${t.date || "—"}</td>
      <td class="${t.kind === "sell" ? "neg" : "pos"}">${t.kind || ""}</td>
      <td>${A.fmtNum(t.quantity, 2)}</td>
      <td>${A.fmtNum(t.price, 2)}</td>
      <td>${A.fmtNum(t.running_qty, 2)}</td>
      <td>${t.source || ""}</td>
      <td style="text-align:left">${t.note || ""}</td>
    </tr>`).join("");
    return `<div class="table-wrap"><table>
      <thead><tr><th>Date</th><th>Kind</th><th>Qty</th><th>Price</th>
        <th>Running qty</th><th>Source</th><th style="text-align:left">Note</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
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

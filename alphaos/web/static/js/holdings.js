// Holdings page — grouped by sleeve, add/update/delete.
(function () {
  const A = window.alphaos;
  const $ = (id) => document.getElementById(id);

  let SLEEVES = [];
  let HOLDINGS = [];

  function sleeveLabel(id) {
    const s = SLEEVES.find((x) => x.id === id);
    return s ? `${s.code} — ${s.name || ""}` : "Unassigned";
  }

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

  function render() {
    const total = HOLDINGS.reduce((a, h) => a + (h.market_value || 0), 0);
    $("h-total").textContent = "Total " + A.fmtSEK(total);

    // group by sleeve_id, preserving sleeve sort order, unassigned last
    const groups = new Map();
    SLEEVES.forEach((s) => groups.set(s.id, []));
    groups.set(null, []);
    HOLDINGS.forEach((h) => {
      const key = groups.has(h.sleeve_id) ? h.sleeve_id : null;
      groups.get(key).push(h);
    });

    const wrap = $("holdings-groups");
    wrap.innerHTML = "";
    if (!HOLDINGS.length) {
      wrap.innerHTML = '<div class="muted" style="padding:8px">No holdings yet — add one above.</div>';
      return;
    }
    groups.forEach((members, id) => {
      if (!members.length) return;
      const subtotal = members.reduce((a, h) => a + (h.market_value || 0), 0);
      const card = document.createElement("div");
      card.className = "subcard";
      card.style.marginBottom = "14px";
      const rows = members.map((h) => {
        const w = total > 0 ? h.market_value / total : 0;
        return `<tr>
          <td><b>${h.symbol}</b></td>
          <td>${h.name || ""}</td>
          <td>${h.isin || ""}</td>
          <td>${(h.asset_class || "").replace(/_/g, " ")}</td>
          <td>${h.currency || ""}</td>
          <td>${A.fmtNum(h.quantity, 2)}</td>
          <td>${A.fmtSEK(h.market_value)}</td>
          <td>${A.fmtPct(w, 1)}</td>
          <td>${h.as_of || ""}</td>
          <td>
            <button class="btn btn-ghost btn-sm" data-edit="${h.id}">edit</button>
            <button class="leg-x" data-del="${h.id}" title="delete">×</button>
          </td>
        </tr>`;
      }).join("");
      card.innerHTML =
        `<div class="subcard-title">${sleeveLabel(id)} · ${A.fmtSEK(subtotal)} · ${A.fmtPct(total > 0 ? subtotal / total : 0, 1)}</div>` +
        `<div class="table-wrap"><table>
          <thead><tr><th>Symbol</th><th>Name</th><th>ISIN</th><th>Class</th><th>Ccy</th>
            <th>Qty</th><th>Market value</th><th>Weight</th><th>As of</th><th></th></tr></thead>
          <tbody>${rows}</tbody></table></div>`;
      wrap.appendChild(card);
    });

    wrap.querySelectorAll("[data-edit]").forEach((b) =>
      b.addEventListener("click", () => editHolding(parseInt(b.dataset.edit, 10))));
    wrap.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", () => delHolding(parseInt(b.dataset.del, 10))));
  }

  function editHolding(id) {
    const h = HOLDINGS.find((x) => x.id === id);
    if (!h) return;
    $("f-id").value = h.id;
    $("f-sleeve").value = h.sleeve_code || (SLEEVES.find((s) => s.id === h.sleeve_id) || {}).code || "";
    $("f-symbol").value = h.symbol || "";
    $("f-isin").value = h.isin || "";
    $("f-name").value = h.name || "";
    $("f-asset").value = h.asset_class || "equity";
    $("f-currency").value = h.currency || "SEK";
    $("f-qty").value = h.quantity != null ? h.quantity : "";
    $("f-mv").value = h.market_value != null ? h.market_value : "";
    $("f-asof").value = h.as_of || "";
    $("f-notes").value = h.notes || "";
    $("form-title").textContent = `Editing ${h.symbol}`;
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
    const h = HOLDINGS.find((x) => x.id === id);
    if (!confirm(`Delete holding ${h ? h.symbol : id}?`)) return;
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
      quantity: num($("f-qty").value),
      market_value: num($("f-mv").value),
      as_of: $("f-asof").value || undefined,
      notes: $("f-notes").value.trim() || undefined,
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

  async function load() {
    A.dbChip($("db-chip"));
    try {
      const [sl, hl] = await Promise.all([
        A.getJSON("/api/sleeves"),
        A.getJSON("/api/holdings"),
      ]);
      SLEEVES = sl.sleeves || [];
      HOLDINGS = hl.holdings || [];
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
  });
})();

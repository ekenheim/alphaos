// Transactions page — the full buy/sell ledger (GET /api/transactions),
// manual rebalance entry (POST /api/transactions), and per-row delete.
// Holdings are derived from this ledger, so recording a transaction here
// recomputes the affected position.
(function () {
  const A = window.alphaos;
  const $ = (id) => document.getElementById(id);

  let TXNS = [];      // full ledger from /api/transactions
  let FILTER = "";    // ISIN substring filter (upper-cased)

  // --- Ledger table ---

  function kindCls(k) { return k === "sell" ? "neg" : "pos"; }

  function txRow(t) {
    return `<tr>
      <td>${t.date || "—"}</td>
      <td><b>${t.isin || "—"}</b></td>
      <td style="text-align:left">${t.name || ""}</td>
      <td class="${kindCls(t.kind)}">${t.kind || ""}</td>
      <td>${A.fmtNum(t.quantity, 2)}</td>
      <td>${A.fmtNum(t.price, 2)}</td>
      <td>${t.currency || ""}</td>
      <td>${t.amount_sek != null ? A.fmtSEK(t.amount_sek) : "—"}</td>
      <td>${A.fmtSEK(t.fees_sek)}</td>
      <td>${t.source || ""}</td>
      <td><button class="leg-x" data-del="${t.id}" data-src="${t.source || ""}"
        data-label="${(t.isin || t.id)}" title="delete">×</button></td>
    </tr>`;
  }

  function render() {
    const tbody = $("tx-table").querySelector("tbody");
    const rows = FILTER
      ? TXNS.filter((t) => (t.isin || "").toUpperCase().includes(FILTER))
      : TXNS;
    $("tx-count").textContent = `${rows.length} rows`;
    if (!rows.length) {
      tbody.innerHTML =
        `<tr><td colspan="11" class="muted" style="text-align:center;padding:14px">` +
        (TXNS.length ? "No transactions match this filter." :
          "No transactions yet — record one above or import a CSV on Holdings.") +
        `</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map(txRow).join("");
    tbody.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", () =>
        delTxn(parseInt(b.dataset.del, 10), b.dataset.src, b.dataset.label)));
  }

  // --- Mutations ---

  async function delTxn(id, source, label) {
    const warn = source === "avanza"
      ? `\n\nThis is an 'avanza' row — it will be re-created on the next CSV import.`
      : "";
    if (!confirm(`Delete transaction ${label}?${warn}`)) return;
    try {
      await A.deleteJSON(`/api/transactions/${id}`);
      await load();
    } catch (e) {
      A.showNotice($("notice"), e);
    }
  }

  function resetForm() {
    $("tx-form").reset();
    $("f-currency").value = "SEK";
    const msg = $("form-msg");
    msg.className = "form-msg";
    msg.textContent = "";
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    const msg = $("form-msg");
    msg.className = "form-msg";
    msg.textContent = "saving…";
    const num = (v) => (v === "" || v == null ? undefined : parseFloat(v));
    const body = {
      date: $("f-date").value,
      isin: $("f-isin").value.trim(),
      kind: $("f-kind").value,
      quantity: num($("f-qty").value),
      price: num($("f-price").value),
      currency: $("f-currency").value.trim() || "SEK",
      fees_sek: num($("f-fees").value),
      note: $("f-note").value.trim() || undefined,
    };
    try {
      await A.postJSON("/api/transactions", body);
      resetForm();
      msg.className = "form-msg ok";
      msg.textContent = "recorded ✓";
      await load();
    } catch (e) {
      msg.className = "form-msg err";
      msg.textContent = A.isDbError(e) ? "database not configured" : e.message;
    }
  }

  // --- Load ---

  async function load() {
    A.dbChip($("db-chip"));
    try {
      const res = await A.getJSON("/api/transactions");
      TXNS = res.transactions || [];
      render();
    } catch (e) {
      A.showNotice($("notice"), e);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    // default the date field to today
    $("f-date").value = new Date().toISOString().slice(0, 10);
    load();
    $("tx-form").addEventListener("submit", onSubmit);
    $("f-reset").addEventListener("click", resetForm);
    $("filter-isin").addEventListener("input", (ev) => {
      FILTER = ev.target.value.trim().toUpperCase();
      render();
    });
    $("filter-clear").addEventListener("click", () => {
      $("filter-isin").value = "";
      FILTER = "";
      render();
    });
  });
})();

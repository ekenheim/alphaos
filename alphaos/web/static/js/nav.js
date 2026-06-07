// NAV ledger page — snapshot table + add-snapshot form.
(function () {
  const A = window.alphaos;
  const $ = (id) => document.getElementById(id);

  function statusPill(status) {
    return `<span class="pill status-${status}">${(status || "normal").toUpperCase()}</span>`;
  }

  function render(snaps) {
    $("nav-count").textContent = `${snaps.length} rows`;
    const tb = $("nav-table").querySelector("tbody");
    tb.innerHTML = "";
    if (!snaps.length) {
      tb.innerHTML = '<tr><td colspan="11" class="muted">No snapshots yet — add one above.</td></tr>';
      return;
    }
    // newest first
    const rows = snaps.slice().reverse();
    rows.forEach((s) => {
      const tr = document.createElement("tr");
      const twrCls = s.twr_period == null ? "" : (s.twr_period >= 0 ? "pos" : "neg");
      const ddCls = (s.drawdown || 0) < 0 ? "neg" : "";
      tr.innerHTML =
        `<td>${s.as_of}</td>` +
        `<td>${A.fmtSEK(s.gross_asset_value)}</td>` +
        `<td>${A.fmtSEK(s.loan_balance)}</td>` +
        `<td>${A.fmtSEKSigned(s.net_contribution)}</td>` +
        `<td>${A.fmtSEK(s.equity)}</td>` +
        `<td class="${twrCls}">${s.twr_period == null ? "—" : A.fmtPct(s.twr_period, 2)}</td>` +
        `<td>${A.fmtNum(s.nav_index, 3)}</td>` +
        `<td class="${ddCls}">${A.fmtPct(s.drawdown, 1)}</td>` +
        `<td>${s.effective_leverage == null ? "—" : A.fmtNum(s.effective_leverage, 2) + "×"}</td>` +
        `<td>${A.fmtPct(s.belaningsgrad, 1)}</td>` +
        `<td>${statusPill(s.delever_status)}</td>`;
      tb.appendChild(tr);
    });
  }

  async function load() {
    A.dbChip($("db-chip"));
    try {
      const data = await A.getJSON("/api/nav");
      const snaps = data.snapshots || [];
      render(snaps);
      // Loan is derived from cash reconciliation; show the live value (blank = derive).
      const derivedLoan = data.risk ? data.risk.loan_balance : null;
      $("f-loan").placeholder = derivedLoan != null
        ? `derived (${A.fmtSEK(derivedLoan)})`
        : "derived from deposits vs purchases";
      await loadCashFlows();
    } catch (e) {
      A.showNotice($("notice"), e);
    }
  }

  // --- Cash flows ---

  function cfRow(c) {
    const cls = (c.amount_sek || 0) < 0 ? "neg" : "pos";
    return `<tr>
      <td>${c.date || "—"}</td>
      <td class="${cls}">${c.kind || ""}</td>
      <td><span class="pill">${c.source || "manual"}</span></td>
      <td class="${cls}">${A.fmtSEKSigned(c.amount_sek)}</td>
      <td style="text-align:left">${c.note || ""}</td>
      <td><button class="leg-x" data-del="${c.id}" data-label="${c.date}"
        title="delete">×</button></td>
    </tr>`;
  }

  function renderCashFlows(flows) {
    $("cf-count").textContent = `${flows.length} rows`;
    const tb = $("cf-table").querySelector("tbody");
    if (!flows.length) {
      tb.innerHTML = '<tr><td colspan="6" class="muted">No cash flows yet — add one above.</td></tr>';
      return;
    }
    tb.innerHTML = flows.slice().reverse().map(cfRow).join("");   // newest first
    tb.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", () => delCashFlow(parseInt(b.dataset.del, 10), b.dataset.label)));
  }

  async function loadCashFlows() {
    try {
      const res = await A.getJSON("/api/cashflows");
      renderCashFlows(res.cashflows || []);
    } catch (e) {
      A.showNotice($("notice"), e);
    }
  }

  async function delCashFlow(id, label) {
    if (!confirm(`Delete cash flow ${label}?`)) return;
    try {
      await A.deleteJSON(`/api/cashflows/${id}`);
      await load();
    } catch (e) {
      A.showNotice($("notice"), e);
    }
  }

  async function onCFSubmit(ev) {
    ev.preventDefault();
    const msg = $("cf-msg");
    msg.className = "form-msg";
    msg.textContent = "saving…";
    const body = {
      date: $("f-cf-date").value,
      kind: $("f-cf-kind").value,
      amount_sek: parseFloat($("f-cf-amount").value),
      note: $("f-cf-note").value.trim() || undefined,
    };
    if (!body.date) { msg.className = "form-msg err"; msg.textContent = "date required"; return; }
    if (!(body.amount_sek > 0)) { msg.className = "form-msg err"; msg.textContent = "amount must be > 0"; return; }
    try {
      await A.postJSON("/api/cashflows", body);
      $("cf-form").reset();
      $("f-cf-date").value = new Date().toISOString().slice(0, 10);
      msg.className = "form-msg ok";
      msg.textContent = "cash flow added ✓";
      await load();
    } catch (e) {
      msg.className = "form-msg err";
      msg.textContent = A.isDbError(e) ? "database not configured" : e.message;
    }
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    const msg = $("form-msg");
    msg.className = "form-msg";
    msg.textContent = "saving…";
    const num = (v) => (v === "" || v == null ? undefined : parseFloat(v));
    const body = {
      as_of: $("f-asof").value,
      gross_asset_value: num($("f-gross").value),
      loan_balance: num($("f-loan").value),
      net_contribution: num($("f-contrib").value),
      notes: $("f-notes").value.trim() || undefined,
    };
    if (!body.as_of) { msg.className = "form-msg err"; msg.textContent = "as_of required"; return; }
    try {
      await A.postJSON("/api/nav", body);
      $("nav-form").reset();
      msg.className = "form-msg ok";
      msg.textContent = "snapshot added ✓";
      await load();
    } catch (e) {
      msg.className = "form-msg err";
      msg.textContent = e.message;
    }
  }

  async function onSnapNow() {
    const msg = $("form-msg");
    msg.className = "form-msg";
    msg.textContent = "snapshotting…";
    try {
      await A.postJSON("/api/nav/snapshot-now", {});
      msg.className = "form-msg ok";
      msg.textContent = "snapshot updated ✓";
      await load();
    } catch (e) {
      msg.className = "form-msg err";
      msg.textContent = A.isDbError(e) ? "database not configured" : e.message;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    load();
    $("nav-form").addEventListener("submit", onSubmit);
    $("snap-now").addEventListener("click", onSnapNow);
    $("f-cf-date").value = new Date().toISOString().slice(0, 10);
    $("cf-form").addEventListener("submit", onCFSubmit);
  });
})();

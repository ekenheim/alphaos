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
      render(data.snapshots || []);
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

  document.addEventListener("DOMContentLoaded", () => {
    load();
    $("nav-form").addEventListener("submit", onSubmit);
  });
})();

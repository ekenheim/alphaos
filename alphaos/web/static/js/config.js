// Settings page — view/edit the portfolio config.
(function () {
  const A = window.alphaos;
  const $ = (id) => document.getElementById(id);

  // Field spec: key, label, type, hint. Order mirrors the config model.
  const FIELDS = [
    { k: "base_currency", l: "Base currency", t: "text", h: "SEK" },
    { k: "account_label", l: "Account label", t: "text", h: "Avanza ISK" },
    { k: "leverage_target", l: "Leverage target", t: "num", h: "running leverage now, e.g. 1.30 (→ 1.10 target)" },
    { k: "leverage_floor", l: "Leverage floor (glide)", t: "num", h: "large-portfolio glide floor, e.g. 1.00" },
    { k: "delever_floor_leverage", l: "De-lever floor leverage", t: "num", h: "level the -35/-45/-20 rule cuts TOWARD in a drawdown, e.g. 1.06 — not the running target" },
    { k: "glide_low_assets", l: "Glide low assets (SEK)", t: "num", h: "below ⇒ target leverage" },
    { k: "glide_high_assets", l: "Glide high assets (SEK)", t: "num", h: "at/above ⇒ floor leverage" },
    { k: "blended_rate", l: "Blended rate", t: "num", h: "fraction, e.g. 0.0234" },
    { k: "repriced_rate", l: "Repriced rate", t: "num", h: "fraction, e.g. 0.0359" },
    { k: "belaningsgrad_cliff", l: "Belåningsgrad cliff", t: "num", h: "fraction, e.g. 0.25" },
    { k: "delever_half_dd", l: "De-lever half DD", t: "num", h: "negative, e.g. -0.35" },
    { k: "delever_full_dd", l: "De-lever full DD", t: "num", h: "negative, e.g. -0.45" },
    { k: "reentry_recovery", l: "Re-entry recovery", t: "num", h: "fraction, e.g. 0.20" },
    { k: "forced_sale_dd", l: "Forced-sale DD", t: "num", h: "negative, e.g. -0.57" },
    { k: "external_reserve", l: "External reserve (SEK)", t: "num", h: "off-book buffer" },
    { k: "planning_cagr_low", l: "Planning CAGR low", t: "num", h: "fraction, e.g. 0.10" },
    { k: "planning_cagr_high", l: "Planning CAGR high", t: "num", h: "fraction, e.g. 0.16" },
    { k: "notes", l: "Notes", t: "textarea", h: "" },
  ];

  function buildForm(cfg) {
    const wrap = $("config-fields");
    wrap.innerHTML = "";
    FIELDS.forEach((f) => {
      const div = document.createElement("div");
      div.className = "field" + (f.t === "textarea" ? " field-wide" : "");
      const val = cfg[f.k];
      let control;
      if (f.t === "textarea") {
        control = `<textarea id="cf-${f.k}">${val == null ? "" : String(val)}</textarea>`;
      } else if (f.t === "num") {
        control = `<input id="cf-${f.k}" type="number" step="any" value="${val == null ? "" : val}" />`;
      } else {
        control = `<input id="cf-${f.k}" type="text" value="${val == null ? "" : String(val)}" />`;
      }
      div.innerHTML = `<label for="cf-${f.k}">${f.l}</label>${control}` +
        (f.h ? `<span class="kpi-foot">${f.h}</span>` : "");
      wrap.appendChild(div);
    });
  }

  // --- FX section ---

  function fillFx(cfg) {
    $("fx-usd").value = cfg.fx_usd_sek == null ? "" : cfg.fx_usd_sek;
    $("fx-eur").value = cfg.fx_eur_sek == null ? "" : cfg.fx_eur_sek;
    $("fx-asof").value = cfg.fx_as_of || "—";
    $("fx-source").value = cfg.fx_source || "—";
    $("fx-asof-pill").textContent = "as of " + (cfg.fx_as_of || "—");
  }

  // Apply the {usd_sek, eur_sek, as_of, source} shape returned by /api/fx/refresh.
  function fillFxFromRefresh(fx) {
    $("fx-usd").value = fx.usd_sek == null ? "" : fx.usd_sek;
    $("fx-eur").value = fx.eur_sek == null ? "" : fx.eur_sek;
    $("fx-asof").value = fx.as_of || "—";
    $("fx-source").value = fx.source || "—";
    $("fx-asof-pill").textContent = "as of " + (fx.as_of || "—");
  }

  async function onFxSubmit(ev) {
    ev.preventDefault();
    const msg = $("fx-msg");
    msg.className = "form-msg";
    msg.textContent = "saving…";
    const body = {};
    const usd = $("fx-usd").value;
    const eur = $("fx-eur").value;
    if (usd !== "") body.fx_usd_sek = parseFloat(usd);
    if (eur !== "") body.fx_eur_sek = parseFloat(eur);
    try {
      const data = await A.postJSON("/api/config", body);
      fillFx(data.config || {});
      msg.className = "form-msg ok";
      msg.textContent = "saved ✓";
    } catch (e) {
      msg.className = "form-msg err";
      msg.textContent = e.message;
    }
  }

  async function onFxRefresh() {
    const msg = $("fx-msg");
    const btn = $("fx-refresh");
    msg.className = "form-msg";
    msg.textContent = "refreshing…";
    btn.disabled = true;
    try {
      const res = await A.postJSON("/api/fx/refresh", {});
      const fx = res.fx || {};
      fillFxFromRefresh(fx);
      if (fx.ok) {
        msg.className = "form-msg ok";
        msg.textContent = `refreshed ✓${fx.source ? " · " + fx.source : ""}`;
      } else {
        msg.className = "form-msg err";
        msg.textContent = fx.error || "FX refresh failed; kept cached rates";
      }
    } catch (e) {
      msg.className = "form-msg err";
      msg.textContent = A.isDbError(e) ? "database not configured" : e.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function load() {
    A.dbChip($("db-chip"));
    try {
      const data = await A.getJSON("/api/config");
      const cfg = data.config || {};
      buildForm(cfg);
      fillFx(cfg);
    } catch (e) {
      A.showNotice($("notice"), e);
    }
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    const msg = $("form-msg");
    msg.className = "form-msg";
    msg.textContent = "saving…";
    const body = {};
    FIELDS.forEach((f) => {
      const el = $(`cf-${f.k}`);
      if (!el) return;
      const raw = el.value;
      if (f.t === "num") {
        if (raw !== "") body[f.k] = parseFloat(raw);
      } else {
        body[f.k] = raw;
      }
    });
    try {
      const data = await A.postJSON("/api/config", body);
      buildForm(data.config || {});
      msg.className = "form-msg ok";
      msg.textContent = "saved ✓";
    } catch (e) {
      msg.className = "form-msg err";
      msg.textContent = e.message;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    load();
    $("config-form").addEventListener("submit", onSubmit);
    $("f-reload").addEventListener("click", load);
    $("fx-form").addEventListener("submit", onFxSubmit);
    $("fx-refresh").addEventListener("click", onFxRefresh);
  });
})();

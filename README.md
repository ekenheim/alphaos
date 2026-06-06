# AlphaOS — V2-FRONTIER portfolio tracker

Self-contained Python package that tracks the **V2-FRONTIER** portfolio: a
leveraged, multi-sleeve allocation run on an **Avanza ISK** account in **SEK**.
It is a FastAPI + static HTML/CSS/JS dashboard backed by a **PostgreSQL** store
that holds the sleeve allocation, the **transactions ledger** (from which the
holdings are derived), the portfolio config (risk / leverage parameters) and the
**NAV-index ledger** used to drive the binding de-lever rule.

This is a **tracker and risk cockpit**, not a trading engine. It does not place
orders or run backtests — you record holdings and NAV snapshots, and it computes
allocation drift, time-weighted return, drawdown and the resulting leverage /
de-lever posture. It can optionally **pull daily closes** for US stocks (from a
MinIO/S3 bucket) and **FX rates** (from the Riksbank) so market values mark
toward the latest price; without those it falls back to your purchase cost.

> **Personal portfolio tooling, not investment advice.**

## What V2-FRONTIER is

A leveraged multi-sleeve equity portfolio held in an Avanza ISK (SEK). Capital
is split across five sleeves with fixed target weights; leverage is applied on
top via the broker margin (belåning) and is governed by a glide path and a hard
de-lever rule keyed off drawdown.

### Sleeves

| Code     | Sleeve                       | Kind (`SleeveKind`)         | Target |
|----------|------------------------------|-----------------------------|-------:|
| `CNDX`   | Nasdaq-100 beta core         | `beta_core`                 |  24%   |
| `VVSM`   | Momentum / factor tilt       | `tilt`                      |  11%   |
| `RAW`    | Discretionary equity (K=20)  | `discretionary_equity`      |  45%   |
| `CA`     | Cross-asset insurance        | `cross_asset_insurance`     |  10%   |
| `LOWVOL` | Low-vol carve-out            | `low_vol_carve`             |  10%   |

`RAW K=20` is the discretionary book: a concentrated basket of up to ~20 single
names. The target weights sum to 100%; the dashboard shows current weight vs
target, drift, and the rebalance delta per sleeve.

### NAV-index / TWR ledger

Performance is tracked as a **time-weighted return (TWR) on equity**
(equity = gross asset value − loan balance), measured **ex contributions** so
deposits/withdrawals don't distort returns. Each NAV snapshot:

- links the periods into a **NAV index** (the first snapshot baselines to 1.0),
- tracks the running **peak** and the **drawdown off that peak**,
- computes **leverage** (gross / equity) and **belåningsgrad** (loan / gross),
- derives the **de-lever status** from the config thresholds.

Snapshots are append-only and are entered with the gross asset value, the loan
balance and any net contribution for the period (gross defaults to the sum of
current holdings if omitted).

### The binding de-lever rule

Drawdown off the NAV peak (ex contributions) drives a hard, non-discretionary
de-lever ladder:

| Drawdown | Status   | Action                                    |
|----------|----------|-------------------------------------------|
| 0 … −35% | `normal` | full target leverage per the glide path   |
| ≤ −35%   | `half`   | cut leverage to half                       |
| ≤ −45%   | `full`   | de-lever fully (loan → 0)                  |
| ≤ −57%   | —        | **forced-sale boundary** (margin call) — the line you never want to touch |

Re-entry happens on recovery back above the configured threshold. This rule is
the whole point of the tool: the glide path sets the *normal* leverage, the
de-lever ladder protects against the forced-sale cliff.

### Leverage glide path

Effective target leverage shrinks as the account grows (linear interpolation
between the configured asset bands):

- **1.30×** while equity is below **2.5M SEK**,
- gliding down to **~1.00×** (no leverage) at **10M+ SEK**.

Smaller accounts run hotter to compound faster; larger accounts de-risk toward
unlevered.

### Belåningsgrad cliff

Independent of drawdown, the loan-to-gross ratio (**belåningsgrad**) is capped
at a **25% cliff** — Avanza's margin headroom before forced action. The glide
path keeps normal operation well inside this; the dashboard surfaces it as a
guardrail.

## Transactions ledger & derived positions

Positions are a **derived view of a transactions ledger** — every buy and sell
is a row (date, ISIN, kind, quantity, price, currency, SEK amount, fees, source,
note). A holding's **quantity**, **average price** (`avg_price`), **SEK cost
basis** (`cost_basis_sek`) and **acquired date** are **computed (average-cost)**
from that ledger; they are **read-only** in the UI. You **change a position by
adding a transaction** — via CSV import or the **manual rebalance form** — not by
editing a quantity directly. Each ISIN's holding is recomputed automatically when
its transactions change. Editing a holding now only sets **metadata** (sleeve,
symbol, name, asset class, last price).

The **Transactions** page lists the full ledger and lets you add or delete rows;
the **Holdings** page shows per-position **history** — the running quantity over
time (e.g. `10 → 12 → 8`) as each buy/sell is applied.

> The ledger lives in the **`transactions`** table added by **migration 0005** —
> run `alphaos db upgrade` after pulling this version to create it.

## Cost basis & market value

The **market value** is not a stored field — it is **computed**:

```
market_value (SEK) = quantity × price × FX
```

where:

- **price** is the holding's `last_price` if one is set, otherwise it falls back
  to `avg_price` (i.e. cost). The source is tracked in `price_source`
  (`minio` / `manual` / `cost` / `none`).
- **FX** converts the instrument currency to SEK (USD→SEK, EUR→SEK; SEK is 1.0).

So an unpriced holding marks at cost, and unrealized P&L (`market_value −
cost_basis`) is shown per holding once a live price is available.

### Where prices come from

- **US stocks** can be marked to the latest **daily close** read from a MinIO/S3
  bucket (`stocks-us`). Run `alphaos prices refresh` (or `POST
  /api/prices/refresh`) to pull the latest closes and set `last_price` /
  `last_price_date` with `price_source=minio`. This is **optional** — without
  MinIO credentials configured, US stocks just stay at cost (or a manual price).
- **Manual price**: edit a holding to set a price by hand (`price_source=manual`)
  for anything not covered by MinIO.
- Otherwise the holding marks at **cost** (`avg_price`).

See [`DEPLOYMENT.md`](DEPLOYMENT.md) for the MinIO env (it is read-only and
optional).

## FX rates

USD→SEK and EUR→SEK rates are **auto-fetched from the Riksbank** (with **ECB** as
a fallback) and **cached in the portfolio config** (`fx_usd_sek`, `fx_eur_sek`,
`fx_as_of`, `fx_source`). Refresh them with `alphaos fx refresh` (or `POST
/api/fx/refresh`); they are also **editable by hand in Settings**, which is the
escape hatch when the cluster has no outbound internet. Cached rates are reused
until you refresh, so the app keeps working offline.

## Import transactions (Avanza CSV)

You can import an **Avanza _transaktioner_ CSV export** instead of entering
holdings by hand. On the Holdings page, upload the CSV and you get a **preview**
first (parsed holdings, total deposits, date range, row count) with no database
writes; confirm to apply.

The import **persists the individual buy/sell rows** into the transactions ledger
(`source='avanza'`) and recomputes the affected holdings from them. It is
**idempotent**: re-importing a full export **replaces that date range** of Avanza
rows, so quantities and deposits are **not** doubled. Any **manual transactions
are preserved** — only the `avanza`-sourced rows in the file's date range are
replaced. Holding metadata (`sleeve_id`, `symbol`, …) is preserved across imports.

> **Privacy:** the transaktioner CSV is **personal data**. It is **gitignored**
> and must **never be committed** — it is only ever uploaded to your running
> instance.

## Install (standalone)

```bash
pip install -e .[dev]      # editable install
cp .env.example .env       # fill in the Postgres connection (see below)
alphaos db upgrade         # create the tables
alphaos db seed            # seed the five default sleeves
alphaos serve              # -> http://127.0.0.1:8503
```

Or without an editable install:

```bash
pip install -r requirements.txt
alphaos db upgrade && alphaos db seed
alphaos serve
```

Python 3.10+. The app requires a PostgreSQL database — configure it via
`DATABASE_URL` (or the discrete `PG*` parts). See
[`DEPLOYMENT.md`](DEPLOYMENT.md) for the container image and the Crunchy
(CPNG) secret mapping.

## Pages

| URL                  | What it shows                                                                 |
|----------------------|------------------------------------------------------------------------------|
| `/` (Overview / Risk)| NAV index + drawdown curve, current TWR, leverage, belåningsgrad, de-lever status, distance to the −57% forced-sale line, glide-path target |
| `/allocation.html`   | Sleeves table: target vs current weight, drift, rebalance delta; total gross value; any unassigned holdings |
| `/holdings.html`     | All holdings per sleeve (symbol, ISIN, asset class, quantity, purchase price, cost basis, last price + source, computed market value, unrealized P&L, weight). Quantity / avg price / cost basis are **derived from the ledger (read-only)**; edit sets metadata only. Per-position **history** (running quantity over time); **import an Avanza transaktioner CSV** (with preview) |
| `/transactions.html` | **Transactions ledger**: every buy/sell (date, ISIN, kind, quantity, price, currency, SEK amount, fees, source, note); add a manual transaction or delete a row (the affected holding is recomputed) |
| `/nav.html`          | **NAV ledger**: every snapshot (gross, loan, net contribution, equity, TWR, NAV index, peak, drawdown, leverage, belåningsgrad, de-lever status); add a snapshot |
| `/config.html` (Settings) | Portfolio config: leverage target/floor, glide bands, de-lever thresholds, belåningsgrad cliff, currency / account label, **FX rates (USD/EUR→SEK) — editable, with as-of + source** |

## CLI

```bash
# Web dashboard
alphaos serve --host 0.0.0.0 --port 8503

# Database
alphaos db upgrade   # apply Alembic migrations (create/update tables)
alphaos db seed      # seed the five default sleeves (idempotent)
alphaos db current   # show the current Alembic revision

# Market data
alphaos fx refresh       # fetch USD/EUR→SEK from Riksbank (ECB fallback), cache in config
alphaos prices refresh   # pull latest US-stock daily closes from MinIO (stocks-us)
```

## API endpoints (JSON)

| Endpoint                          | Purpose |
|-----------------------------------|---------|
| `GET /api/health`                 | Health probe (`{"ok": true}`) — used by k8s probes |
| `GET /api/status`                 | DB connectivity / diagnostics |
| `GET /api/risk`                   | Current risk: latest snapshot, drawdown, leverage, belåningsgrad, de-lever status, glide-path target |
| `GET /api/allocation`             | Sleeves with target/current weight, drift, rebalance delta, holdings, totals, unassigned |
| `GET /api/holdings`               | List holdings (optionally filtered by sleeve) |
| `POST /api/holdings`              | Create / update a holding |
| `DELETE /api/holdings/{id}`       | Delete a holding |
| `GET /api/transactions`           | List ledger transactions (optionally filtered by `?isin=`) |
| `POST /api/transactions`          | Add a manual transaction (inserts + recomputes that ISIN's holding) |
| `DELETE /api/transactions/{id}`   | Delete a transaction (removes + recomputes the affected holding) |
| `GET /api/history?isin=`          | Per-position history for an ISIN: each buy/sell with the **running quantity** over time |
| `GET /api/nav`                    | NAV snapshots (the ledger) + current risk |
| `POST /api/nav`                   | Add a NAV snapshot |
| `GET /api/sleeves`                | List sleeves |
| `POST /api/sleeves`               | Upsert a sleeve (e.g. edit a target weight) |
| `GET /api/config`                 | Portfolio config singleton |
| `POST /api/config`                | Update portfolio config fields (incl. manual FX rates) |
| `POST /api/import/transactions`   | Import an Avanza transaktioner CSV into the ledger (idempotent: replaces the file's `avanza` date range, preserves manual rows, recomputes holdings); `?preview` parses only and returns the preview without writing |
| `POST /api/fx/refresh`            | Fetch USD/EUR→SEK (Riksbank, ECB fallback) and cache in config |
| `POST /api/prices/refresh`        | Pull latest US-stock daily closes from MinIO and set holding prices |

All responses are JSON. Endpoints that touch the database return `503` when no
database is configured.

## File layout

```
alphaos/
├── __init__.py
├── server.py            # FastAPI backend (static + JSON API)
├── cli.py               # CLI: serve / db upgrade|seed|current / fx refresh / prices refresh
├── db/                  # PostgreSQL layer
│   ├── __init__.py      # session_scope, have_database, db_status, models/enums
│   ├── engine.py        # connection from env (DATABASE_URL / PG*), psycopg3
│   ├── models.py        # Sleeve, Holding, Transaction, NavSnapshot, PortfolioConfig + enums (TransactionKind, TxnSource)
│   ├── config.py        # config singleton + glide-path target_leverage() + FX fields
│   ├── allocation.py    # sleeves / holdings CRUD + allocation() drift report (qty/avg/cost derived)
│   ├── transactions.py  # ledger: list / add / delete / position_history / recompute_holdings
│   ├── nav.py           # add_snapshot / latest / list / current_risk (TWR, DD)
│   ├── fx.py            # refresh_fx / fetch_rates / fx_to_sek (Riksbank, ECB fallback)
│   ├── pricing.py       # MinIO closes: have_credentials / latest_closes / refresh_prices
│   ├── importer.py      # Avanza CSV: parse_avanza_csv (preview) / import_transactions
│   └── serialize.py     # jsonable + *_to_dict serializers
├── web/
│   ├── index.html       # Overview / Risk
│   └── static/css,js/
├── tests/
├── requirements.txt
└── README.md
```

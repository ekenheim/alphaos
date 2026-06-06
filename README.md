# AlphaOS — V2-FRONTIER portfolio tracker

Self-contained Python package that tracks the **V2-FRONTIER** portfolio: a
leveraged, multi-sleeve allocation run on an **Avanza ISK** account in **SEK**.
It is a FastAPI + static HTML/CSS/JS dashboard backed by a **PostgreSQL** store
that holds the sleeve allocation, the holdings, the portfolio config (risk /
leverage parameters) and the **NAV-index ledger** used to drive the binding
de-lever rule.

This is a **tracker and risk cockpit**, not a trading engine. It does not fetch
market data, place orders, or run backtests — you record holdings and NAV
snapshots, and it computes allocation drift, time-weighted return, drawdown and
the resulting leverage / de-lever posture.

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
| `/holdings.html`     | All holdings per sleeve (symbol, ISIN, asset class, quantity, market value, weight); add / edit / delete |
| `/nav.html`          | **NAV ledger**: every snapshot (gross, loan, net contribution, equity, TWR, NAV index, peak, drawdown, leverage, belåningsgrad, de-lever status); add a snapshot |
| `/config.html` (Settings) | Portfolio config: leverage target/floor, glide bands, de-lever thresholds, belåningsgrad cliff, currency / account label |

## CLI

```bash
# Web dashboard
alphaos serve --host 0.0.0.0 --port 8503

# Database
alphaos db upgrade   # apply Alembic migrations (create/update tables)
alphaos db seed      # seed the five default sleeves (idempotent)
alphaos db current   # show the current Alembic revision
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
| `GET /api/nav`                    | NAV snapshots (the ledger) + current risk |
| `POST /api/nav`                   | Add a NAV snapshot |
| `GET /api/sleeves`                | List sleeves |
| `POST /api/sleeves`               | Upsert a sleeve (e.g. edit a target weight) |
| `GET /api/config`                 | Portfolio config singleton |
| `POST /api/config`                | Update portfolio config fields |

All responses are JSON. Endpoints that touch the database return `503` when no
database is configured.

## File layout

```
alphaos/
├── __init__.py
├── server.py            # FastAPI backend (static + JSON API)
├── cli.py               # CLI: serve / db upgrade|seed|current
├── db/                  # PostgreSQL layer
│   ├── __init__.py      # session_scope, have_database, db_status, models/enums
│   ├── engine.py        # connection from env (DATABASE_URL / PG*), psycopg3
│   ├── models.py        # Sleeve, Holding, NavSnapshot, PortfolioConfig + enums
│   ├── config.py        # config singleton + glide-path target_leverage()
│   ├── allocation.py    # sleeves / holdings CRUD + allocation() drift report
│   ├── nav.py           # add_snapshot / latest / list / current_risk (TWR, DD)
│   └── serialize.py     # jsonable + *_to_dict serializers
├── web/
│   ├── index.html       # Overview / Risk
│   └── static/css,js/
├── tests/
├── requirements.txt
└── README.md
```

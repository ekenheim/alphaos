# AlphaOS — multi-asset levels/momentum research engine + live ledger

Self-contained Python package: backtest engine, multi-asset levels & breakout
research, FastAPI + static HTML/CSS/JS dashboard, MinIO/S3 historical-data
loader with yfinance fallback, a **Postgres-backed live ledger** (positions +
executions/rebalances) for forward-testing strategies before any real money, and
a **strategy/backtest archive** that persists every saved run.

> **Research surface, not investment advice.** Strategies in this repo are
> backtested but **not** PLACEBO-validated as of the latest research log
> entry. See `Research log` section below for the kill record. Anything that
> touches capital has to clear walk-forward + PLACEBO first.

## Install (standalone)

The package lives in this folder and can be lifted out as its own repo:

```bash
# inside the alphaos/ directory
pip install -e .[dev]      # editable install
cp .env.example .env       # optional — fill in MinIO creds if you have them
python -m alphaos.cli serve  # -> http://127.0.0.1:8503
```

Or without an editable install:

```bash
pip install -r requirements.txt
python -m alphaos.cli serve
```

Python 3.10+. yfinance is the default data backend (free, ~60-day cap on 5m);
drop MinIO credentials into `.env` to get 10-year intraday history via the
hive-partitioned `bars/tf=*/date=*/part.parquet` layout.

## Pages

| URL                   | What it shows                                                            |
|-----------------------|--------------------------------------------------------------------------|
| `/`                   | META-EA hero ($ net profit), CAGR/Sharpe/PF/DD KPIs, equity curve, monthly P&L heatmap, trades-by-symbol pie, per-instrument stats table |
| `/strategies.html`    | KPI grid, Stand-alone vs Under-prop-firm comparison cards, per-instrument breakdown table |
| `/accounts.html`      | 7-tile KPI strip (ACCOUNTS / BLOW-UPS / ATTEMPTS / TOTAL COST / PAYOUTS / TOTAL PAID OUT / NET PROFIT) + ROI / FINAL PHASE, monthly cashflow bar chart (payouts vs challenge fees), account list table |
| `/ledger.html`        | **Live ledger** (Postgres-backed): open/closed positions with qty, avg entry, realized P&L; per-position execution history; forms to **record an execution** (open/add/trim/close) and to **rebalance** a basket in one batch |
| `/archive.html`       | **Strategy & backtest archive**: strategy catalog (slug/name/status/params) and the persisted backtest runs per strategy (n_trades, win%, Sharpe, max DD, CAGR, total R, PLACEBO pass) |

## Trading style implemented

AlphaOSTrader-style high-RRR algorithmic EA + common-denominator levels setups:

| Setup                    | Trigger |
|--------------------------|---------|
| `alphaos_ea` *(default)*   | 3-EMA stack uptrend + ATR(14) > 1.10 × ATR(50) + close > rolling 50-bar high (fresh break) + 24-bar cooldown. Pairs with target 6 ATR + trailing 1.5 ATR after +2R → high RRR / low win-rate profile |
| `orb_break`              | First close above opening-range high with volume confirmation |
| `pdh_reclaim`            | Reclaim of prior-day high after a dip below it |
| `vwap_reclaim`           | Reclaim of session VWAP from below, before PDH is tagged |
| `momentum_continuation`  | Higher-high after a contained pullback inside trend |

All signals are **causal** (filtered): at bar `t`, only data through `t` is
referenced. Entries fill at bar `t+1` open + slippage. Verified by tests.

## CLI

```bash
# Web dashboard
python -m alphaos.cli serve --port 8503

# Single-symbol scan
python -m alphaos.cli scan US100 --setup momentum_continuation --interval 1h

# Single-symbol backtest with PLACEBO gate
python -m alphaos.cli backtest XAUUSD --setup momentum_continuation --interval 1h --placebo 200

# ...and persist the run to the Postgres backtest archive
python -m alphaos.cli backtest XAUUSD --setup momentum_continuation --interval 1h --placebo 200 --save

# Database (Postgres ledger + archive)
alphaos db upgrade   # apply Alembic migrations (create/update tables)
alphaos db seed      # populate the strategy catalog from SETUPS (idempotent)
```

The live ledger and strategy/backtest archive live in **PostgreSQL**. Configure
the connection via `DATABASE_URL` (or `PG*` parts) and run `alphaos db upgrade`
once before first use — see [`DEPLOYMENT.md`](DEPLOYMENT.md) for the full setup
(incl. the Crunchy/CPNG secret mapping and an initContainer example).

## API endpoints (JSON)

| Endpoint            | Purpose |
|---------------------|---------|
| `GET /api/health`     | Health probe |
| `GET /api/portfolio`  | META-EA KPIs, equity curve, monthly heatmap, trades-by-symbol |
| `GET /api/strategies` | Per-instrument stats array |
| `GET /api/propfirm`   | Simulated prop-firm portfolio (KPIs, cashflow, account list) |

Backend cache: portfolio + sim are recomputed once every 5 minutes; first hit
takes ~10-30s to fetch data and run the multi-symbol backtest.

## Research log

### 2026-06-06 — intraday 5min EOD-flat on liquid ETFs — KILLED

**Setup.** Pivot per operator clarification: target is Avanza/IBKR paper-trade
first, pure intraday 5-15min close-by-EOD. Backtest engine extended with
`eod_flat`, `session_hours_utc`, `no_entry_after_utc`. Sharpe computation
fixed to use log returns with `1e-6` equity floor (the old `pct_change` form
exploded to +13 Sharpe with -99% DD).

**Tests.** `orb_break` + `vwap_reclaim` × 4 ETFs (QQQ/GLD/EWJ/DIA) at
`stop=1, target=3, no-trail, EOD-flat, no entry after 14:30 ET`, costs
`0.5 bps + 10% bar slip`:

| sym    | setup        | n    | win% | PF   | DD     |
|--------|--------------|-----:|-----:|-----:|-------:|
| US100  | orb_break    | 434  | 21%  | 0.60 | -82 %  |
| US100  | vwap_reclaim | 945  | 20%  | 0.54 | -99 %  |
| XAUUSD | orb_break    | 434  | 23%  | 0.60 | -80 %  |
| XAUUSD | vwap_reclaim | 761  | 25%  | 0.69 | -90 %  |
| JP225  | orb_break    | 321  | 27%  | 0.77 | -50 %  |
| JP225  | vwap_reclaim | 777  | 25%  | 0.68 | -90 %  |
| US30   | orb_break    | 337  | 26%  | 0.80 | -49 %  |
| US30   | vwap_reclaim | 1004 | 20%  | 0.55 | -99 %  |

All PF < 1.0. PLACEBO "PASS" labels are meaningless when both real and
placebo Sharpe are tiny-negative (the test only discriminates with margin).
Intraday breakouts on liquid US ETFs have no edge after realistic costs.

### 2026-06-06 — intraday 5min EOD-flat on single high-vol stocks — KILLED

**Hypothesis.** ORB anomaly lives on individual high-vol stocks per academic
literature, not ETFs (Akbas/Boehmer 2018, etc.). Pulled NVDA/TSLA/MU/AMD/PLTR
5min from MinIO. Same setup × params as above.

| sym  | setup        | n   | win% | PF   | DD    | totR  |
|------|--------------|----:|-----:|-----:|------:|------:|
| MU   | orb_break    | 308 | 28%  | 0.89 | -34%  | -27.2 |
| PLTR | orb_break    | 305 | 25%  | 0.78 | -48%  | -57.0 |
| NVDA | orb_break    | 345 | 26%  | 0.76 | -62%  | -71.0 |
| TSLA | orb_break    | 330 | 25%  | 0.73 | -60%  | -77.1 |
| AMD  | orb_break    | 331 | 24%  | 0.70 | -62%  | -86.1 |
| (vwap_reclaim all worse, PF 0.57-0.71) | | | | | |

Best is MU PF 0.89 — close to flat but still losing. **Intraday breakouts on
single high-vol stocks ALSO have no edge** at these params after costs.

Honest read: the ORB anomaly in the literature uses survivorship-biased
samples, equal-weight portfolios, lower-cost windows, or different exit
rules. AlphaOSTrader's screenshots are likely explained by prop-firm
leverage + survivorship + curated windows — not strategy edge.

### 2026-06-06 — fade-the-breakout (SHORT on signal) — KILLED

Hypothesis: at 25% win rate, 75% of "breakouts" reverse. Shorting the
breakout might invert the edge (in theory PF ≈ 1/0.89 ≈ 1.12 for MU).

Result on NVDA (representative): orb_break SHORT → PF 0.66 (was 0.76 long);
vwap_reclaim SHORT → PF 0.76 (was 0.71 long). Fade is WORSE than long. The
cost model is symmetric but the slight intraday upward drift in equity
markets (~+5 bps/day on average) hurts shorts.

**Conclusion: intraday 5min EOD-flat in either direction has no edge on
liquid US equity/ETF instruments after IBKR-style costs.**

### Next research axes (queued — not started)

This iteration confirms the intraday-EOD-flat axis is exhausted. To make
the system actually work for the operator, try in priority order:

1. **Daily-close swing (1-3 day hold)** on UCITS-tradable ETFs. Matches the
   operator's existing V2-FRONTIER cadence and Avanza cost structure.
   The operator's own memory documents Ariel-style daily-close at +7.5%
   CAGR on top-500 US universe — there's known edge at this cadence.
2. **Cross-sectional momentum** — rank top-500 by 12-month return, hold
   top decile, rebal weekly. Standard well-validated alpha.
3. **Regime / volatility indicator** — use AlphaOS signals not to TRADE but
   to time V2-FRONTIER's de-lever rule (intraday vol → next-day lev).
4. **Different setup entirely** — e.g. earnings-surprise drift, post-event
   momentum, gap-and-go on individual stocks at open.

The live (Postgres) ledger + dashboard + MinIO loader are reusable for any
of these axes.

### 2026-06-06 — MinIO 5min→1h, multi-symbol, no-trail discovery — POSITIVE / NOT PLACEBO-VALIDATED

**Setup.** Wired the MinIO loader (`alphaos/minio.py`) into the data path
(`ALPHAOS_USE_MINIO=1`). Pulled QQQ / GLD / EWJ / DIA 5min for 2024-01 → 2026-05
from `bars/tf=5min/date=*/part.parquet`, resampled to 1h. Per-symbol cache:
~100k bars each (vs yfinance's ~6k cap).

Run alphaos_ea against each ETF as a AlphaOSTrader-instrument proxy:
US100→QQQ, XAUUSD→GLD, JP225→EWJ, US30→DIA.

**Default (trail_atr=1.5 active after +2R) results:**

| symbol | trades | win % | PF   | DD     | PLACEBO    |
|--------|-------:|------:|-----:|-------:|-----------:|
| US100  | 31     | 32 %  | 0.56 | -14 %  | FAIL       |
| XAUUSD | 59     | 14 %  | 0.18 | -38 %  | (Sharpe-PASS but PF wrong) |
| JP225  | 43     | 40 %  | 0.85 | -12 %  | FAIL       |
| US30   | 36     | 39 %  | 0.68 | -12 %  | FAIL       |

XAUUSD passing PLACEBO on Sharpe while losing money flagged a hypothesis:
**the entry signal has information, but the trailing stop is destroying it.**

**Iteration: drop trailing stop. `stop=2.0, target=6.0, trail=None` across the board:**

| symbol | trades | win % | PF   | Δ PF  | DD     | PLACEBO |
|--------|-------:|------:|-----:|------:|-------:|--------:|
| US100  | 25     | 36 %  | **1.28** | +0.72 | -7 %   | FAIL    |
| XAUUSD | 50     | 38 %  | **1.64** | +1.46 | -20 %  | FAIL    |
| JP225  | 38     | 26 %  | 0.94 | +0.09 | -8 %   | FAIL    |
| US30   | 32     | 22 %  | 0.79 | +0.11 | -8 %   | FAIL    |

Every symbol improved. The trail stop was eating edge across the board.

**Walk-forward parameter sweep on XAUUSD only** (60/40 split):

- Train (1.4 yr): best `stop=2.5 target=10 trail=None` → PF 2.01
- Val (1 yr, train-chosen params): **PF 2.82**, WR 43.8 %, totR +17.84
- Val (default trail params): PF 0.20, totR −18.07 (huge gap)
- Val PLACEBO (sticky-Markov Sharpe): real +0.60, P95 +0.70 → **FAIL at 85th percentile**

Magnitude is real (PF 2.82, +17.84 R from 16 trades) but n=16 isn't enough
to clear P95 Sharpe gate. Per CLAUDE.md: **NOT a validated alpha**, but
the strongest signal this iteration has found.

**Action.** Default params changed to `stop=2.0, target=6.0, trail=None`
(strictly Pareto-better on the available data, doesn't claim PLACEBO PASS).
GLD-specific `stop=2.5 target=10 trail=None` saved in research log; needs
2x more history (8 yrs from MinIO) before deployment talk.

### 2026-06-06 — walk-forward bucket filter for alphaos_ea — WEAK / KILLED

**Hypothesis.** Slice the unfiltered backtest by instrument × hour-of-day × DOW.
Drop instruments / hours / DOWs whose train-half profit factor is < 0.6 with
n >= 8. Walk forward: discover on 60% train, blindly score on 40% val.

**Result.**

| stage              | trades | win % | PF   | total R |
|--------------------|-------:|------:|-----:|--------:|
| train baseline     | 147    | 32 %  | 0.68 | −35.51  |
| val baseline       | 87     | 41 %  | 1.04 | +2.24   |
| val with filter    | 28     | 46 %  | 1.47 | +7.74   |

Walk-forward gate: **PASS** (+0.43 PF, +5.5 R).

**PLACEBO.** Random-drop test: sample 28 of the 87 val trades at random,
compute PF, N=500 sims. Median random PF = 0.99, **P95 = 1.81**. Real filter
PF 1.47 ranks at the **84th percentile** — beats median but not P95.

**Per CLAUDE.md** ("Rule must beat P95 of placebo distribution to count as
real signal") this is **KILLED**.

What was discovered (kept under `apply_walk_forward_filter=True` for
research/inspection only — not the default):

- dropped instruments: USDJPY, BTCUSD (train PF 0.35 and 0.45)
- dropped hours UTC: 6, 12, 22, 23
- dropped DOWs: Tue, Sat, Sun

Run `python -c "from alphaos.research import walk_forward_filter_discovery; print(walk_forward_filter_discovery())"`
to reproduce.

**Next ideas** that did NOT get tried this iteration (queue):

- Per-instrument parameter tuning (one knob set per asset class)
- News/event blackout filter (NFP, FOMC, CPI)
- Trend-strength filter (slope of EMA200)
- Pyramid-add-to-winners (single position → 2-3 stacked entries on continuation)

## Discipline gates (project rule, see CLAUDE.md)

This module honors the regime/timing backtest discipline:

1. **Causal-only signals.** No look-ahead. Verified by `tests/test_setups.py`.
2. **Walk-forward** — available via `cli backtest` (60/40 train/val split).
3. **PLACEBO** — sticky-Markov binary timers matched on firing frequency AND
   autocorrelation. Real Sharpe must beat P95 of placebo distribution.
   `cli backtest --placebo 200`.

A setup that fails PLACEBO is **not real signal** at the tested cadence. Do
not deploy.

## File layout

```
alphaos/
├── __init__.py
├── data.py             # OHLCV loader, symbol registry (incl. US100/XAUUSD/USDJPY/JP225)
├── levels.py           # PDH/PDL/PDC, ORH/ORL, VWAP, HOD/LOD (all causal)
├── setups.py           # 4 setup detectors -> bool Series
├── backtest.py         # vectorized event-driven backtest + metrics
├── placebo.py          # sticky-Markov placebo baseline
├── portfolio.py        # multi-instrument META-EA + prop-firm account simulator
├── db/                 # Postgres layer: engine, models, ledger + archive services
│   ├── __init__.py     # session_scope, have_database, get_engine, models/enums
│   ├── engine.py       # connection from env (DATABASE_URL / PG*), psycopg3 driver
│   ├── models.py       # Strategy, Backtest, Position, TradeEvent + enums
│   ├── ledger.py       # record_execution / rebalance / positions / summary
│   └── archive.py      # upsert/seed strategies, save/list backtests, performance
├── server.py           # FastAPI backend (static + JSON API + cache)
├── cli.py              # CLI: scan / backtest / serve / db upgrade|seed
├── web/
│   ├── index.html           # overview page
│   ├── strategies.html      # strategy comparison page
│   ├── accounts.html        # prop accounts page
│   └── static/
│       ├── css/style.css    # dark prop-firm theme
│       └── js/
│           ├── common.js, overview.js, strategies.js, accounts.js
├── tests/
│   └── test_setups.py  # causality, sanity, sticky-Markov tests
├── requirements.txt
└── README.md
```

## Longer history via parquet (bypass yfinance)

yfinance caps intraday history. To use your own parquet store
(MinIO export, broker dump, anything):

1. **Set the directory** (one of):
   - Env var `ALPHAOS_PARQUET_DIR=/path/to/your/parquets`
   - Or drop files into `alphaos/data_cache/parquet/` (default fallback)

2. **One file per (symbol, interval)** named `{SYMBOL}_{INTERVAL}.parquet`
   - e.g. `US100_1h.parquet`, `XAUUSD_5m.parquet`

3. **Schema** — DatetimeIndex (UTC) or any of `ts_utc` / `timestamp` / `date` / `datetime` columns; columns `open`, `high`, `low`, `close`, `volume` (case-insensitive)

Loader priority is parquet → yfinance cache → yfinance live. Drop a parquet
in and the dashboard automatically uses it on next cache refresh (5 min TTL).

## Honest caveats

- yfinance is the data backend: 1m data ~7 days, 5m ~60 days, 1h ~730 days.
  Default is `1h` for the META-EA portfolio because it gives the longest
  history without subscription data.
- `JP225` (`^N225`) doesn't return intraday from yfinance — it gets skipped
  in the META-EA portfolio. Other 4 instruments populate fully.
- The prop-firm simulator is a simplified model: monthly payouts above a
  threshold, hard blow-up at max-DD. Real prop firms have more nuanced rules
  (consistency, news lockouts, minimum trading days, etc.).
- The four setups are an opinionated common-denominator interpretation of
  levels trading. They're a starting point, not a literal reproduction of any
  specific trader's system. Replace the detector in `setups.py` with the real
  strategy you want to trade.
- Strategy alpha is honestly evaluated — you'll see negative numbers when the
  setup has no edge on a given window. That's the discipline working.

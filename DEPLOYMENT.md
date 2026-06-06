# Building & shipping the AlphaOS image

This repo produces the **AlphaOS container image** and publishes it to **GitHub
Container Registry (GHCR)**. Kubernetes manifests live in a separate k8s/GitOps
repo — pull this image there and fill in your vars.

Image: **`ghcr.io/ekenheim/alphaos`**

## What the image is

A FastAPI app (`alphaos.server:app`) serving the **V2-FRONTIER portfolio
tracker** — a web dashboard + JSON APIs on port **8503**. All state (sleeves,
holdings, portfolio config, NAV-index ledger) lives in **PostgreSQL** (see
_Database (Crunchy Postgres)_ below).

> **MinIO / S3 is used again — but only read-only, and only for daily closes of
> US stocks** (bucket `stocks-us`). It is **optional**: the app works without it
> (US stocks simply stay at cost / a manual price). There is still no OHLCV
> loader, no parquet cache, and no `data_cache` PVC. The only **required**
> external dependency is the Postgres database.
>
> The app also fetches **FX rates** (USD/EUR→SEK) over outbound HTTPS from the
> Riksbank (ECB fallback). This is optional too — rates are cached in the DB and
> can be set by hand in Settings if the cluster has no egress.

- Entry: `python -m alphaos.cli serve --host 0.0.0.0 --port 8503` (the image CMD).
  The CLI default host is `127.0.0.1` — **must** be `0.0.0.0` in a container/pod.
- Health: `GET /api/health` → `{"ok": true}` (Dockerfile HEALTHCHECK + k8s probes).
- Runs as non-root (uid **10001**); the root filesystem can be **read-only** —
  the app holds no local state, everything is in Postgres.

## Publishing the image (CI — primary path)

`.github/workflows/build-image.yml` builds and pushes to GHCR automatically:

- on every push to `main` → tags `latest` + `sha-<short>`
- on git tags `v*` → semver tags (e.g. `v0.1.0`)
- manually via the **Run workflow** button (`workflow_dispatch`)

It authenticates with the built-in `GITHUB_TOKEN` (no secrets to configure).
After the first successful run the image appears under the repo's **Packages**.

> By default a new GHCR package is **private**. To let your cluster pull it
> without credentials, make it public: GitHub → repo → Packages → `alphaos` →
> Package settings → Change visibility → Public. Otherwise create an image-pull
> Secret in your k8s repo using a PAT with `read:packages`.

Pull it:

```bash
docker pull ghcr.io/ekenheim/alphaos:latest
```

## Building locally (optional)

```bash
docker build -t ghcr.io/ekenheim/alphaos:dev .
docker run --rm -p 8503:8503 \
  -e DATABASE_URL=postgresql://user:pass@host:5432/alphaos \
  ghcr.io/ekenheim/alphaos:dev
# then: curl http://localhost:8503/api/health  -> {"ok": true}
```

The app needs a reachable Postgres to serve data; without `DATABASE_URL` / `PG*`
the process still starts and `/api/health` responds, but the data endpoints
return `503` until a database is configured.

## Runtime configuration (set these in your k8s repo)

The container is configured entirely via environment variables. The only
**required** configuration is the **database connection**; the **MinIO** vars
(below) are optional and only enable US-stock price refresh.

## Database (Crunchy Postgres)

The app needs a **PostgreSQL** database. It holds the entire portfolio state:
sleeves, holdings, the portfolio config singleton, and the NAV-index ledger.

The image already bundles the required deps (`sqlalchemy`,
`psycopg[binary]`, `alembic`) — they're declared in `pyproject.toml` /
`requirements.txt`, nothing extra to install.

### Connection (env)

Configure the connection with **either** form:

| Variable | Notes |
|---|---|
| `DATABASE_URL` | full connection URL, e.g. `postgresql://user:pass@host:5432/dbname` |

…or the discrete `PG*` parts:

| Variable | Notes |
|---|---|
| `PGHOST` | host |
| `PGPORT` | port (default `5432`) |
| `PGUSER` | user |
| `PGPASSWORD` | password |
| `PGDATABASE` | database name |

`ALPHAOS_DATABASE_URL` is also accepted and takes precedence over
`DATABASE_URL`. The driver is **normalized to psycopg3 automatically**
(`postgresql://` / `postgres://` → `postgresql+psycopg://`), so a raw Crunchy
(CPNG) `uri` value works as-is — no rewriting needed.

### Mapping the Crunchy (CPNG) secret

Crunchy Postgres (CPNG) publishes a Secret named
`<cluster>-pguser-<user>` with keys: `host`, `port`, `dbname`, `user`,
`password`, and a ready-to-use `uri`. Map it in your k8s repo either way.

**Simplest — map the `uri` to `DATABASE_URL`:**

```yaml
env:
  - name: DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: alphaos-db-pguser-alphaos   # <cluster>-pguser-<user>
        key: uri
```

**Or map the discrete parts:**

```yaml
env:
  - name: PGHOST
    valueFrom: { secretKeyRef: { name: alphaos-db-pguser-alphaos, key: host } }
  - name: PGPORT
    valueFrom: { secretKeyRef: { name: alphaos-db-pguser-alphaos, key: port } }
  - name: PGUSER
    valueFrom: { secretKeyRef: { name: alphaos-db-pguser-alphaos, key: user } }
  - name: PGPASSWORD
    valueFrom: { secretKeyRef: { name: alphaos-db-pguser-alphaos, key: password } }
  - name: PGDATABASE
    valueFrom: { secretKeyRef: { name: alphaos-db-pguser-alphaos, key: dbname } }
```

> The CPNG secret key names match both forms, so `envFrom` straight off the
> Secret also populates `host`/`port`/`user`/`password`/`dbname`/`uri` — but the
> app reads `PG*` / `DATABASE_URL`, so prefer the explicit `secretKeyRef`
> mapping above (or an `envFrom` plus the `DATABASE_URL <- uri` alias).

### Migrations & seeding

Schema is managed with Alembic. Run the migrations **before** the app starts,
then seed the sleeves:

```bash
alphaos db upgrade   # apply Alembic migrations (creates/updates tables)
alphaos db seed      # populate the five default V2-FRONTIER sleeves (idempotent)
```

The recommended place for this in your k8s repo is an **initContainer** that
shares the same image and the same DB env as the main container:

```yaml
initContainers:
  - name: db-migrate
    image: ghcr.io/ekenheim/alphaos:latest
    command: ["alphaos", "db", "upgrade"]
    env:
      - name: DATABASE_URL
        valueFrom:
          secretKeyRef:
            name: alphaos-db-pguser-alphaos
            key: uri
  # second init step to seed the sleeves (idempotent):
  - name: db-seed
    image: ghcr.io/ekenheim/alphaos:latest
    command: ["alphaos", "db", "seed"]
    env:
      - name: DATABASE_URL
        valueFrom:
          secretKeyRef:
            name: alphaos-db-pguser-alphaos
            key: uri
```

`alphaos db seed` is idempotent, so it's safe to run on every rollout.

## Market data (optional)

### MinIO / S3 — US-stock daily closes (read-only)

The app can mark **US stocks** to their latest **daily close**, read **read-only**
from a MinIO/S3 bucket (`stocks-us`, latest daily partition). This is **entirely
optional**: if these vars are unset (or the bucket is unreachable), price refresh
is a no-op and US stocks simply stay at cost or at a manually entered price.

| Variable | Notes |
|---|---|
| `MINIO_ENDPOINT_URL` | S3 endpoint URL, e.g. `https://minio.example.com` |
| `MINIO_ACCESS_KEY_ID` | access key (read-only credential is sufficient) |
| `MINIO_SECRET_ACCESS_KEY` | secret key |
| `MINIO_BUCKET` | bucket holding the closes (default `stocks-us`) |

Refresh prices on demand with `alphaos prices refresh` (or `POST
/api/prices/refresh`); this sets `last_price` / `last_price_date` with
`price_source=minio`. Nothing is ever written back to the bucket. The
`data_cache` PVC from older versions is **still not needed**.

### FX rates — outbound internet (optional)

FX refresh (`alphaos fx refresh` / `POST /api/fx/refresh`) makes outbound HTTPS
calls to **`api.riksbank.se`** (primary) and **`ecb.europa.eu`** (fallback) to
fetch USD/EUR→SEK. Rates are **cached in the Postgres config**, so this only
needs egress at refresh time. **If the cluster has no outbound internet**, leave
FX refresh unused and **set the rates by hand in Settings** — the cached values
are used for all market-value math.

## Future: LAN registry

To later publish to an internal registry (e.g. `registry.ekenhome.se/alphaos`)
instead of / in addition to GHCR, note a LAN registry is **not reachable from
GitHub-hosted runners** — that build/push must run from inside the LAN (a
self-hosted runner or a local `docker build && docker push`).

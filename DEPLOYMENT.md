# Building & shipping the AlphaOS image

This repo produces the **AlphaOS container image** and publishes it to **GitHub
Container Registry (GHCR)**. Kubernetes manifests live in a separate k8s/GitOps
repo — pull this image there and fill in your vars.

Image: **`ghcr.io/ekenheim/alphaos`**

## What the image is

A FastAPI app (`alphaos.server:app`) serving a web dashboard + JSON APIs on port
**8503**. It reads US market data from MinIO/S3 and writes a runtime parquet
cache under `alphaos/data_cache/`.

- Entry: `python -m alphaos.cli serve --host 0.0.0.0 --port 8503` (the image CMD).
  The CLI default host is `127.0.0.1` — **must** be `0.0.0.0` in a container/pod.
- Health: `GET /api/health` → `{"ok": true}` (Dockerfile HEALTHCHECK + k8s probes).
- Runs as non-root (uid **10001**); root filesystem can be read-only **if** you
  mount a writable volume at `/app/alphaos/data_cache` (cache is created at import).

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
  -e MINIO_ACCESS_KEY_ID=... -e MINIO_SECRET_ACCESS_KEY=... \
  ghcr.io/ekenheim/alphaos:dev
# then: curl http://localhost:8503/api/health  -> {"ok": true}
```

Without MinIO credentials the app still starts and falls back to yfinance for data.

## Runtime configuration (set these in your k8s repo)

The container is configured entirely via environment variables.

**MinIO / S3 credentials (secret — never commit):**

| Variable | Alias | Notes |
|---|---|---|
| `MINIO_ENDPOINT_URL` | `MINIO_URL` | default `http://s3-lan.ekenhome.se:9000` |
| `MINIO_ACCESS_KEY_ID` | `MINIO_USERNAME` | access key |
| `MINIO_SECRET_ACCESS_KEY` | `MINIO_PASSWORD` | secret key |
| `MINIO_BUCKET` | `S3_BUCKET` | default `stocks-us` |

**Optional toggles:** `ALPHAOS_USE_MINIO=1`, `ALPHAOS_PARQUET_DIR=/path`,
`ALPHAOS_ENV_FILE=/path/to/.env`.

## Future: LAN registry

To later publish to an internal registry (e.g. `registry.ekenhome.se/alphaos`)
instead of / in addition to GHCR, note a LAN registry is **not reachable from
GitHub-hosted runners** — that build/push must run from inside the LAN (a
self-hosted runner or a local `docker build && docker push`).

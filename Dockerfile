# syntax=docker/dockerfile:1
#
# Production image for alphaos (FastAPI dashboard + JSON APIs).
# Build:  docker build -t ghcr.io/ekenheim/alphaos:latest .
# Run:    docker run -p 8503:8503 --env-file .env ghcr.io/ekenheim/alphaos:latest
#
FROM python:3.12-slim

# --- runtime env -----------------------------------------------------------
# PYTHONUNBUFFERED      -> logs flush immediately (good for k8s log tailing)
# PYTHONDONTWRITEBYTECODE -> no .pyc files written into the source tree
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# --- dependency layer (cache-friendly) -------------------------------------
# Copy only the dependency manifests first so this expensive layer is reused
# whenever application source changes but dependencies do not.
# requirements.txt is kept in exact sync with pyproject [project.dependencies],
# so installing it here pre-populates the dep set; the later `pip install .`
# then only needs to build/install the package itself.
COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --- application layer -----------------------------------------------------
# Copy the full repo, then install the package from pyproject (source of truth
# for the complete dependency list). Deps are already satisfied from the layer
# above, so this resolves quickly while still using pyproject as authoritative.
COPY . .
RUN pip install --no-cache-dir .

# --- non-root runtime user -------------------------------------------------
# The app writes no local files (all state lives in Postgres), so the root
# filesystem can be read-only at runtime. Still run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Run from /app so `python -m alphaos.cli` resolves the writable source tree
# (cwd is on sys.path for `-m`). WORKDIR already set to /app above.

EXPOSE 8503

# Health probe without curl (not installed in slim). Exit 0 only when the
# endpoint returns HTTP 200 with {"ok": true}.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8503/api/health', timeout=4).status == 200 else 1)"]

# Default host is 127.0.0.1 in the CLI; MUST override to 0.0.0.0 in container.
CMD ["python", "-m", "alphaos.cli", "serve", "--host", "0.0.0.0", "--port", "8503"]

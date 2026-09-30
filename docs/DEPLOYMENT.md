# Deployment

Targets: Apple Silicon Mac, Intel Mac (Docker Desktop), Linux x86_64, Linux ARM64. The image is plain
`python:3.11-slim` (multi-arch); no platform-specific binaries. Trading-platform bridges that require Windows
(e.g. MT5's Python API) are **not** part of this image (see ADR-0005).

## 1. Docker Compose (recommended)

```bash
git clone <repo> propguard && cd propguard
cp .env.example .env
python3 -c "import secrets;print(secrets.token_hex(16))"      # → POSTGRES_PASSWORD
python3 -c "import secrets;print(secrets.token_urlsafe(32))"  # → PROPGUARD_API_TOKEN
chmod 600 .env
docker compose up -d
docker compose ps           # db, api, worker → healthy
curl -s 127.0.0.1:8000/health
```

On start the API container runs `propguard db upgrade` (Alembic) and loads `seed/firms` (idempotent;
disable with `PROPGUARD_SEED_ON_START=false`). The worker waits for migrations, then monitors sources.

Behind a TLS-intercepting corporate proxy pass its CA bundle as a build secret (not stored in the image):
`DOCKER_BUILDKIT=1 docker build --secret id=extra_ca,src=/path/ca.crt -t propguard:local .` then `docker compose up -d`.

Verified in this repository's CI-like environment: build, `docker compose up -d`, all three services healthy,
migrations at head on PostgreSQL 16, seeds loaded, `/health` OK, token never in container logs.

### Remote server
Keep the port on 127.0.0.1 and use `ssh -L 8000:127.0.0.1:8000 server`, WireGuard/Tailscale, or a reverse proxy
(Caddy/nginx) with TLS and its own authentication in front of it.

## 2. Local mode (no Docker, SQLite)

```bash
python3.11 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,llm]"
propguard db upgrade && propguard seed load
propguard serve &     # dashboard
propguard worker      # monitoring
```
SQLite (WAL) is fine for one owner; use PostgreSQL for a 24/7 server.

## Migrations
`propguard db upgrade | current | downgrade --revision <rev>`. New revisions: edit models, then
`alembic revision --autogenerate -m "..."` (dev `alembic.ini`), review, commit.

## Backup

PostgreSQL (compose):
```bash
mkdir -p backups
docker compose exec -T db pg_dump -U propguard -Fc propguard > backups/propguard-$(date +%F).dump
gpg -c backups/propguard-$(date +%F).dump && rm backups/propguard-$(date +%F).dump   # encrypt at rest
```
Also back up `.env` (separately, securely) and the `appdata` volume (`acceptance.json`, heartbeat).
SQLite: `sqlite3 data/propguard.db ".backup data/backup.db"`.

## Restore

```bash
docker compose stop api worker
gpg -d backups/propguard-2026-09-29.dump.gpg > /tmp/r.dump
docker compose exec -T db pg_restore -U propguard -d propguard --clean --if-exists < /tmp/r.dump
docker compose start api worker
docker compose exec api propguard audit verify
docker compose exec api propguard db current
```
After restoring into an account that was trading, the next session start reconciles against the platform;
any mismatch raises kill switches until reviewed (by design).

## Upgrades
`git pull && docker compose build && docker compose up -d` (migrations run automatically). Any change to
`risk/`, `execution/` or `rules/` changes the code fingerprint: LIVE approvals are revoked until
`propguard acceptance` passes and the account is re-approved.

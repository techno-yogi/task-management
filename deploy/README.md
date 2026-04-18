# Deployment

This deployment path uses:
- GitHub Actions to build and push the application image to GHCR
- Ansible to connect to the target machine over SSH
- Jinja templates to render `/opt/sweep-app/.env` and `/opt/sweep-app/docker-compose.yml`
- Docker Compose on the target host to run the API, Postgres (TLS-enabled), Redis, and 3 Celery worker tiers

## Quick Start: Local Validation

Before deploying to a real VM, validate the full Ansible flow locally using the
`dev-staging` test harness. This spins up a sshd container that proxies to your
local Docker engine.

```powershell
# 1. Generate SSH keypair + extra-vars (one-time)
pwsh ./deploy/dev-staging/dev_staging.ps1 init

# 2. Build + start the staging container + ansible runner
pwsh ./deploy/dev-staging/dev_staging.ps1 up

# 3. Syntax check + ansible-lint
pwsh ./deploy/dev-staging/dev_staging.ps1 lint

# 4. Dry-run (--check --diff, no side effects)
pwsh ./deploy/dev-staging/dev_staging.ps1 check

# 5. Real deploy
pwsh ./deploy/dev-staging/dev_staging.ps1 apply

# 6. Verify health from host
pwsh ./deploy/dev-staging/dev_staging.ps1 health

# 7. Tear down
pwsh ./deploy/dev-staging/dev_staging.ps1 down
```

The deployed stack runs on:
- API: `http://localhost:8100`
- Postgres: internal only (TLS-enabled, self-signed cert)
- Redis: internal only

## Required GitHub Environment Secrets

Set these on the `staging` and/or `production` GitHub environment. Use the
helper script to bulk-import from an env-file:

```powershell
# 1. Authenticate gh CLI (interactive)
gh auth login

# 2. Bulk-import secrets
pwsh ./scripts/setup_github_secrets.ps1 -EnvFile deploy/secrets.staging.env -Environment staging
```

### Secret List

| Secret | Description |
|--------|-------------|
| `SSH_PRIVATE_KEY` | Ed25519 private key (no passphrase) for the `deploy` user on the target |
| `SSH_HOST` | Target hostname or IP |
| `SSH_KNOWN_HOSTS` | Output of `ssh-keyscan -t ed25519,ecdsa,rsa <host>` (pinned fingerprints) |
| `APP_ENV` | `staging` or `production` |
| `APP_PORT` | Host port for the API (default `8000`) |
| `POSTGRES_USER` | Postgres username |
| `POSTGRES_PASSWORD` | Postgres password |
| `POSTGRES_DB` | Postgres database name |
| `SECRET_KEY` | App secret key (generate with `python -c 'import secrets;print(secrets.token_urlsafe(64))'`) |
| `DATABASE_URL_ASYNC` | e.g. `postgresql+asyncpg://user:pass@postgres:5432/db` |
| `DATABASE_URL_SYNC` | e.g. `postgresql+psycopg://user:pass@postgres:5432/db?sslmode=verify-full&sslrootcert=/etc/ssl/postgres/server.crt` |
| `APP_CELERY_BROKER_URL` | e.g. `redis://redis:6379/0` |
| `APP_CELERY_RESULT_BACKEND` | e.g. `redis://redis:6379/1` |
| `WEB_CONCURRENCY` | Uvicorn workers (default `2`) |
| `CELERY_WORKER_CONCURRENCY` | Child processes per worker container (default `8`) |

### Environment Variables (non-secret, set via `vars` in the GH environment)

| Variable | Description |
|----------|-------------|
| `WORKER_EXEC_REPLICAS` | Number of `worker_exec` containers (default `12`) |
| `WORKER_AGGREGATE_REPLICAS` | Number of `worker_aggregate` containers (default `2`) |
| `WORKER_SWEEP_FINALIZE_REPLICAS` | Number of `worker_sweep_finalize` containers (default `2`) |

## Switching to a Real VM

1. **Provision VM**: Ubuntu 22.04+, Docker installed, `deploy` user with sudo
2. **SSH access**: Copy your public key to `/home/deploy/.ssh/authorized_keys`
3. **Get host fingerprint**: `ssh-keyscan -t ed25519 <your-host>` and store in `SSH_KNOWN_HOSTS`
4. **Update inventory**: Edit `deploy/inventory/staging.ini` with the real IP/hostname
5. **Push secrets**: Use `scripts/setup_github_secrets.ps1` to push all secrets
6. **Trigger workflow**: `gh workflow run deploy.yml -f environment=staging`

## Architecture

The deployed stack mirrors the repo root `docker-compose.yml`:

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Postgres  │────▶│     API     │────▶│    Redis    │
│  (TLS 5432) │     │  (port 8000)│     │  (port 6379)│
└─────────────┘     └─────────────┘     └─────────────┘
                          │
         ┌────────────────┼────────────────┐
         ▼                ▼                ▼
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│ worker_exec │   │  worker_    │   │  worker_    │
│ (×12, Q:    │   │  aggregate  │   │  sweep_     │
│  execution) │   │ (×2, Q: job │   │  finalize   │
│             │   │  +chunk fin)│   │ (×2, Q:sweep│
└─────────────┘   └─────────────┘   └─────────────┘
```

- **worker_exec**: `--queues=sweep.execution`, `--disable-prefetch` for fair work distribution
- **worker_aggregate**: `--queues=sweep.job.finalize,sweep.chunk.finalize`
- **worker_sweep_finalize**: `--queues=sweep.sweep.finalize`

## Notes

- `.env` is rendered on the target host with mode `0600`
- Secrets are not echoed into the workflow logs
- The target host logs into GHCR before `docker compose pull`
- DB migrations run before the stack is restarted
- Health probes (`/health` + `/health/ready`) are checked before marking deploy complete
- The Postgres container generates a self-signed TLS cert on first boot (stored in a named volume)

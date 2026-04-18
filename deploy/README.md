# Deployment

This deployment path uses:
- GitHub Actions to build and push the application image to GHCR
- Ansible to connect to the target machine over SSH
- Jinja templates to render `/opt/sweep-app/.env` and `/opt/sweep-app/docker-compose.yml`
- Docker Compose on the target host to run the API, Postgres, Redis, and 3 Celery workers

## Required GitHub environment secrets

Set these on the `staging` and/or `production` environment:

- `SSH_PRIVATE_KEY`
- `SSH_HOST`
- `ANSIBLE_VAULT_PASSWORD`
- `APP_ENV`
- `APP_PORT`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`
- `SECRET_KEY`
- `REDIS_URL`
- `DATABASE_URL_ASYNC`
- `DATABASE_URL_SYNC`
- `APP_CELERY_BROKER_URL`
- `APP_CELERY_RESULT_BACKEND`
- `WEB_CONCURRENCY`
- `CELERY_WORKER_COUNT`
- `CELERY_WORKER_CONCURRENCY`

## Notes

- `.env` is rendered on the target host with mode `0600`
- secrets are not echoed into the workflow logs
- the target host logs into GHCR before `docker compose pull`
- DB migrations run before the stack is restarted

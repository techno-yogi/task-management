#!/usr/bin/env bash
set -euo pipefail

# Persist certs on the pgssl volume so all containers share one trust anchor.
SSL_DIR=/var/lib/postgresql/ssl
mkdir -p "${SSL_DIR}"

if [[ ! -f "${SSL_DIR}/server.crt" ]]; then
  openssl req -new -x509 -days 3650 -nodes \
    -out "${SSL_DIR}/server.crt" \
    -keyout "${SSL_DIR}/server.key" \
    -subj "/CN=postgres" \
    -addext "subjectAltName=DNS:postgres,DNS:localhost,IP:127.0.0.1"
  chmod 600 "${SSL_DIR}/server.key"
  chown postgres:postgres "${SSL_DIR}/server.key" "${SSL_DIR}/server.crt"
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"

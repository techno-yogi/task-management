#!/usr/bin/env bash
# Make the bind-mounted docker.sock callable by the `deploy` user without
# sudo. Two paths:
#   1. The host group that owns the socket has a free, non-root gid -> point
#      the in-container `docker` group at that gid (the canonical pattern).
#   2. The socket is owned by root:root (Docker Desktop's helper VM does this
#      because Linux gid 0 == root) -> just add `deploy` to root.
set -euo pipefail

if [[ -S /var/run/docker.sock ]]; then
  sock_gid=$(stat -c '%g' /var/run/docker.sock)
  if [[ "$sock_gid" -eq 0 ]]; then
    if ! id -nG deploy | grep -qw root; then
      usermod -aG root deploy
    fi
  else
    current_gid=$(getent group docker | cut -d: -f3 || echo 0)
    if [[ "$sock_gid" != "$current_gid" ]]; then
      groupmod -g "$sock_gid" docker || true
    fi
    if ! id -nG deploy | grep -qw docker; then
      usermod -aG docker deploy
    fi
  fi
fi

exec /usr/sbin/sshd -D -e

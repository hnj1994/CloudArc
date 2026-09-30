#!/usr/bin/env bash
# Install CloudArc on Ubuntu Server 22.04/24.04 with Docker. Idempotent; run as a sudo-capable user.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl
  curl -fsSL https://get.docker.com | sudo sh
fi

if [ ! -f .env ]; then
  cp .env.example .env
  key=$(head -c 32 /dev/urandom | base64)
  sed -i "s|^CLOUDARC_MASTER_KEY=.*|CLOUDARC_MASTER_KEY=${key}|" .env
  chmod 600 .env
  echo "Created .env with a fresh master key. Back up CLOUDARC_MASTER_KEY separately: credentials cannot be decrypted without it."
fi

sudo docker compose up -d --build
echo "Waiting for health check…"
for _ in $(seq 1 30); do
  if sudo docker compose exec -T cloudarc python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health')" 2>/dev/null; then break; fi
  sleep 2
done

if [ -n "${ADMIN_EMAIL:-}" ]; then
  sudo docker compose exec -T cloudarc cloudarc init --admin-email "$ADMIN_EMAIL"
fi
echo "CloudArc is running. First admin: ADMIN_EMAIL=you@example.com ./deploy/install.sh (prints an API token once)."

#!/usr/bin/env bash
# Daily backup (cron: 30 1 * * *). Keeps 14 days. RPO 24 h. Restore: see docs/OPERATIONS.md.
set -euo pipefail
cd "$(dirname "$0")/.."
sudo docker compose exec -T cloudarc cloudarc backup --dir /data/backups
sudo docker compose exec -T cloudarc sh -c 'ls -1d /data/backups/* | head -n -14 | xargs -r rm -rf'

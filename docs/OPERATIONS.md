# Operations

## 1. Deploy

```bash
git clone <repo> /opt/cloudarc && cd /opt/cloudarc
ADMIN_EMAIL=admin@yourcompany.com ./deploy/install.sh
```

- `.env` is created with a random `CLOUDARC_MASTER_KEY`. **Store the key in your password vault.** Without it, stored client secrets can't be decrypted after a restore.
- Set `CLOUDARC_HOSTNAME`. For internal names, Caddy issues certificates from its own CA; for public names, it uses Let's Encrypt.
- Upgrade with `git pull && sudo docker compose up -d --build`. The schema is created and migrated on start.

### Platform sign-in (Entra ID SSO)

1. In Entra ID, register an app, e.g. **CloudArc Console**, as a single-tenant *SPA*. Set the redirect URI to `https://<CLOUDARC_HOSTNAME>`.
2. Under **Expose an API**, set the Application ID URI to `api://<client-id>` and add the scope `access_as_user`.
3. Enforce MFA through Conditional Access for this app.
4. Set `CLOUDARC_AUTH_MODE=entra`, `CLOUDARC_ENTRA_TENANT_ID` and `CLOUDARC_ENTRA_CLIENT_ID`, then restart.
5. Users must be pre-provisioned under Administration (by e-mail/UPN) and assigned to clients. Sign-in alone grants nothing.

API tokens (`cloudarc create-user … --token`, or Administration › Issue API token) remain available for automation and read-only integrations.

## 2. Onboard a client subscription

In the client's Entra tenant:

1. Create an app registration and a client secret.
2. On each subscription, assign **Reader**, **Cost Management Reader** and **Billing Reader** to the app.
3. Do **not** assign Contributor or Owner. CloudArc flags credentials with write access as *over-privileged*.

Then, in CloudArc:

1. Go to **Accounts & data › Connect an Azure subscription** and enter the directory ID, client ID and secret.
2. Select **Validate**. This checks the effective permissions of every visible subscription.
3. Select the subscriptions to enable. The initial sync backfills `CLOUDARC_INITIAL_BACKFILL_MONTHS` (3 by default).
4. Daily syncs run at `CLOUDARC_SYNC_HOUR_UTC` and reload the last `CLOUDARC_SYNC_LOOKBACK_DAYS` days to absorb Azure restatements.

For clients without API access, upload cost exports (Azure cost details / exports, AWS CUR, GCP billing export) under **Accounts & data › Upload**. Uploading the same period again replaces it.

### Acceptance: ±0.5% reconciliation

For a closed month, take the total from Azure Cost Management › Cost analysis (Actual cost, subscription scope, pre-tax, INR). Enter it under **Reports › Reconcile**. The check passes within ±0.5%.

## 3. Alerts

Under **Alerts › Notification channels** (tenant admin), add a Teams incoming-webhook URL, an e-mail address (needs the `CLOUDARC_SMTP_*` settings) or a generic webhook. Use **Send test** to verify.

Budgets, forecast and anomaly alerts are evaluated after every sync or upload and de-duplicated per period and threshold.

## 4. Backup & restore (RPO 24 h, RTO 4 h)

```bash
# daily, e.g. cron: 30 1 * * * /opt/cloudarc/deploy/backup.sh
./deploy/backup.sh                       # Parquet snapshot in the data volume, 14 days kept
sudo docker compose cp cloudarc:/data/backups ./offsite-copy   # ship off-host
```

Restore:

```bash
sudo docker compose stop cloudarc
sudo docker compose run --rm -e CLOUDARC_DB_PATH=/data/restored.duckdb cloudarc cloudarc restore /data/backups/<YYYY-MM-DD>
# point CLOUDARC_DB_PATH=/data/restored.duckdb in .env (same CLOUDARC_MASTER_KEY), then:
sudo docker compose up -d
```

## 5. Monitoring

- `GET /api/health` (no auth) returns the version, account sync-status counts and failed sync jobs in the last 24 h. Point your uptime monitor at it and alert when `failed_sync_jobs_24h > 0`.
- Container logs are JSON lines with a request ID, path, status and duration, with secrets redacted: `docker compose logs -f cloudarc`.
- Failed syncs retry 3 times (5 and 10 minutes apart), then raise a critical `sync_failure` alert to the tenant's channels.

## 6. Optional: AI narrative

The report narrative is deterministic by default. To let an LLM polish the wording:

1. Get the data-handling policy approved.
2. Set `CLOUDARC_LLM_PROVIDER=anthropic`, `CLOUDARC_LLM_DATA_POLICY_APPROVED=true` and `ANTHROPIC_API_KEY`.
3. Run `pip install anthropic` in the image.

Only aggregated figures for the section are sent. Any output containing a number that is not in those figures is discarded.

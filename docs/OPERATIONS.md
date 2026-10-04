# Operations

## 1. Deploy

```bash
git clone <repo> /opt/cloudarc && cd /opt/cloudarc
ADMIN_EMAIL=admin@yourcompany.com ./deploy/install.sh
```

- `.env` is created with a random `CLOUDARC_MASTER_KEY`. **Store the key in your password vault.** Without it, stored client secrets can't be decrypted after a restore.
- Set `CLOUDARC_HOSTNAME`. For internal names, Caddy issues certificates from its own CA; for public names, it uses Let's Encrypt.
- Upgrade with `git pull && sudo docker compose up -d --build`. The schema is created and migrated on start.

### Alternative: Azure App Service

```bash
az login
CLOUDARC_APP=cloudarc-console ADMIN_EMAIL=you@example.com ./deploy/azure/deploy-webapp.sh
```

The script is idempotent; re-run it to ship a new version. It creates a Container Registry (the image is built in Azure with `az acr build`), a Key Vault holding `CLOUDARC_MASTER_KEY` (generated once, referenced from app settings), and a Linux App Service plan and Web App. The web app pulls from the registry with its managed identity, is HTTPS-only with TLS 1.2+, has Always On enabled and is health-checked on `/api/health`. Defaults are resource group `rg-cloudarc`, region `centralindia` and plan `B2`; override them with `CLOUDARC_RG`, `CLOUDARC_LOCATION` and `CLOUDARC_PLAN_SKU`.

- **One instance only.** DuckDB allows a single writer and the scheduler runs in-process, so the plan is pinned to one worker. Scale up, not out.
- **Data** lives on App Service persistent storage (`/home/cloudarc`). Back it up with `cloudarc backup` or App Service backups.
- **First admin.** With `ADMIN_EMAIL` set on a fresh database, the admin API token is written to `/home/cloudarc/bootstrap-admin-token.txt`. It is never written to the logs. The script prints the `az rest` commands to read the file once and then delete it.
- **Continuous deployment.** `.github/workflows/deploy-azure.yml` runs the tests and deploys the code on every push to `main`, the same way `deploy-webapp-code.sh` does, then checks `/api/health`. Set it up once with `.\deploy\azure\setup-github-deploy.ps1 -App cloudarc-console -Repo <owner>/<repo>`, or the `.sh` version in Cloud Shell. The script creates a dedicated Entra app whose federated credential trusts only workflow runs on `main`, so no client secret exists. It grants that app Website Contributor on the one web app and sets the `AZURE_*` secrets and variables, using the GitHub CLI if installed; otherwise it prints them for you to add.
- **Not yet validated on a live subscription:** the script has only been syntax-checked. In particular, confirm on the first run that the non-root container user can write to `/home`.

### Platform sign-in (Entra ID SSO)

On Azure App Service, one script does all of it. Run it as a user who can create app registrations, for example in Azure Cloud Shell:

```bash
CLOUDARC_APP=cloudarc-console ./deploy/azure/setup-entra-sso.sh
```

The script creates the app registration, redirect URIs and scope, pre-authorizes the console so users see no consent prompt, and switches the web app to `CLOUDARC_AUTH_MODE=entra`. The manual steps below are the equivalent for other hosts.


1. In Entra ID, register an app, e.g. **CloudArc Console**, as a single-tenant *SPA*. Set the redirect URI to `https://<CLOUDARC_HOSTNAME>`.
2. Under **Expose an API**, set the Application ID URI to `api://<client-id>` and add the scope `access_as_user`.
3. Enforce MFA through Conditional Access for this app.
4. Set `CLOUDARC_AUTH_MODE=entra`, `CLOUDARC_ENTRA_TENANT_ID` and `CLOUDARC_ENTRA_CLIENT_ID`, then restart.
5. Users must be pre-provisioned under Administration (by e-mail/UPN) and assigned to clients. Sign-in alone grants nothing.

API tokens (`cloudarc create-user … --token`, or Administration › Issue API token) remain available for automation and read-only integrations. When SSO is on, the sign-in page shows only **Sign in with Microsoft**; token sign-in is behind **Use an API token instead**.

### Custom domain and browser warnings

Shared `*.azurewebsites.net` addresses are often used for phishing pages. A new sign-in page on one can be flagged by Google Safe Browsing (Chrome's red "Dangerous site" warning). Run the console on your own subdomain:

```powershell
.\deploy\azure\add-custom-domain.ps1 -App cloudarc-console -Domain cloudarc.yourcompany.com
```

The first run prints a CNAME and an `asuid` TXT record to create at your DNS provider. Run it again once they resolve. It then binds the domain, issues a free managed certificate, enforces HTTPS, and adds the SSO redirect URIs. `add-custom-domain.sh` is the bash equivalent.

To clear an existing Safe Browsing flag, prove ownership in [Google Search Console](https://search.google.com/search-console):
1. Add a **URL prefix** property for the console address.
2. Choose **HTML tag** as the verification method and copy only the `content` value.
3. Set it as `CLOUDARC_GOOGLE_SITE_VERIFICATION` on the web app; the console serves the tag on `/`.
4. Select **Verify**.
5. Under **Security & manual actions › Security issues**, select **Request review**.

Do steps 1–4 for each address you use: the custom domain and the `azurewebsites.net` address are separate properties.

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

### Azure App Service

`deploy/azure/setup-backups.sh` sets up nightly backups to a private Blob container:
- The web app authenticates with its managed identity; the storage account has no keys enabled.
- Backups older than 30 days are deleted automatically, and deleted backups stay recoverable for 7 days.
- Each backup is a consistent Parquet export with a manifest of row counts, named `cloudarc-<UTC timestamp>.tar.gz`. It runs daily at 04:00 UTC, after the 02:00 sync.
- `GET /api/health` reports the last backup's status and time. A failed backup is recorded there and in the audit log, and is retried the next night.

Run these from the web app's SSH console (**Portal › App Service › SSH**, or `https://<app>.scm.azurewebsites.net/webssh/host`):

```bash
python -m cloudarc.cli backup --upload        # back up now
python -m cloudarc.cli backups                # list backups
python -m cloudarc.cli restore --from-blob latest   # or a name from the list
```

`restore --from-blob` downloads the backup into a separate file next to the database and checks every table's row count against the backup's manifest. It does not touch the running database. **Restart the app** (Portal › Restart, or `az webapp restart`) to switch over. The previous database is kept as `cloudarc.duckdb.pre-restore-<timestamp>`; to roll back, stop the app, move it back and start again. The encryption key (`CLOUDARC_MASTER_KEY`) must be the same one that was in use when the backup was taken, or stored cloud credentials cannot be decrypted. Keep a copy of it outside Azure.

### Docker host

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

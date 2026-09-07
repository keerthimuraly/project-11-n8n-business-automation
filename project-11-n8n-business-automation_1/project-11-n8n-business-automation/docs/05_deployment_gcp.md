# 05 · Deployment to Google Cloud Platform

Two supported targets. Cloud Run is the recommended one and is fully scripted;
the GCE alternative is documented because it is closer to how a real enterprise
self-hosts n8n.

---

## Option A — Cloud Run + Cloud SQL (scripted)

### What gets created

| Resource | Purpose |
|---|---|
| Cloud SQL for PostgreSQL 16 (`db-f1-micro`) | n8n metadata (`n8n_meta`) + application tables (`app`) |
| Secret Manager × 4 | DB password, n8n encryption key, OpenAI key, ITSM API key |
| Service account `p11-n8n-runner` | `cloudsql.client`, `secretmanager.secretAccessor`, `logging.logWriter` |
| Cloud Run `p11-itsm-api` | Mock system of record, **internal ingress only** |
| Cloud Run `p11-n8n` | The orchestration engine, public so webhooks can reach it |
| Artifact Registry `p11-images` | Built container image for the ITSM API |

### Run it

```bash
cd deploy/gcp
export PROJECT_ID=your-gcp-project
export OPENAI_API_KEY=sk-...
export REGION=asia-south1            # optional, Mumbai is the default

chmod +x deploy.sh
./deploy.sh
```

Ten to fifteen minutes, most of it Cloud SQL provisioning. Every step is
idempotent, so re-running after a failure picks up where it left off.

### Two things the script does that are easy to get wrong

**It deploys n8n twice.** n8n needs `WEBHOOK_URL` set to its own public address
in order to build correct webhook URLs and — critically — the approval
`$execution.resumeUrl`. That address is not known until the service exists. So
the script deploys with a placeholder, reads back the assigned URL, and deploys
again with it set. Skipping the second pass produces approval emails whose
buttons point at `placeholder.invalid`.

**`--min-instances=1`.** Cloud Run scales to zero by default, which breaks two
things here: the schedule trigger in `wf_sla_sweep` never fires because nothing
is running to fire it, and held approval executions are lost when the instance is
reclaimed. One warm instance is the price of a functioning scheduler.

### After the script

```bash
# 1. apply the application schema
gcloud sql connect p11-n8n-pg --user=n8n --database=n8n < ../../db/init/001_schema.sql
gcloud sql connect p11-n8n-pg --user=n8n --database=n8n < ../../db/init/002_seed.sql

# 2. retrieve the DB password for the n8n credential
gcloud secrets versions access latest --secret=p11-db-password
```

Then in the n8n editor: create the owner account, import the four workflows, and
create the two credentials.

The **Postgres** credential differs from local:

| Field | Cloud Run value |
|---|---|
| Host | `/cloudsql/PROJECT_ID:REGION:p11-n8n-pg` |
| Database | `n8n` |
| User | `n8n` |
| Password | from Secret Manager, above |
| Schema | `app` |
| SSL | disable — the Cloud SQL connector already encrypts |

The **SMTP** credential must change. Mailpit is a local development sink and is
deliberately not deployed. Use SendGrid, Mailgun, Amazon SES or your
organisation's relay, and keep the name `SMTP - Mailpit (local)` so the nodes
still resolve — or rename it in all nine email nodes.

### Verify

```bash
N8N_URL=$(gcloud run services describe p11-n8n --region=asia-south1 --format='value(status.url)')

curl -X POST "$N8N_URL/webhook/it-request" \
  -H 'Content-Type: application/json' \
  -d '{
    "requester_email": "arun.mehta@tcs-demo.local",
    "subject": "VPN keeps dropping on Wi-Fi",
    "description": "The corporate VPN disconnects every ten minutes on my home network.",
    "category": "network"
  }'
```

### Costs

Rough monthly estimate for a demo left running, `asia-south1`:

| Item | Estimate |
|---|---|
| Cloud SQL `db-f1-micro`, 10 GB | ₹700–900 |
| Cloud Run n8n, 1 warm instance, 2 vCPU / 2 GB | ₹2,600–3,500 |
| Cloud Run ITSM API, scale to zero | under ₹80 |
| Secret Manager, Artifact Registry, logging | under ₹150 |
| OpenAI `gpt-4o-mini` | about ₹0.10 per request |

The warm n8n instance dominates. `gcloud run services delete p11-n8n
p11-itsm-api` and `gcloud sql instances delete p11-n8n-pg` when the assessment is
submitted. Cloud SQL keeps billing whether or not anything connects to it.

### Operating it

```bash
# logs
gcloud run services logs tail p11-n8n --region=asia-south1

# roll the OpenAI key without redeploying
printf 'sk-newkey' | gcloud secrets versions add p11-openai-key --data-file=-
gcloud run services update p11-n8n --region=asia-south1 \
  --set-secrets="OPENAI_API_KEY=p11-openai-key:latest"

# scale for load
gcloud run services update p11-n8n --region=asia-south1 --max-instances=10
```

---

## Option B — Compute Engine VM with docker-compose

Closer to a typical enterprise self-hosted deployment, and it runs the same
`docker-compose.yml` you developed against — including Mailpit, which makes it
the better choice for a live demo.

```bash
# 1. create the VM
gcloud compute instances create p11-n8n-vm \
  --zone=asia-south1-a \
  --machine-type=e2-medium \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --tags=n8n-server

# 2. allow HTTPS only (never open 5678 to the internet)
gcloud compute firewall-rules create allow-https-n8n \
  --allow=tcp:443 --target-tags=n8n-server --source-ranges=0.0.0.0/0

# 3. reserve a static IP so the webhook URL stays stable
gcloud compute addresses create p11-n8n-ip --region=asia-south1

# 4. on the VM
gcloud compute ssh p11-n8n-vm --zone=asia-south1-a
```

Then on the VM:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git nginx certbot python3-certbot-nginx
sudo usermod -aG docker "$USER" && newgrp docker

git clone <your-repo-url> && cd project-11-n8n-business-automation
cp .env.example .env && nano .env
#   OPENAI_API_KEY=...
#   N8N_ENCRYPTION_KEY=$(openssl rand -hex 32)
#   N8N_HOST=n8n.yourdomain.com
#   N8N_PROTOCOL=https
#   WEBHOOK_URL=https://n8n.yourdomain.com/

docker compose up -d
sudo certbot --nginx -d n8n.yourdomain.com    # TLS + the reverse proxy
```

Nginx site config, proxying to n8n with the WebSocket upgrade the editor needs:

```nginx
server {
    server_name n8n.yourdomain.com;
    client_max_body_size 20M;

    location / {
        proxy_pass         http://127.0.0.1:5678;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection 'upgrade';
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
        proxy_read_timeout 3600s;   # long-running executions
    }
}
```

Bind Docker's published ports to localhost only in `docker-compose.yml`
(`"127.0.0.1:5678:5678"`) so nothing is reachable except through Nginx.

---

## Which to choose

| | Cloud Run | GCE VM |
|---|---|---|
| Setup effort | one script | ~30 min manual |
| Cost at rest | warm instance always billed | can be stopped between demos |
| TLS | automatic | certbot, manual renewal watch |
| Scheduled triggers | needs `--min-instances=1` | always on |
| Held approval executions | survive only while the instance lives | survive restarts via the volume |
| Mailpit for demos | not deployable | works as-is |
| Matches "deployment-ready" | yes, managed | yes, conventional self-host |

For this assessment: **deploy to Cloud Run** because the script is the artefact
that proves deployment readiness, and **demo from local Docker** because Mailpit
makes the notification and approval paths visible. Say exactly that in the video
— it is a defensible engineering choice, not a shortcut.

---

## Production hardening checklist

Not required for the assessment, but this is what the honest gap looks like:

- [ ] Put Cloud Armor or an API gateway in front of the webhook — rate limiting
      per source, and a shared secret or HMAC signature so anyone who learns the
      URL cannot inject tickets. Right now the webhook is unauthenticated, which
      is fine for a demo and not fine for production.
- [ ] Enable n8n SSO / SAML so the editor is not protected by a single local
      owner account.
- [ ] Move to `EXECUTIONS_MODE=queue` with Memorystore Redis and separate worker
      services once throughput exceeds a few requests per second.
- [ ] Switch `EXECUTIONS_DATA_SAVE_ON_SUCCESS` to `none` and rely on
      `app.audit_log`, once retention cost matters.
- [ ] Private IP on Cloud SQL with Serverless VPC Access, instead of the
      connector over the Google-managed path.
- [ ] Cloud SQL automated backups verified by an actual restore test, plus
      point-in-time recovery enabled.
- [ ] Log-based alerting on ERROR rows in `app.audit_log` and on Cloud Run 5xx.
- [ ] A per-day OpenAI token budget with a hard cutoff, so a runaway loop cannot
      produce a surprise invoice.
- [ ] Replace the mock ITSM API with the real ServiceNow connector — four call
      sites in the workflow change, nothing else.

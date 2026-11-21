# Running Gazi in production

One Linux server (Ubuntu 24.04, 4 GB RAM or more) runs everything: Supabase (Postgres + Auth) from Supabase's
own self-hosting setup, and Gazi's API, workers, web app, Redis, Nginx and Let's Encrypt from
`infra/docker-compose.yml`. Run the commands below over SSH on the server, one at a time.

## 1. Server and domain

1. Point your domain (for example `app.your-domain.com`) at the server's IP address with an **A record**.
2. Install Docker:
   ```
   curl -fsSL https://get.docker.com | sh
   ```
3. Get the code:
   ```
   git clone https://github.com/mehedi2283/AG-SaaS.git /opt/gazi && cd /opt/gazi
   ```

## 2. Supabase

Follow Supabase's self-hosting guide (https://supabase.com/docs/guides/self-hosting/docker) in `/opt/supabase`.
In its `.env`:

- set strong values for `POSTGRES_PASSWORD`, `JWT_SECRET`, `ANON_KEY`, `SERVICE_ROLE_KEY`, `DASHBOARD_PASSWORD`;
- set `SITE_URL` and `API_EXTERNAL_URL` to `https://app.your-domain.com`;
- turn on Gazi's access-token hook:
  `GOTRUE_HOOK_CUSTOM_ACCESS_TOKEN_ENABLED=true` and
  `GOTRUE_HOOK_CUSTOM_ACCESS_TOKEN_URI=pg-functions://postgres/app/custom_access_token_hook`;
- set up email sending for sign-up confirmation and password resets (`SMTP_*`);
- make new accounts confirm their email: `ENABLE_EMAIL_AUTOCONFIRM=false`.

Then lock it down in Supabase's `docker-compose.yml`:

- **Ports.** Gazi's Nginx reaches Supabase over the Docker network, so nothing of Supabase needs to be public.
  Docker opens published ports past the `ufw` firewall, so a firewall alone does not help. Under the `kong` and
  `supavisor` services, put `127.0.0.1:` in front of every port, for example
  `- 127.0.0.1:${KONG_HTTP_PORT}:8000` and `- 127.0.0.1:${POSTGRES_PORT}:5432`. To open Studio or connect to the
  database from your PC, use an SSH tunnel: `ssh -L 8000:127.0.0.1:8000 you@server`, then http://localhost:8000.
- **Passwords.** Under the `auth` service's `environment:`, add `GOTRUE_PASSWORD_MIN_LENGTH: 10`.

Also turn on the server firewall for SSH and web only:

```
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw enable
```

Check from your own PC that only 22, 80 and 443 answer (for example `nmap -Pn app.your-domain.com`).

Start it (`docker compose up -d` in `/opt/supabase`), then apply Gazi's database migrations in filename order:

```
for f in /opt/gazi/infra/supabase/migrations/*.sql; do docker exec -i supabase-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 < "$f"; done
```

Create Gazi's database login (pick a long random password; it must NOT be a superuser):

```
docker exec -it supabase-db psql -U postgres -c "create role gazi_api login noinherit password 'CHANGE-ME'; grant authenticated, gazi_worker to gazi_api;"
```

## 3. Secrets

Each secret is one file in `/opt/gazi/infra/secrets/` (git ignores the folder). Create them with `nano`, one
value per file, no quotes:

| File | Value |
|---|---|
| `gazi_database_url` | `postgresql://gazi_api:CHANGE-ME@supabase-db:5432/postgres` |
| `gazi_kek` | a new master key: `openssl rand -base64 32` (**back this up offline; without it encrypted keys are lost**) |
| `gazi_gotrue_service_key` | Supabase's `SERVICE_ROLE_KEY` |
| `gazi_stripe_secret_key` | Stripe **live** secret key (`sk_live_...` or a restricted `rk_live_...`) |
| `gazi_stripe_webhook_secret` | from step 6 |
| `gazi_openai_api_key` | the platform OpenAI key |
| `gazi_sendgrid_api_key` | SendGrid key (weekly reports and alerts) |

```
chmod 600 /opt/gazi/infra/secrets/*
```

## 4. Settings

```
cp /opt/gazi/infra/.env.production.example /opt/gazi/infra/.env.production
nano /opt/gazi/infra/.env.production
```

Fill in the domain, Supabase's `ANON_KEY`, and the **live** Stripe price ids (create the $99 plan and the three
token packs in Stripe live mode first, like the sandbox ones in `docs/status.md`).

## 5. First certificate, then start

```
cd /opt/gazi/infra
docker compose -f docker-compose.yml --env-file .env.production run --rm --entrypoint "certbot certonly --standalone -d app.your-domain.com --agree-tos -m you@your-domain.com -n" certbot
docker compose -f docker-compose.yml --env-file .env.production up -d --build
```

Check: `https://app.your-domain.com/healthz` shows `{"status":"ok"}`, and the sign-in page loads.

## 6. Connect the outside services

- **Stripe** (live mode): Developers, Webhooks, add endpoint `https://app.your-domain.com/webhooks/stripe` with the
  events `customer.subscription.created`, `.updated`, `.deleted`, `.paused`, `.resumed`. Put its signing secret in
  `secrets/gazi_stripe_webhook_secret`, then `docker compose ... up -d` again. Turn on the customer portal.
- **Instantly / HeyReach / Calendly** (per workspace): each workspace copies its webhook address from
  Settings, Webhook addresses in Gazi into the provider's webhook settings (Instantly: event "reply received").
- **SendGrid**: verify the sending domain for `GAZI_EMAIL_FROM`.
- **Rotate the keys the old n8n setup exposed.** The n8n workflows hold the Instantly API key and the old
  Supabase project's service_role key in plain text.
  - Instantly: Settings, Integrations, API Keys: create a new key, paste it into Gazi (Settings, Connected
    accounts, Instantly, Replace key), then delete the old key in Instantly.
  - Old Supabase project (the one n8n used, not this server): Gazi doesn't use it, so pause or delete it. If
    you keep it, rotate its JWT secret under Project Settings, API, so the leaked service_role key stops working.
  - Then switch off the n8n workflows, or delete those credentials from n8n.

## 7. Make yourself the superuser

Sign up in the app first, then:

```
docker exec -it supabase-db psql -U postgres -c "insert into public.platform_admins (user_id, note) select id, 'owner' from public.users where email = 'you@your-domain.com';"
```

## Every day

| What | Command (in `/opt/gazi/infra`) |
|---|---|
| Update to the latest code | `git pull`, apply any new migrations (step 2 loop, new files only), then `docker compose -f docker-compose.yml --env-file .env.production up -d --build` |
| Logs | `docker compose -f docker-compose.yml logs -f api worker-replies` |
| Restart | `docker compose -f docker-compose.yml --env-file .env.production restart` |
| Back up the database | `docker exec supabase-db pg_dump -U postgres -Fc postgres > /backups/gazi-$(date +%F).dump` (run daily from cron; keep copies off the server) |

Keep a copy of `secrets/gazi_kek` with the backups, stored separately: a database backup without the KEK can't
decrypt workspaces' provider keys and signatures.

Note: in Supabase's `.env`, also add `https://app.your-domain.com/account` to `ADDITIONAL_REDIRECT_URLS`, so
password-reset links return people to the page where they set a new password.

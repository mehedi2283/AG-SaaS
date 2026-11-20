# Run Gazi on your computer

This starts the database, sign-in service and API on your machine. There's no app screen yet: you use the
API from its docs page in the browser. The commands work in PowerShell (Windows) and Terminal (Mac).

## You need

- Docker Desktop, running.
- Node.js 20 or newer (for `npx`). Check with `node --version`.
- Git.

## Fastest way (Windows)

From the `AG-SaaS` folder, with Docker Desktop open:

```bash
powershell -ExecutionPolicy Bypass -File infra/dev-setup.ps1
```

It starts Supabase, writes `infra/.env` and `frontend/.env.local`, starts Redis and the API, creates the
demo user `demo@gazi.local` (password `gazi-demo-password`) and prints a sign-in token. Run it again any
time for a fresh token. The manual steps below do the same thing by hand.

Then start the app in a second PowerShell window and open <http://localhost:3000>:

```bash
cd frontend
npm install
npm run dev
```

## First time

1. Get the code and open the folder:

   ```bash
   git clone https://github.com/mehedi2283/AG-SaaS.git
   cd AG-SaaS
   ```

2. Start Supabase (database, sign-in, Studio). The first run downloads a few GB and takes several minutes:

   ```bash
   npx supabase start --workdir infra
   ```

3. Create your settings file by copying `infra/.env.example` to `infra/.env`
   (Windows: `copy infra\.env.example infra\.env`, Mac: `cp infra/.env.example infra/.env`).

4. Fill in `infra/.env`:
   - Run `npx supabase status --workdir infra -o env` and copy the values of `JWT_SECRET`,
     `SERVICE_ROLE_KEY` and `ANON_KEY` into `GAZI_JWT_SECRET`, `GAZI_GOTRUE_SERVICE_KEY` and
     `GAZI_DEV_ANON_KEY`.
   - Make a master key with the command below and paste the output into `GAZI_KEK`:

     ```bash
     docker run --rm python:3.12-slim python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
     ```

5. Start Redis and the API:

   ```bash
   docker compose -f infra/docker-compose.dev.yml up -d --build
   ```

6. Check it's working: <http://localhost:8000/readyz> should say `{"status":"ready"}`.

## Try it

1. Open Studio at <http://localhost:54323>, go to **Authentication → Users → Add user**, enter an email and
   password, and tick **Auto Confirm User**.
2. Get a sign-in token for that user:

   ```bash
   docker compose -f infra/docker-compose.dev.yml exec api python -m scripts.dev_token you@example.com yourpassword
   ```

3. Open the API docs at <http://localhost:8000/docs>, click **Authorize**, paste the token, then:
   - `POST /v1/tenants` with `{"name": "My Company", "slug": "my-company"}` creates a workspace.
   - `POST /v1/session/active-tenant` with that `tenant_id` selects it.
   - Run step 2 again for a fresh token (it now carries the workspace), click **Authorize** again and paste it.
   - `PUT /v1/integrations/instantly` with `{"api_key": "..."}` saves a key; `GET /v1/integrations` shows it masked.
   - `POST /v1/webhook-endpoints` with `{"provider": "instantly"}` creates a webhook URL.

Studio shows every table, so you can see what each call stored.

## Every day

| What | Command |
|---|---|
| Start | `npx supabase start --workdir infra`, then `docker compose -f infra/docker-compose.dev.yml up -d` |
| Stop | `docker compose -f infra/docker-compose.dev.yml down`, then `npx supabase stop --workdir infra` |
| API logs | `docker compose -f infra/docker-compose.dev.yml logs -f api` |
| Wipe the database and re-apply migrations | `npx supabase db reset --workdir infra` |

The API reloads by itself when code in `backend/app` changes. After changing `infra/.env`, run the start
command for the API again with `--force-recreate`.

## Billing (Stripe sandbox)

Billing is on when `GAZI_STRIPE_SECRET_KEY` and `GAZI_STRIPE_PRICE_ID` are set in `infra/.env` (see
`infra/.env.example`). Leave them empty and nothing is gated. With billing on, a new workspace sees the plan
screen until checkout finishes.

1. Forward Stripe's events to the API (keep it running): `stripe listen --forward-to localhost:8000/webhooks/stripe`.
   The first time, put the `whsec_...` it prints into `GAZI_STRIPE_WEBHOOK_SECRET` and recreate the API.
2. In the app, click **Start 14-day free trial** and pay with the test card `4242 4242 4242 4242`
   (any future date, any CVC).
3. Try the other states on that subscription in the Stripe Dashboard (sandbox): cancel it, or end the trial
   early after checking out with `4000 0000 0000 0341` (a card that attaches but fails when charged).
   A failed payment or a cancellation gives 7 days of grace, then the workspace pauses.
   (`stripe trigger` makes events for a brand-new customer, which Gazi ignores as not one of its workspaces.)

Subscription state only ever comes from signed Stripe webhooks; nothing in the app or API can mark a
workspace as paid.

## If something goes wrong

- **`readyz` says not ready:** Supabase isn't running. Run `npx supabase start --workdir infra`.
- **401 "access token is invalid":** the token is older than an hour, or `GAZI_JWT_JWKS_URL` is missing from
  `infra/.env`. Get a fresh token.
- **403 "Choose a workspace first":** select a workspace, then get a fresh token (step 3 above).
- **Port already in use:** another Supabase project is running. Stop it with `npx supabase stop --all`.

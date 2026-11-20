# Gazi architecture

Gazi replaces the n8n workflows with a FastAPI backend, ARQ workers and self-hosted Supabase.
Instantly (email) and HeyReach (LinkedIn) remain the sending layer. The rules every change must follow
are in `CLAUDE.md`; this document describes how the parts fit together.

## Components

```
 browser ──▶ Nginx (TLS) ──▶ Next.js frontend
                        └──▶ FastAPI  /v1/*  (JWT from Supabase Auth, runs as `authenticated` under RLS)
                        └──▶ FastAPI  /webhooks/{provider}/{token}   (no JWT; token → tenant)
 FastAPI ──enqueue──▶ Redis (AOF) ──▶ ARQ workers (run as `gazi_worker` under RLS)
 workers ──▶ OpenAI (platform key) · Instantly · HeyReach · Apollo · Calendly · SendGrid
 everything ──▶ Postgres (Supabase), RLS forced on every table
```

## Tenancy

- The active tenant lives in the JWT (`tenant_id`, `tenant_role`), injected by
  `app.custom_access_token_hook` after checking membership. Switching workspace sets
  `app_metadata.active_tenant_id` and refreshes the session.
- API transactions: `SET LOCAL ROLE authenticated` + `request.jwt.claims`. Workers: `SET LOCAL ROLE
  gazi_worker` + `app.tenant_id`. Every `tenant_id` column defaults to `app.tenant_id()` and no role may
  write it.
- Tests in `backend/tests/rls` prove isolation for every table and role.

## Backend modules (`backend/app`)

| Module | Owns |
|---|---|
| `core/` | config, JWT verification, envelope crypto, structlog with secret redaction, typed errors |
| `db/` | asyncpg pool, per-transaction tenant context |
| `domain/tenants`, `members` | workspaces, invites, roles |
| `domain/integrations` | encrypted keys for Instantly, HeyReach, Apollo, Calendly; live key checks |
| `domain/campaigns` | campaign settings, sequence steps, creating campaigns in Instantly/HeyReach |
| `domain/leads` | CSV import, Apollo search + buyer scoring, duplicate review |
| `domain/generation` | per-step subject/body generation, guards, approval |
| `domain/replies` | inbox, intent, drafts, approval, sending |
| `domain/bookings` | Calendly events matched to campaigns and leads |
| `domain/reports` | weekly campaign reports |
| `domain/prompts` | versioned, immutable prompt templates |
| `domain/usage`, `audit` | token usage and caps, audit trail |
| `llm/` | OpenAI client, Pydantic output models, guards |
| `providers/` | Instantly, HeyReach, Apollo, Calendly, SendGrid clients (retryable vs permanent errors) |
| `workers/` | ARQ settings, `@tenant_job`, rate limits, jobs |

## API (`/v1`, tenant from the JWT only)

| Area | Endpoints |
|---|---|
| Session | `GET /me`, `GET /tenants`, `POST /tenants`, `POST /session/active-tenant` |
| Members | `GET/POST /members`, `PATCH/DELETE /members/{user_id}` |
| Integrations | `GET /integrations`, `PUT /integrations/{provider}`, `POST /integrations/{provider}/test`, `DELETE` |
| Senders | CRUD `/sender-identities` |
| Campaigns | CRUD `/campaigns`, `POST /campaigns/{id}/launch`, `/pause`, `GET /campaigns/{id}/stats` |
| Sequence | `GET/PUT /campaigns/{id}/steps` |
| Leads | `POST /lead-imports` (CSV, 202), `GET /lead-imports/{id}`, `GET /lead-imports/{id}/duplicates`, `POST /lead-imports/{id}/duplicates/resolve`, `POST /campaigns/{id}/apollo-search` (202), `GET/PATCH/DELETE /leads/{id}` |
| Generation | `POST /campaigns/{id}/generate` (202), `GET /generated-emails`, `PATCH /generated-emails/{id}`, `POST /generated-emails/{id}/approve` |
| Prompts | `GET/POST /prompt-templates`, `POST /prompt-templates/{id}/activate`, `POST /prompt-templates/{id}/preview` |
| Replies | `GET /replies`, `GET /replies/{id}`, `PATCH /replies/{id}`, `POST /replies/{id}/send`, `POST /replies/{id}/dismiss` |
| Reports, usage, audit | `GET /reports`, `GET /usage`, `GET /audit-log` |
| Webhook endpoints | `GET/POST /webhook-endpoints`, `POST /webhook-endpoints/{id}/rotate`, `DELETE` |
| Ops | `GET /healthz`, `GET /readyz` |

Webhooks (no JWT, rate-limited at Nginx): `POST /webhooks/instantly/{token}`,
`POST /webhooks/heyreach/{token}`, `POST /webhooks/calendly/{token}`. Each stores the raw event in
`webhook_events`, enqueues a job and returns 200 at once.

## Jobs (ARQ)

Queues: `replies` (own worker, highest priority), `llm`, `sync`, `imports`. Every job takes `tenant_id`
first, runs under `@tenant_job`, is idempotent, and records its status in Postgres.

| Job | What it does |
|---|---|
| `import_leads` | Normalise CSV headers, validate rows, upsert, park duplicates for review |
| `apollo_search` | Search Apollo, reveal emails, score each lead as a buyer (LLM), save |
| `generate_campaign` → `generate_lead_sequence` | One LLM call per lead and step; guards check links, sign-offs, placeholders |
| `launch_email_campaign` | Create the Instantly campaign and its steps, add leads with `custom_subject_{n}` / `personalization_{n}` |
| `launch_linkedin_campaign` | Create the HeyReach list, add leads, create the campaign with schedule and sequence, start it |
| `handle_reply` | Match the lead, classify intent, draft a reply, then send after a short delay or wait for approval |
| `send_reply` | Instantly reply API, or HeyReach message using the sending account and conversation |
| `handle_booking` | Match a Calendly booking by `utm_campaign`, mark the lead, count the booking |
| `weekly_report` (cron) | Instantly analytics + reply quality → LLM summary → email via SendGrid |
| `reconcile_stuck`, `purge_imports` (cron) | Re-enqueue stuck rows; delete CSVs after 7 days |

## Frontend

Design system and prototype: `docs/design/`. Screens: Replies (home), Campaigns, Campaign, Leads,
Settings. The frontend talks only to the API; Supabase JS is used for sign-in only.

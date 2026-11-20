# Project status and handoff

Read this first in a new session, after `CLAUDE.md`. It holds what the first build session learned that isn't
in the code: where things stand, what was found in the old n8n setup, open questions and the owner's
preferences. Update it at the end of each session.

Last updated: 2026-11-20 (full v1 build session).

## Where things stand

Every v1 area is built, with 341 backend tests (RLS, API, jobs with recorded provider responses), ruff + strict
mypy clean, and the frontend type-checked and linted. Everything is pushed to `main` (2026-11-20); pushing needs the owner's go-ahead each time (the owner must
approve pushing to `main`).

| Area | State | How it was verified |
|---|---|---|
| Schema, RLS, tenancy, auth, crypto, local dev | Done | Tests; local stack |
| Billing: $99 plan, 14-day trial, portal, grace/pause, token packs, AI allowance | Done | Stripe sandbox end to end |
| Workspace AI settings (auto-reply switch, model choice) | Done | Tests |
| Superuser dashboard (`/admin`) | Done | Tests; GoTrue ban against local Supabase |
| Workers: `@tenant_job`, per-tenant rate limits + concurrency, 4 queues, reconciler, CSV purge, alerts | Done | Tests; running in local Docker |
| Campaigns, senders (encrypted signature), sequence steps | Done | Tests; live API |
| Lead import (CSV, Apollo headers, duplicates review) | Done | Tests; 3,000-row live import |
| Apollo search + buyer scoring | Done | Recorded Apollo responses only |
| LLM client, prompts, guards, sequence generation | Done | Tests; **live OpenAI** 3-step sequence |
| Launch to Instantly / HeyReach, pause/resume | Done | Recorded responses only: **not yet tried with a real Instantly/HeyReach account** |
| Reply pipeline (classify, draft, auto-send after 15 min or approval, links by code) + Replies desk | Done | Tests with recorded OpenAI/Instantly |
| Calendly bookings (utm_campaign) | Done | Tests |
| Weekly reports (Saturdays) | Done | Tests; needs a SendGrid key to email |
| Production deployment (compose, Nginx, Certbot, runbook) | Written | Compose validated; frontend image builds and serves; **not yet run on a server** |
| Screens | Built | Each page compiles on the dev server; **not yet clicked through in a browser** |

## Before going live

1. Try one real Instantly campaign and one HeyReach campaign end to end; adjust `app/providers/instantly.py` /
   `heyreach.py` if their API shapes differ (they follow the public docs and the n8n workflow field names).
2. Send a real reply to a test campaign and check the webhook payload fields (`app/domain/replies/parse.py`).
3. Set `GAZI_SENDGRID_API_KEY` + `GAZI_EMAIL_FROM` (reports and alerts are stored but not emailed without it).
4. Add 2FA for superusers (Supabase MFA, `aal2`).
5. Create the live Stripe product, price and packs; follow `docs/runbooks/production.md`.
6. Roll the Stripe sandbox secret key (it appeared in a CLI error output during testing).

## What the old n8n setup does (reviewed 2026-11-20, read-only)

n8n instance `gaziai.app.n8n.cloud`, 16 workflows. The ones that matter:

| Workflow (id) | What it does | Notes for the rebuild |
|---|---|---|
| Push Imported leads to Campaign (`LgKkiETY0ACaTE4r`) | Dashboard posts parsed CSV rows; per lead, GPT writes `sequence_count` steps (subject + HTML body each); pushes to Instantly `POST /api/v2/leads` with `custom_variables.custom_subject_{n}` / `personalization_{n}`; registers a Calendly webhook | Step prompts (hook/problem, case study, macro trend, resource offer, breakup) are the source for v1 `email_step` prompt templates |
| Linked_In Campaign ( IMPORT ) (`3TI25xgIcYCKJOHD`) | Writes 5 LinkedIn messages per lead; creates the HeyReach list (`list/CreateEmptyList`, `list/AddLeadsToListV2`), campaign (`campaign/Create` with schedule), sequence (`campaign/UpdateSequence`: connection request, then messages with delays) and starts it (`campaign/StartCampaign`) | personalization_1 is the connection request (n8n capped it at 180 chars; DB allows 200) |
| Email Reply (`zQ9Fa95WgmvPEuXW`, **switched off**) | Webhook → waits 15 min → classifies warm/cold/neutral → GPT reply using campaign context → `POST /api/v2/emails/reply` with `eaccount`, `reply_to_uuid` | Keep the ~15 min delay before auto-sending. Instantly webhook fields used: `id`, `campaign_id`, `eaccount`, `from_email`, `lead_email`, `subject`, `body`, `thread_id`, `message_id`, `is_auto_reply` |
| LinkedIn Reply (`Uf87LSKozSUugSIx`, on) | Webhook → GPT reply → HeyReach `sendMessage` with `conversation_id` and `sender.id` (the sending account) | HeyReach webhook fields used: `campaign.id`, `conversation_id`, `sender.id`, `lead.{id, full_name, position, company_name, profile_url}`, `recent_messages[].{message, creation_time, message_type}` |
| Apollo Lead Puller (`enBlqvfgWP6ybNfQ`) | Apollo `mixed_people/api_search`, then `people/match` to reveal emails; GPT scores each lead (`buyer_score` 1–10, `confidence_score` 1–100, decision maker) | Source for the `lead_score` prompt |
| Booking Notification (`NWx6LJdmgu5Jc46C`) | Calendly `invitee.created` → match campaign by `payload.tracking.utm_campaign` → lead "hot", booking count +1 | Calendar links carry `?utm_campaign=<campaign id>` |
| Weekly Report (`FQMouHle5Og8E5N9`) | Saturdays 09:00: Instantly analytics + reply analysis → GPT summary → HTML email via SendGrid | Gazi purple `#A234FD`; the n8n prompt forces relentlessly positive wording, which v1 should not copy without asking |

No n8n execution history is stored, so there are no real payload samples. Field names above come from
the workflow code and must be checked against a real event before the reply pipeline goes live.

## Security to-dos for the owner

- The n8n workflows contain the **Instantly API key** and the **Supabase service_role key** in plain text.
  Rotate both (after the new system is live, or sooner if others can open the n8n workspace).

## Open questions (proposed defaults, not yet confirmed by the owner)

| Question | Proposed default |
|---|---|
| Apollo keys | Per workspace (each tenant brings its own) |
| Weekly reports and alerts email | One platform SendGrid account and sending domain |
| Models | **Decided:** each workspace picks from `GAZI_LLM_MODELS`; defaults gpt-5.5 (writing) and gpt-4o-mini (sorting/scoring) |
| Monthly LLM token cap per workspace | **Decided:** 2M included with the $99 plan, plus monthly packs (+2M $49, +5M $99, +12M $199). Over the cap: AI writing and drafting pause, nothing else. Calendar month, UTC. |
| Auto-send | **Decided:** a per-workspace on/off switch (off by default). When on, Gazi still holds a draft below the confidence threshold (0.85). |
| Billing / plans | **Decided 2026-11-20, built:** one plan, $99/month USD per workspace, 14-day trial with card up front (one trial per workspace), 7 days of grace after a failed payment or cancellation, then launches and AI pause (data stays readable). Stripe Tax off until go-live. |

## Billing notes

- Stripe sandbox product `prod_Vf91FyWl5If50d`, price `price_1Udoj5K75IMqipLxWl7pVFJj` (lookup key
  `gazi_standard_monthly`). Live mode needs its own product, price, keys and webhook endpoint.
- Keys live in `infra/.env` locally; Docker secrets in production. Production refuses to start without them.
- The Stripe webhook is applied inline (one row update), not queued, because there are no workers yet. It can
  move to the queue once the worker exists.
- Token packs: sandbox prices `price_1Udp7rK75IMqipLxSD2Gpiyl` (+2M), `price_1Udp7sK75IMqipLxyBqDlZei` (+5M),
  `price_1Udp7sK75IMqipLxx79JnnQI` (+12M), configured in `GAZI_STRIPE_TOKEN_PACKS`. Adding a pack charges the prorated
  amount now (`always_invoice`, refused if the card fails); adding one during the trial ends the trial.
- Stripe CLI is installed via winget; local testing: `stripe listen --forward-to localhost:8000/webhooks/stripe`.
- The owner should roll the sandbox secret key: it appeared in a CLI error output during testing.

## Superuser notes (decided 2026-11-20)

- Account overview only: workspaces, people, plans, usage, audit history. Never tenant content.
- Added by SQL only (runbook). Password login for now; add 2FA (Supabase MFA, `aal2`) before going live.
- Disabling a login is a GoTrue ban; an access token already issued lasts until it expires (max 1 hour).
- Free access for a paying workspace doesn't stop Stripe billing; refund or cancel in Stripe.

## Owner's preferences

- Plain-language explanations; they are not a developer. Give exact commands, one per block, for PowerShell.
- Design: very clean and elegant, not "AI-looking". Follow `docs/design/design-system.md`.
- They want Claude to do the work end to end and push to GitHub (`mehedi2283/AG-SaaS`).
- Windows PC with Docker Desktop and Node; the repo is at `C:\Users\Babu\Documents\AG-SaaS`.

## Notes for the next session

- Workers: `arq --watch` reloads code but not the job list. After adding a job to `app/workers/jobs.py`, restart
  the worker containers.
- Never run `npm run build` in `frontend/` while the owner's dev server is up (it breaks it); use
  `npx tsc --noEmit` + `npm run lint`, or build the Docker image.
- RLS/API tests run against the local Supabase Postgres as `supabase_admin` (the `postgres` role can't SET ROLE):
  `GAZI_TEST_DSN=postgresql://supabase_admin:postgres@127.0.0.1:54322/postgres uv run pytest`.
- Inside SECURITY DEFINER functions `app.tenant_id()` reads JWT claims (current_user is gazi_definer); functions
  called by workers read `current_setting('app.tenant_id')` instead.
- Campaigns are one channel each (email or LinkedIn); the owner didn't ask for mixed campaigns.
- 2026-11-21: prompts get `{{lead_details}}` (extra Apollo columns from `leads.raw`: seniority, technologies,
  funding, revenue, keywords; at most one used per message). Built-in prompts carry a `version`; `ensure_prompt`
  retires an older built-in ('default') version and installs the new one. Bump the version when changing wording.
- Python 3.13 can't install `pyyaml` without C++ build tools; run the backend with `UV_PYTHON=3.12`.
- The current Stripe CLI needs `--events` on `stripe listen` (the subscription events in
  `app/domain/billing/service.py`).

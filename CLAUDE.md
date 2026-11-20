# Gazi AI: CLAUDE.md

Multi-tenant B2B cold-outreach SaaS. Replaces the old n8n workflows.
Pipeline: leads (Apollo CSV or Apollo API search + LLM buyer scoring) → LLM-personalised sequence
→ Instantly (email) / HeyReach (LinkedIn) → reply webhook → intent classification → auto-reply or
human-approved draft. Plus: Calendly booking tracking and weekly per-campaign report emails.
Instantly/HeyReach are the sending layer: we create their campaigns, sequences and lists via their APIs,
and their sequences send the follow-ups (we never schedule individual sends).

## Stack
- frontend/  Next.js (App Router, TypeScript). Supabase JS for auth only; all data via the API.
- backend/   Python 3.12, FastAPI, Pydantic v2, asyncpg, httpx, structlog. Managed with uv.
- workers    ARQ + Redis (AOF on). Code in backend/app/workers.
- DB/Auth    Self-hosted Supabase (Postgres + GoTrue) via Docker Compose.
- infra/     docker-compose.yml, Nginx, Certbot, SQL migrations in infra/supabase/migrations.
- docs/      architecture.md, ADRs, runbooks.

## Commands
- Local dev:       see docs/runbooks/local-dev.md. In short: npx supabase start --workdir infra, then
                   docker compose -f infra/docker-compose.dev.yml up -d --build (API on :8000, Studio on :54323)
- Backend tests:   cd backend && uv run pytest
- RLS tests only:  cd backend && uv run pytest tests/rls
                   (needs GAZI_TEST_DSN = superuser DSN of a disposable Postgres 15+; fails if unset)
- Lint/type:       cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app
- Worker (local):  cd backend && uv run arq app.workers.settings.DefaultWorker
- Frontend:        cd frontend && npm run dev | npm run lint | npm run build
- New migration:   add infra/supabase/migrations/<YYYYMMDDHHMM>_<name>.sql (never edit an applied one)

## Non-negotiable rules
1. Multi-tenant everywhere
   - Every table has `tenant_id uuid not null default app.tenant_id()` and RLS ENABLED + FORCED, with policies
     for each operation. Only exemptions: `tenants` (id is the tenant) and `users` (identity spans tenants).
   - No role is granted INSERT/UPDATE on tenant_id; it always comes from the column default.
   - Tenant-to-tenant references are composite FKs on (tenant_id, id).
   - New tables must also: revoke from anon/service_role (default privileges already do this), use
     column-level grants for secret columns, and get a worker policy if workers touch them.
   - The tenant comes ONLY from the verified JWT claim `tenant_id` (injected by `app.custom_access_token_hook`).
   - Never accept tenant_id in a request body, query, path or header. Use `Depends(current_tenant)`.
   - API DB work runs as role `authenticated` with `request.jwt.claims` set per transaction. Never use
     the service role for tenant data.
   - Workers run as `gazi_worker` (no BYPASSRLS) with `app.tenant_id` set by `@tenant_job`.
   - Every new table ships with RLS policies AND a case in tests/rls in the same change.
2. Secrets
   - Per-tenant keys (Instantly, HeyReach), the Calendly link and sender signatures are
     envelope-encrypted (AES-256-GCM, per-tenant DEK, KEK from a Docker secret). Never plaintext, never in code
     or committed env files.
   - OpenAI uses ONE platform key (Docker secret), not per-tenant keys. Before every LLM call, check the
     tenant's monthly token cap against usage_logs; over the cap → fail with error_code `llm_quota_exceeded`.
   - Decrypt only via `app/core/crypto.py`, only in memory. The API returns masked values only.
   - Never log secrets. The structlog redaction processor must stay in the chain.
3. Jobs
   - Every ARQ job takes `tenant_id` as its first argument and is wrapped in `@tenant_job`.
   - Per-tenant token-bucket rate limits and per-tenant concurrency semaphores apply to every provider call.
     Use `Retry(defer=…)` when limited; never sleep-block a worker.
   - Jobs are idempotent (deterministic `_job_id`, status checks under row lock). The Postgres status is the
     source of truth, and the reconciler re-enqueues stuck rows.
   - Replies use their own queue and worker. Never let imports/generation starve them.
4. LLM
   - Log token usage to `usage_logs` for every call (tenant, model, tokens, est. cost, ref).
   - Prompts live in `prompt_templates`, are versioned and immutable. Store `prompt_template_id` and
     `prompt_version` on every generated email and reply.
5. Structured output
   - Use JSON-schema response format, validated by Pydantic models in `app/llm/outputs.py` (`extra="forbid"`).
   - Every prompt has an explicit "You must NOT" section (no sign-off or sender name, no links unless told,
     no invented facts, no unfilled placeholders).
   - `app/llm/guards.py` re-checks these rules in code. Never trust the prompt alone.
6. Reply rules
   - Links in replies (calendar link, attachment_url) are added by code, only when intent is "interested"
     or "question". The model never writes URLs. Every other intent: no links.
   - personalization_2..5: no clickable links (<a> tags, raw URLs, www.). Resources are offered in words.
     The signature image (<img>) is the only URL allowed, in sequence steps and replies. The guard strips
     <img> tags before checking, rejects violations, and the job regenerates.
   - DB backstops: constraints `follow_ups_have_no_links` and `links_only_when_interested_or_question`.
   - Below the confidence threshold, or when tenant policy says so → draft for approval, never auto-send.
7. Webhooks
   - Routes: `/webhooks/instantly/{token}`, `/webhooks/heyreach/{token}`. One token per tenant+provider;
     it is hashed and resolved to the tenant. The campaign comes from the payload and MUST be verified to
     belong to that tenant (else 404 + warning log).
   - Where the provider supports a custom header, also require the per-endpoint shared secret (constant-time compare).
   - Store the raw event in `webhook_events` (dedupe), enqueue, return 200 fast. No LLM calls inline.
8. Defensive coding
   - Normalise CSV headers (strip, NFKC, lowercase, snake_case + Apollo alias map: "First Name" → first_name).
   - Uploaded CSVs are kept on a local volume for 7 days only (for retries/re-runs), then deleted by the
     `purge_imports` cron. Lead data lives in Postgres, not in the file.
   - Duplicate leads (same email already in the tenant, or repeated in the file) never block an import: the
     rest of the import finishes, duplicates go to `lead_duplicates` for user review (skip / update existing /
     add anyway). Unresolved duplicates are excluded from generation and push.
   - Never fail silently: every caught exception is re-raised as a typed error, or logged with structured
     context (tenant_id, job_id, request_id, error_code) and reflected in a row status.
   - API errors are RFC 7807 problem+json with error_code and request_id.
9. Changes
   - Minimal and targeted. No unrequested refactors, renames or new files. Ask before adding dependencies.

## Decisions (2026-11-20)
- Clean start: no migration of the old n8n/Supabase data.
- Intents: interested, not_interested, question, referral, out_of_office, unsubscribe, bounce, other.
- No cross-channel stop: a LinkedIn reply does not pause the Instantly sequence, and vice versa.
- Sequence length is per campaign (sequence_count); each step has its own subject + body.
- Instantly and HeyReach campaigns are created via API by Gazi, not linked by hand.
- In scope for v1: Apollo API lead pulling + buyer scoring, Calendly booking webhooks, weekly report
  emails, error alerts.
- Supabase JWT: current Supabase signs ES256 tokens; verify them against GAZI_JWT_JWKS_URL. HS256 with
  GAZI_JWT_SECRET is accepted only if configured (legacy). Only algorithms with a configured key are
  accepted. Verify ONLY in `app/core/auth.py`. (Changed 2026-11-20 after testing against real Supabase.)

## Provider gotchas
- Instantly: use a direct webhook to our endpoint (the native reply trigger times out). Sequence steps
  are fed by lead custom variables `custom_subject_{n}` and `personalization_{n}` (n = step number).
- HeyReach: conversation lookups are scoped by the SENDING ACCOUNT ID, not the lead ID. Store
  account_id + conversation_id on replies.thread_ref.
- Nginx: client_max_body_size 50m on /v1/lead-imports (large Apollo CSVs). Keep it 1m elsewhere.

## Conventions
- Backend modules: app/domain/<module>/{schemas.py, service.py, repo.py}. Routers in app/api/v1 stay thin.
- SQL lives in repo.py, parameterised only. No ORM.
- Provider clients in app/providers classify errors as retryable (429/5xx/timeout) or permanent.
- Roles: owner > admin > member > viewer. Check them with `require_role("admin")` in the API, mirrored in RLS.
- Writes that change config, membership, prompts or sends also write to audit_log.
- Time: UTC everywhere, timestamptz in the DB.
- Tests: unit for guards/normalisers. Integration against real Postgres for RLS and repos. Provider calls
  mocked with recorded fixtures.
- Commits: small, imperative subject. Never commit .env, keys or the KEK.

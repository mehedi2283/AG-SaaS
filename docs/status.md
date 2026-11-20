# Project status and handoff

Read this first in a new session, after `CLAUDE.md`. It holds what the first build session learned that isn't
in the code: where things stand, what was found in the old n8n setup, open questions and the owner's
preferences. Update it at the end of each session.

Last updated: 2026-11-20.

## Where things stand

| Area | State | Branch |
|---|---|---|
| Schema, RLS, tests (Postgres) | Done. 188 RLS tests. | in `claude/frontend` |
| Backend core (auth, crypto, errors, tenant DB context, workspaces, integrations, webhook endpoints + intake) | Done. 218 tests total, ruff + strict mypy clean. | in `claude/frontend` |
| Local dev (Supabase CLI, Docker, `infra/dev-setup.ps1`) | Done. Verified on the owner's Windows PC. | in `claude/frontend` |
| Frontend: sign-in, workspace setup and switcher, Settings (accounts, webhook addresses) | Done. Verified in a browser against the local stack. | `claude/frontend` |
| Frontend: Replies, Campaigns, Leads | Empty states only; each waits for its backend. | |
| Reply pipeline (worker, classify, draft, guards, send) | **Next.** | |
| Leads (CSV import + duplicates, Apollo search + scoring), sequence generation, launching Instantly/HeyReach campaigns | Not started. | |
| Calendly bookings, weekly reports, error alerts | Not started. | |
| Production deployment (compose, Nginx, Certbot, self-hosted Supabase) | Not started. | |

`claude/frontend` contains all work so far (each branch was built on the previous one). `main` only has the
first two commits until the owner merges.

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
| Models | gpt-5.5 for writing; gpt-4o-mini for classification and scoring |
| Monthly LLM token cap per workspace | 2M tokens |
| Auto-send | Only at confidence >= 0.85; `interested` and `question` always wait for approval; others auto-send |
| Billing / plans | Not in v1 |

## Owner's preferences

- Plain-language explanations; they are not a developer. Give exact commands, one per block, for PowerShell.
- Design: very clean and elegant, not "AI-looking". Follow `docs/design/design-system.md`.
- They want Claude to do the work end to end and push to GitHub (`mehedi2283/AG-SaaS`).
- Windows PC with Docker Desktop and Node; the repo is at `C:\Users\Babu\Documents\AG-SaaS`.

## Next: reply pipeline

1. ARQ worker settings and `@tenant_job` (sets `app.tenant_id`, binds logs, checks tenant status,
   per-tenant semaphore and rate limit).
2. `handle_webhook_event`: parse Instantly/HeyReach events, match the lead, insert `replies` (dedupe).
3. LLM client (platform OpenAI key, token cap check, `usage_logs`), Pydantic outputs, guards.
4. Classify intent, draft the reply, add links by code (interested/question only), policy → send or hold.
5. `send_reply` via Instantly / HeyReach clients; reconciler cron.
6. API + Replies screen (inbox, draft editing, approve/dismiss), per the prototype.

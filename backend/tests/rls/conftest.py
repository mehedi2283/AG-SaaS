"""RLS test harness.

Creates a throwaway database, applies the test-only Supabase shim plus every migration in
infra/supabase/migrations (in filename order), seeds two tenants with one user per role, and
exposes helpers that run SQL exactly as the API (role `authenticated` + verified JWT claims) or a
worker (role `gazi_worker` + app.tenant_id) would.

Requires GAZI_TEST_DSN: a superuser DSN for a disposable Postgres 15+ server, e.g.
  GAZI_TEST_DSN=postgresql://postgres:postgres@127.0.0.1:5432/postgres
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "infra" / "supabase" / "migrations"
SHIM = Path(__file__).with_name("supabase_shim.sql")

ROLES = ("owner", "admin", "member", "viewer")
ROLE_RANK = {"viewer": 1, "member": 2, "admin": 3, "owner": 4}


@dataclass
class TenantSeed:
    id: uuid.UUID
    users: dict[str, uuid.UUID] = field(default_factory=dict)  # role -> user id
    rows: dict[str, uuid.UUID] = field(default_factory=dict)  # table -> one row id
    webhook_token: str = ""


@dataclass
class Seed:
    a: TenantSeed
    b: TenantSeed
    outsider: uuid.UUID


@pytest.fixture(scope="session")
def dsn() -> Iterator[str]:
    admin_dsn = os.environ.get("GAZI_TEST_DSN")
    if not admin_dsn:
        pytest.fail(
            "GAZI_TEST_DSN is not set. The RLS suite must run against a real Postgres; it is never skipped silently.",
            pytrace=False,
        )
    db_name = f"gazi_rls_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(db_name)))
    test_dsn = make_conninfo(**{**conninfo_to_dict(admin_dsn), "dbname": db_name})
    try:
        with psycopg.connect(test_dsn, autocommit=True) as conn:
            conn.execute(SHIM.read_text())
            migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
            assert migrations, f"no migrations found in {MIGRATIONS_DIR}"
            for path in migrations:
                try:
                    conn.execute(path.read_text())
                except psycopg.Error as exc:
                    raise RuntimeError(f"migration {path.name} failed: {exc}") from exc
        yield test_dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(db_name)))


@pytest.fixture(scope="session")
def seed(dsn: str) -> Seed:
    with psycopg.connect(dsn, autocommit=True) as conn:
        a, b = _seed_tenant(conn, "alpha"), _seed_tenant(conn, "bravo")
        outsider = _create_auth_user(conn, "outsider@example.com")
    return Seed(a=a, b=b, outsider=outsider)


@pytest.fixture
def conn(dsn: str, seed: Seed) -> Iterator[psycopg.Connection]:
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Execution contexts. Every context is one transaction that is ALWAYS rolled back.
# ---------------------------------------------------------------------------


@contextmanager
def as_user(
    conn: psycopg.Connection,
    user_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    *,
    setup: list[str] | None = None,
) -> Iterator[psycopg.Connection]:
    """Run as the API does: role `authenticated` with claims from a verified JWT."""
    claims: dict[str, object] = {"role": "authenticated", "aud": "authenticated"}
    if user_id is not None:
        claims["sub"] = str(user_id)
    if tenant_id is not None:
        claims["tenant_id"] = str(tenant_id)
    conn.execute("begin")
    try:
        for stmt in setup or []:
            conn.execute(stmt)
        conn.execute("set local role authenticated")
        conn.execute("select set_config('request.jwt.claims', %s, true)", [json.dumps(claims)])
        yield conn
    finally:
        conn.execute("rollback")


@contextmanager
def as_worker(conn: psycopg.Connection, tenant_id: uuid.UUID | None) -> Iterator[psycopg.Connection]:
    """Run as an ARQ job does: role `gazi_worker` with app.tenant_id set by @tenant_job."""
    conn.execute("begin")
    try:
        conn.execute("set local role gazi_worker")
        if tenant_id is not None:
            conn.execute("select set_config('app.tenant_id', %s, true)", [str(tenant_id)])
        yield conn
    finally:
        conn.execute("rollback")


@contextmanager
def as_role(conn: psycopg.Connection, role: str) -> Iterator[psycopg.Connection]:
    """Run as a bare database role with no claims (anon, service_role, supabase_auth_admin)."""
    conn.execute("begin")
    try:
        conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
        yield conn
    finally:
        conn.execute("rollback")


def scalar(conn: psycopg.Connection, query: str, params: list[object] | None = None) -> object:
    row = conn.execute(query, params or []).fetchone()
    assert row is not None
    return row[0]


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


# ---------------------------------------------------------------------------
# Seeding (runs as the superuser, which bypasses RLS)
# ---------------------------------------------------------------------------


def _create_auth_user(conn: psycopg.Connection, email: str) -> uuid.UUID:
    row = conn.execute("insert into auth.users (email) values (%s) returning id", [email]).fetchone()
    assert row is not None
    return row[0]


def _seed_tenant(conn: psycopg.Connection, slug: str) -> TenantSeed:
    def ins(query: str, params: list[object]) -> uuid.UUID:
        row = conn.execute(query, params).fetchone()
        assert row is not None
        return row[0]

    t = TenantSeed(
        id=ins(
            "insert into public.tenants (name, slug, wrapped_dek) values (%s, %s, %s) returning id",
            [slug.title(), slug, os.urandom(60)],
        )
    )
    for role in ROLES:
        t.users[role] = _create_auth_user(conn, f"{role}@{slug}.example.com")

    # Memberships are inserted the way create_tenant does it (as gazi_definer), so the owner rules
    # trigger runs for real.
    conn.execute("begin")
    conn.execute("set local role gazi_definer")
    for role in ROLES:
        conn.execute(
            "insert into public.memberships (tenant_id, user_id, role, status) values (%s, %s, %s, 'active')",
            [t.id, t.users[role], role],
        )
    conn.execute("commit")

    tid = t.id
    t.rows["integrations"] = ins(
        "insert into public.integrations (tenant_id, provider, config_ciphertext, config_nonce, dek_version,"
        " key_last4) values (%s, 'instantly', %s, %s, 1, 'abcd') returning id",
        [tid, os.urandom(48), os.urandom(12)],
    )
    t.rows["sender_identities"] = ins(
        "insert into public.sender_identities (tenant_id, display_name, signature_ciphertext, signature_nonce,"
        " dek_version, is_default) values (%s, 'Sam Sender', %s, %s, 1, true) returning id",
        [tid, os.urandom(32), os.urandom(12)],
    )
    t.rows["prompt_templates"] = ins(
        "insert into public.prompt_templates (tenant_id, kind, name, version, system_prompt, user_template,"
        " model, is_active) values (%s, 'email_step', 'default', 1,"
        " 'Write a cold email. You must NOT add a sign-off.', '{{first_name}}', 'gpt-5.5', true) returning id",
        [tid],
    )
    t.rows["campaigns"] = ins(
        "insert into public.campaigns (tenant_id, name, channel, instantly_campaign_id, prompt_template_id,"
        " sender_identity_id) values (%s, 'Q1 outbound', 'email', %s, %s, %s) returning id",
        [tid, f"inst-{slug}", t.rows["prompt_templates"], t.rows["sender_identities"]],
    )
    t.rows["lead_imports"] = ins(
        "insert into public.lead_imports (tenant_id, campaign_id, original_filename, file_path)"
        " values (%s, %s, 'apollo.csv', '/data/imports/x.csv') returning id",
        [tid, t.rows["campaigns"]],
    )
    t.rows["leads"] = ins(
        "insert into public.leads (tenant_id, campaign_id, email, first_name, source_import_id)"
        " values (%s, %s, %s, 'Lee', %s) returning id",
        [tid, t.rows["campaigns"], f"lee@{slug}-prospect.com", t.rows["lead_imports"]],
    )
    t.rows["lead_duplicates"] = ins(
        "insert into public.lead_duplicates (tenant_id, import_id, existing_lead_id, incoming_email, incoming,"
        " reason) values (%s, %s, %s, %s, '{}', 'exists_in_tenant') returning id",
        [tid, t.rows["lead_imports"], t.rows["leads"], f"lee@{slug}-prospect.com"],
    )
    t.rows["campaign_steps"] = ins(
        "insert into public.campaign_steps (tenant_id, campaign_id, step, delay_days, angle)"
        " values (%s, %s, 1, 0, 'hook and soft CTA') returning id",
        [tid, t.rows["campaigns"]],
    )
    t.rows["lead_messages"] = ins(
        "insert into public.lead_messages (tenant_id, lead_id, campaign_id, step, channel, subject, body,"
        " prompt_template_id, prompt_version, model, status) values (%s, %s, %s, 1, 'email', 'Hi',"
        " 'Saw https://example.com', %s, 1, 'gpt-5.5', 'ok') returning id",
        [tid, t.rows["leads"], t.rows["campaigns"], t.rows["prompt_templates"]],
    )
    t.webhook_token = f"tok_{slug}_{uuid.uuid4().hex}"
    t.rows["webhook_endpoints"] = ins(
        "insert into public.webhook_endpoints (tenant_id, provider, token_hash, token_prefix)"
        " values (%s, 'instantly', %s, %s) returning id",
        [tid, token_hash(t.webhook_token), t.webhook_token[:8]],
    )
    t.rows["webhook_events"] = ins(
        "insert into public.webhook_events (tenant_id, provider, endpoint_id, dedupe_key, payload)"
        " values (%s, 'instantly', %s, 'evt-1', '{}') returning id",
        [tid, t.rows["webhook_endpoints"]],
    )
    t.rows["replies"] = ins(
        "insert into public.replies (tenant_id, lead_id, campaign_id, webhook_event_id, channel,"
        " provider_message_id, inbound_body, intent, confidence) values (%s, %s, %s, %s, 'email', 'msg-1',"
        " 'Sounds good', 'interested', 0.9) returning id",
        [tid, t.rows["leads"], t.rows["campaigns"], t.rows["webhook_events"]],
    )
    t.rows["bookings"] = ins(
        "insert into public.bookings (tenant_id, campaign_id, lead_id, webhook_event_id, external_id, invitee_email)"
        " values (%s, %s, %s, %s, %s, %s) returning id",
        [
            tid,
            t.rows["campaigns"],
            t.rows["leads"],
            t.rows["webhook_events"],
            f"inv-{slug}",
            f"lee@{slug}-prospect.com",
        ],
    )
    t.rows["campaign_reports"] = ins(
        "insert into public.campaign_reports (tenant_id, campaign_id, period_start, period_end)"
        " values (%s, %s, '2026-11-09', '2026-11-15') returning id",
        [tid, t.rows["campaigns"]],
    )
    t.rows["subscriptions"] = ins(
        "insert into public.subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, status)"
        " values (%s, %s, %s, 'active') returning id",
        [tid, f"cus_{slug}", f"sub_{slug}"],
    )
    t.rows["billing_events"] = ins(
        "insert into public.billing_events (tenant_id, stripe_event_id, type, payload)"
        " values (%s, %s, 'customer.subscription.created', '{}') returning id",
        [tid, f"evt_{slug}"],
    )
    t.rows["usage_logs"] = ins(
        "insert into public.usage_logs (tenant_id, kind, provider, model, prompt_tokens, completion_tokens)"
        " values (%s, 'llm', 'openai', 'gpt-4o', 100, 50) returning id",
        [tid],
    )
    t.rows["audit_log"] = ins(
        "insert into public.audit_log (tenant_id, actor_kind, action) values (%s, 'system', 'seed') returning id",
        [tid],
    )
    return t

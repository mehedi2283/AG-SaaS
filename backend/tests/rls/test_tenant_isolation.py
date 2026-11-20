"""Behavioural RLS tests: two tenants x four roles x every table, plus workers, hook and integrity rules."""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest
from psycopg import errors, sql

from .conftest import (
    ROLE_RANK,
    ROLES,
    Seed,
    as_role,
    as_user,
    as_worker,
    scalar,
    token_hash,
)

# Minimum role needed to READ each tenant table.
READ_ROLE = {
    "memberships": "viewer",
    "integrations": "admin",
    "sender_identities": "viewer",
    "prompt_templates": "viewer",
    "campaigns": "viewer",
    "lead_imports": "viewer",
    "leads": "viewer",
    "lead_duplicates": "viewer",
    "generated_emails": "viewer",
    "webhook_endpoints": "admin",
    "webhook_events": "admin",
    "replies": "viewer",
    "usage_logs": "admin",
    "audit_log": "admin",
}

# A column each table lets `authenticated` UPDATE (used for no-op cross-tenant update attempts).
UPDATABLE = {
    "memberships": "status",
    "integrations": "last_error",
    "sender_identities": "title",
    "prompt_templates": "is_active",
    "campaigns": "name",
    "leads": "title",
    "lead_duplicates": "resolution",
    "generated_emails": "subject",
    "replies": "draft_body",
    "webhook_endpoints": "revoked_at",
}
DELETABLE = ["memberships", "integrations", "sender_identities", "campaigns", "leads"]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("table", sorted(READ_ROLE))
def test_reads_are_scoped_to_active_tenant_and_role(
    conn: psycopg.Connection, seed: Seed, table: str, role: str
) -> None:
    with as_user(conn, seed.a.users[role], seed.a.id) as c:
        tenants = {
            r[0]
            for r in c.execute(
                sql.SQL("select tenant_id from public.{}").format(sql.Identifier(table))
            )
        }
    if ROLE_RANK[role] >= ROLE_RANK[READ_ROLE[table]]:
        assert tenants == {seed.a.id}, (
            f"{role} should see only tenant A rows in {table}"
        )
    else:
        assert tenants == set(), f"{role} must not read {table}"


@pytest.mark.parametrize("role", ROLES)
def test_tenants_and_users_visibility(
    conn: psycopg.Connection, seed: Seed, role: str
) -> None:
    with as_user(conn, seed.a.users[role], seed.a.id) as c:
        assert {r[0] for r in c.execute("select id from public.tenants")} == {seed.a.id}
        visible_users = {r[0] for r in c.execute("select id from public.users")}
    assert visible_users == set(seed.a.users.values())
    assert not visible_users & set(seed.b.users.values())


@pytest.mark.parametrize("table", sorted(READ_ROLE))
def test_forged_tenant_claim_sees_nothing(
    conn: psycopg.Connection, seed: Seed, table: str
) -> None:
    """A JWT naming tenant B for a user who only belongs to A must yield nothing (membership re-check)."""
    with as_user(conn, seed.a.users["owner"], seed.b.id) as c:
        count = scalar(
            c,
            sql.SQL("select count(*) from public.{}")
            .format(sql.Identifier(table))
            .as_string(c),
        )
    assert count == 0


def test_outsider_and_missing_claims_see_nothing(
    conn: psycopg.Connection, seed: Seed
) -> None:
    for user, tenant in [
        (seed.outsider, seed.a.id),
        (seed.a.users["owner"], None),
        (None, seed.a.id),
    ]:
        with as_user(conn, user, tenant) as c:
            assert scalar(c, "select count(*) from public.campaigns") == 0
            assert scalar(c, "select count(*) from public.tenants") == 0


def test_removed_membership_loses_access_immediately(
    conn: psycopg.Connection, seed: Seed
) -> None:
    viewer = seed.a.users["viewer"]
    setup = [f"delete from public.memberships where user_id = '{viewer}'"]
    with as_user(conn, viewer, seed.a.id, setup=setup) as c:
        assert scalar(c, "select count(*) from public.leads") == 0


def test_suspended_tenant_locks_out_users(conn: psycopg.Connection, seed: Seed) -> None:
    setup = [f"update public.tenants set status = 'suspended' where id = '{seed.a.id}'"]
    with as_user(conn, seed.a.users["owner"], seed.a.id, setup=setup) as c:
        assert scalar(c, "select count(*) from public.campaigns") == 0


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(UPDATABLE))
def test_cross_tenant_update_affects_nothing(
    conn: psycopg.Connection, seed: Seed, table: str
) -> None:
    col = sql.Identifier(UPDATABLE[table])
    query = sql.SQL("update public.{t} set {c} = {c} where tenant_id = %s").format(
        t=sql.Identifier(table), c=col
    )
    with as_user(conn, seed.a.users["owner"], seed.a.id) as c:
        assert c.execute(query, [seed.b.id]).rowcount == 0


@pytest.mark.parametrize("table", DELETABLE)
def test_cross_tenant_delete_affects_nothing(
    conn: psycopg.Connection, seed: Seed, table: str
) -> None:
    query = sql.SQL("delete from public.{} where tenant_id = %s").format(
        sql.Identifier(table)
    )
    with as_user(conn, seed.a.users["owner"], seed.a.id) as c:
        assert c.execute(query, [seed.b.id]).rowcount == 0


def test_tenant_id_cannot_be_supplied_and_defaults_to_jwt(
    conn: psycopg.Connection, seed: Seed
) -> None:
    with (
        as_user(conn, seed.a.users["member"], seed.a.id) as c,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        c.execute(
            "insert into public.campaigns (tenant_id, name, channel) values (%s, 'x', 'email')",
            [seed.b.id],
        )
    with as_user(conn, seed.a.users["member"], seed.a.id) as c:
        tid = scalar(
            c,
            "insert into public.campaigns (name, channel) values ('New', 'email') returning tenant_id",
        )
    assert tid == seed.a.id


def test_forged_tenant_claim_cannot_insert(
    conn: psycopg.Connection, seed: Seed
) -> None:
    with (
        as_user(conn, seed.a.users["owner"], seed.b.id) as c,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        c.execute("insert into public.campaigns (name, channel) values ('x', 'email')")


@pytest.mark.parametrize(
    ("role", "allowed"),
    [("viewer", False), ("member", True), ("admin", True), ("owner", True)],
)
def test_campaign_insert_needs_member(
    conn: psycopg.Connection, seed: Seed, role: str, allowed: bool
) -> None:
    with as_user(conn, seed.a.users[role], seed.a.id) as c:
        if allowed:
            c.execute(
                "insert into public.campaigns (name, channel) values ('x', 'email')"
            )
        else:
            with pytest.raises(errors.InsufficientPrivilege):
                c.execute(
                    "insert into public.campaigns (name, channel) values ('x', 'email')"
                )


@pytest.mark.parametrize(("role", "deleted"), [("member", 0), ("admin", 1)])
def test_campaign_delete_needs_admin(
    conn: psycopg.Connection, seed: Seed, role: str, deleted: int
) -> None:
    setup = [
        f"delete from public.campaigns where id <> '{seed.a.rows['campaigns']}' and tenant_id = '{seed.a.id}'"
    ]
    with as_user(conn, seed.a.users[role], seed.a.id, setup=setup) as c:
        assert (
            c.execute(
                "delete from public.campaigns where id = %s", [seed.a.rows["campaigns"]]
            ).rowcount
            == deleted
        )


@pytest.mark.parametrize(("role", "allowed"), [("member", False), ("admin", True)])
def test_integration_write_needs_admin(
    conn: psycopg.Connection, seed: Seed, role: str, allowed: bool
) -> None:
    stmt = (
        "insert into public.integrations (provider, config_ciphertext, config_nonce, dek_version)"
        " values ('heyreach', '\\x00', %s, 1)"
    )
    with as_user(conn, seed.a.users[role], seed.a.id) as c:
        if allowed:
            c.execute(stmt, [b"\x00" * 12])
        else:
            with pytest.raises(errors.InsufficientPrivilege):
                c.execute(stmt, [b"\x00" * 12])


@pytest.mark.parametrize(("role", "deleted"), [("admin", 0), ("owner", 1)])
def test_integration_delete_needs_owner(
    conn: psycopg.Connection, seed: Seed, role: str, deleted: int
) -> None:
    with as_user(conn, seed.a.users[role], seed.a.id) as c:
        assert c.execute("delete from public.integrations").rowcount == deleted


# ---------------------------------------------------------------------------
# Secret columns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("tenants", "wrapped_dek"),
        ("integrations", "config_ciphertext"),
        ("integrations", "config_nonce"),
        ("sender_identities", "signature_ciphertext"),
        ("webhook_endpoints", "token_hash"),
        ("webhook_endpoints", "secret_ciphertext"),
    ],
)
def test_secret_columns_are_not_readable_by_clients(
    conn: psycopg.Connection, seed: Seed, table: str, column: str
) -> None:
    query = sql.SQL("select {} from public.{}").format(
        sql.Identifier(column), sql.Identifier(table)
    )
    with (
        as_user(conn, seed.a.users["owner"], seed.a.id) as c,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        c.execute(query)


def test_wrapped_dek_only_via_admin_function(
    conn: psycopg.Connection, seed: Seed
) -> None:
    with as_user(conn, seed.a.users["admin"], seed.a.id) as c:
        assert len(c.execute("select * from app.current_tenant_dek()").fetchall()) == 1
    with as_user(conn, seed.a.users["member"], seed.a.id) as c:
        assert c.execute("select * from app.current_tenant_dek()").fetchall() == []


@pytest.mark.parametrize("role", ["anon", "service_role"])
def test_anon_and_service_role_are_denied(
    conn: psycopg.Connection, seed: Seed, role: str
) -> None:
    with as_role(conn, role) as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute("select count(*) from public.leads")


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def test_worker_is_scoped_to_job_tenant(conn: psycopg.Connection, seed: Seed) -> None:
    with as_worker(conn, seed.a.id) as c:
        assert {r[0] for r in c.execute("select tenant_id from public.leads")} == {
            seed.a.id
        }
        assert {r[0] for r in c.execute("select id from public.tenants")} == {seed.a.id}
        assert (
            c.execute(
                "update public.replies set status = 'classified' where tenant_id = %s",
                [seed.b.id],
            ).rowcount
            == 0
        )
        tid = scalar(
            c,
            "insert into public.usage_logs (kind, provider, model, prompt_tokens) values ('llm','openai','gpt-4o',1)"
            " returning tenant_id",
        )
        assert tid == seed.a.id
    with as_worker(conn, None) as c:
        assert scalar(c, "select count(*) from public.leads") == 0


def test_worker_cannot_touch_memberships_or_delete(
    conn: psycopg.Connection, seed: Seed
) -> None:
    with as_worker(conn, seed.a.id) as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute("select * from public.memberships")
    with as_worker(conn, seed.a.id) as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute("delete from public.leads")


def test_worker_insert_into_other_tenant_is_rejected(
    conn: psycopg.Connection, seed: Seed
) -> None:
    with as_worker(conn, seed.a.id) as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute(
            "insert into public.leads (tenant_id, campaign_id, email) values (%s, %s, 'x@y.com')",
            [seed.b.id, seed.b.rows["campaigns"]],
        )


# ---------------------------------------------------------------------------
# Webhook token resolution
# ---------------------------------------------------------------------------


def test_webhook_token_resolves_to_its_tenant(
    conn: psycopg.Connection, seed: Seed
) -> None:
    with as_worker(conn, None) as c:
        row = c.execute(
            "select endpoint_id, tenant_id, provider from app.resolve_webhook_token(%s)",
            [token_hash(seed.b.webhook_token)],
        ).fetchone()
        assert row == (seed.b.rows["webhook_endpoints"], seed.b.id, "instantly")
        assert (
            c.execute(
                "select * from app.resolve_webhook_token(%s)", [token_hash("bogus")]
            ).fetchall()
            == []
        )


def test_revoked_webhook_token_resolves_to_nothing(
    conn: psycopg.Connection, seed: Seed
) -> None:
    conn.execute("begin")
    try:
        conn.execute(
            "update public.webhook_endpoints set revoked_at = now() where id = %s",
            [seed.a.rows["webhook_endpoints"]],
        )
        conn.execute("set local role gazi_worker")
        rows = conn.execute(
            "select * from app.resolve_webhook_token(%s)",
            [token_hash(seed.a.webhook_token)],
        ).fetchall()
        assert rows == []
    finally:
        conn.execute("rollback")


def test_clients_cannot_resolve_webhook_tokens(
    conn: psycopg.Connection, seed: Seed
) -> None:
    with (
        as_user(conn, seed.a.users["owner"], seed.a.id) as c,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        c.execute(
            "select * from app.resolve_webhook_token(%s)",
            [token_hash(seed.a.webhook_token)],
        )


# ---------------------------------------------------------------------------
# Access-token hook
# ---------------------------------------------------------------------------


def _hook(
    conn: psycopg.Connection,
    user_id: uuid.UUID,
    active_tenant: str | None,
    extra: dict | None = None,
) -> dict:
    claims = {"sub": str(user_id), "app_metadata": {}, **(extra or {})}
    if active_tenant is not None:
        claims["app_metadata"] = {"active_tenant_id": active_tenant}
    event = {"user_id": str(user_id), "claims": claims}
    with as_role(conn, "supabase_auth_admin") as c:
        result = scalar(
            c, "select app.custom_access_token_hook(%s::jsonb)", [json.dumps(event)]
        )
    assert isinstance(result, dict)
    return result["claims"]


def test_hook_injects_selected_tenant_for_a_member(
    conn: psycopg.Connection, seed: Seed
) -> None:
    claims = _hook(conn, seed.b.users["admin"], str(seed.b.id))
    assert claims["tenant_id"] == str(seed.b.id) and claims["tenant_role"] == "admin"


def test_hook_ignores_selection_without_membership(
    conn: psycopg.Connection, seed: Seed
) -> None:
    claims = _hook(conn, seed.a.users["member"], str(seed.b.id))
    assert claims["tenant_id"] == str(
        seed.a.id
    )  # falls back to the user's own membership
    assert claims["tenant_role"] == "member"


def test_hook_strips_forged_claims_for_outsider(
    conn: psycopg.Connection, seed: Seed
) -> None:
    claims = _hook(
        conn,
        seed.outsider,
        str(seed.a.id),
        {"tenant_id": str(seed.a.id), "tenant_role": "owner"},
    )
    assert "tenant_id" not in claims and "tenant_role" not in claims


def test_hook_not_executable_by_clients(conn: psycopg.Connection, seed: Seed) -> None:
    with (
        as_user(conn, seed.a.users["owner"], seed.a.id) as c,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        c.execute("select app.custom_access_token_hook('{}'::jsonb)")


# ---------------------------------------------------------------------------
# Tenant creation and owner rules
# ---------------------------------------------------------------------------


def test_create_tenant_makes_caller_owner(conn: psycopg.Connection, seed: Seed) -> None:
    conn.execute("begin")
    try:
        conn.execute("set local role authenticated")
        conn.execute(
            "select set_config('request.jwt.claims', %s, true)",
            [json.dumps({"sub": str(seed.outsider)})],
        )
        tid = scalar(
            conn, "select app.create_tenant('Charlie', 'charlie', %s)", [b"\x01" * 60]
        )
        conn.execute("reset role")
        role = scalar(
            conn,
            "select role from public.memberships where tenant_id = %s and user_id = %s",
            [tid, seed.outsider],
        )
        assert role == "owner"
    finally:
        conn.execute("rollback")


def test_anon_cannot_create_tenant(conn: psycopg.Connection, seed: Seed) -> None:
    with as_role(conn, "anon") as c, pytest.raises(errors.InsufficientPrivilege):
        c.execute("select app.create_tenant('X', 'xx', %s)", [b"\x01" * 60])


def test_admin_cannot_grant_owner(conn: psycopg.Connection, seed: Seed) -> None:
    with (
        as_user(conn, seed.a.users["admin"], seed.a.id) as c,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        c.execute(
            "update public.memberships set role = 'owner' where user_id = %s",
            [seed.a.users["member"]],
        )


def test_last_owner_cannot_leave(conn: psycopg.Connection, seed: Seed) -> None:
    with (
        as_user(conn, seed.a.users["owner"], seed.a.id) as c,
        pytest.raises(errors.CheckViolation),
    ):
        c.execute(
            "update public.memberships set role = 'admin' where user_id = %s",
            [seed.a.users["owner"]],
        )


def test_owner_can_hand_over_ownership(conn: psycopg.Connection, seed: Seed) -> None:
    with as_user(conn, seed.a.users["owner"], seed.a.id) as c:
        c.execute(
            "update public.memberships set role = 'owner' where user_id = %s",
            [seed.a.users["admin"]],
        )
        c.execute(
            "update public.memberships set role = 'admin' where user_id = %s",
            [seed.a.users["owner"]],
        )


# ---------------------------------------------------------------------------
# Integrity rules (append-only, immutable prompts, rule 6 backstops, composite FKs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", ["usage_logs", "audit_log"])
def test_append_only_tables_reject_update_and_delete(
    conn: psycopg.Connection, seed: Seed, table: str
) -> None:
    for stmt in ("update public.{} set created_at = now()", "delete from public.{}"):
        conn.execute("begin")
        try:
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(
                    sql.SQL(stmt).format(sql.Identifier(table))
                )  # even as superuser
        finally:
            conn.execute("rollback")


def test_prompt_versions_are_immutable(conn: psycopg.Connection, seed: Seed) -> None:
    pid = seed.a.rows["prompt_templates"]
    with (
        as_user(conn, seed.a.users["admin"], seed.a.id) as c,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        c.execute(
            "update public.prompt_templates set system_prompt = 'x must not' where id = %s",
            [pid],
        )
    conn.execute("begin")
    try:
        with pytest.raises(errors.InsufficientPrivilege):  # trigger, even as superuser
            conn.execute(
                "update public.prompt_templates set system_prompt = 'x must not' where id = %s",
                [pid],
            )
    finally:
        conn.execute("rollback")
    with as_user(conn, seed.a.users["admin"], seed.a.id) as c:
        assert (
            c.execute(
                "update public.prompt_templates set is_active = false where id = %s",
                [pid],
            ).rowcount
            == 1
        )


def test_prompt_must_state_prohibitions(conn: psycopg.Connection, seed: Seed) -> None:
    with (
        as_user(conn, seed.a.users["admin"], seed.a.id) as c,
        pytest.raises(errors.CheckViolation),
    ):
        c.execute(
            "insert into public.prompt_templates (kind, name, version, system_prompt, user_template, model)"
            " values ('email_gen', 'loose', 1, 'Write an email.', '', 'gpt-4o')"
        )


@pytest.mark.parametrize("n", [2, 3, 4, 5])
def test_follow_ups_cannot_contain_links(
    conn: psycopg.Connection, seed: Seed, n: int
) -> None:
    col = sql.Identifier(f"personalization_{n}")
    with (
        as_user(conn, seed.a.users["member"], seed.a.id) as c,
        pytest.raises(errors.CheckViolation),
    ):
        c.execute(
            sql.SQL(
                "update public.generated_emails set {} = 'book here: www.cal.com/x' where id = %s"
            ).format(col),
            [seed.a.rows["generated_emails"]],
        )


SIGNATURE = (
    '<img src="https://cdn.example.com/sig.png" width="600" alt="Email Signature" />'
)


@pytest.mark.parametrize("n", [2, 3, 4, 5])
def test_follow_ups_reject_anchor_tags(
    conn: psycopg.Connection, seed: Seed, n: int
) -> None:
    col = sql.Identifier(f"personalization_{n}")
    with (
        as_user(conn, seed.a.users["member"], seed.a.id) as c,
        pytest.raises(errors.CheckViolation),
    ):
        c.execute(
            sql.SQL("update public.generated_emails set {} = %s where id = %s").format(
                col
            ),
            [
                '<p>See <a href="/case-study">the breakdown</a></p>',
                seed.a.rows["generated_emails"],
            ],
        )


def test_follow_ups_allow_signature_image(conn: psycopg.Connection, seed: Seed) -> None:
    with as_user(conn, seed.a.users["member"], seed.a.id) as c:
        updated = c.execute(
            "update public.generated_emails set personalization_2 = %s, personalization_5 = %s where id = %s",
            [
                f"<p>Quick follow-up.</p>{SIGNATURE}",
                SIGNATURE,
                seed.a.rows["generated_emails"],
            ],
        ).rowcount
    assert updated == 1


@pytest.mark.parametrize(
    ("intent", "allowed"),
    [
        ("interested", True),
        ("question", True),
        ("not_interested", False),
        ("out_of_office", False),
        ("other", False),
    ],
)
def test_reply_links_only_for_interested_or_question(
    conn: psycopg.Connection, seed: Seed, intent: str, allowed: bool
) -> None:
    rid = seed.a.rows["replies"]
    pid = seed.a.rows["prompt_templates"]
    setup = [
        (
            f"update public.replies set prompt_template_id = '{pid}', prompt_version = 1, intent = '{intent}'"
            f" where id = '{rid}'"
        )
    ]
    stmt = "update public.replies set draft_body = %s where id = %s"
    body = '<p>Grab a slot: <a href="https://cal.com/x">here</a></p>'
    with as_user(conn, seed.a.users["member"], seed.a.id, setup=setup) as c:
        if allowed:
            c.execute(stmt, [body, rid])
        else:
            with pytest.raises(errors.CheckViolation):
                c.execute(stmt, [body, rid])


@pytest.mark.parametrize("new_intent", ["not_interested", None])
def test_intent_change_cannot_keep_links(
    conn: psycopg.Connection, seed: Seed, new_intent: str | None
) -> None:
    rid = seed.a.rows["replies"]
    pid = seed.a.rows["prompt_templates"]
    setup = [
        f"update public.replies set prompt_template_id = '{pid}', prompt_version = 1 where id = '{rid}'"
    ]
    with as_user(conn, seed.a.users["member"], seed.a.id, setup=setup) as c:
        c.execute(
            "update public.replies set draft_body = 'Grab a slot: https://cal.com/x' where id = %s",
            [rid],
        )
        with pytest.raises(errors.CheckViolation):
            c.execute(
                "update public.replies set intent = %s where id = %s", [new_intent, rid]
            )


def test_signature_image_allowed_on_any_reply(
    conn: psycopg.Connection, seed: Seed
) -> None:
    rid = seed.a.rows["replies"]
    pid = seed.a.rows["prompt_templates"]
    setup = [
        (
            f"update public.replies set prompt_template_id = '{pid}', prompt_version = 1, intent = 'not_interested'"
            f" where id = '{rid}'"
        )
    ]
    with as_user(conn, seed.a.users["member"], seed.a.id, setup=setup) as c:
        c.execute(
            "update public.replies set draft_body = %s where id = %s",
            [f"<p>Understood.</p>{SIGNATURE}", rid],
        )


def test_rows_cannot_reference_another_tenants_rows(
    conn: psycopg.Connection, seed: Seed
) -> None:
    conn.execute("begin")
    try:
        with pytest.raises(errors.ForeignKeyViolation):  # even as superuser
            conn.execute(
                "insert into public.campaigns (tenant_id, name, channel, prompt_template_id)"
                " values (%s, 'x', 'email', %s)",
                [seed.a.id, seed.b.rows["prompt_templates"]],
            )
    finally:
        conn.execute("rollback")


# ---------------------------------------------------------------------------
# Audit fixes (202611200005)
# ---------------------------------------------------------------------------


def test_approval_is_signed_by_the_caller(conn: psycopg.Connection, seed: Seed) -> None:
    gid = seed.a.rows["generated_emails"]
    member, owner = seed.a.users["member"], seed.a.users["owner"]
    stmt = "update public.generated_emails set status = 'approved', approved_by = %s where id = %s"
    with (
        as_user(conn, member, seed.a.id) as c,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        c.execute(stmt, [owner, gid])
    with as_user(conn, member, seed.a.id) as c:
        c.execute(stmt, [member, gid])
        assert scalar(
            c,
            "select approved_at is not null from public.generated_emails where id = %s",
            [gid],
        )
    with (
        as_user(conn, member, seed.a.id) as c,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        c.execute(
            "update public.generated_emails set approved_at = now() where id = %s",
            [gid],
        )


def test_users_with_history_can_be_deleted(
    conn: psycopg.Connection, seed: Seed
) -> None:
    member, owner, admin = (
        seed.a.users["member"],
        seed.a.users["owner"],
        seed.a.users["admin"],
    )
    conn.execute("begin")
    try:
        conn.execute(
            "insert into public.audit_log (tenant_id, actor_kind, actor_user_id, action) values (%s, 'user', %s, 'x')",
            [seed.a.id, member],
        )
        conn.execute(
            "insert into public.prompt_templates (tenant_id, kind, name, version, system_prompt, user_template, model,"
            " created_by) values (%s, 'email_gen', 'mine', 1, 'You must NOT link.', '', 'gpt-4o', %s)",
            [seed.a.id, member],
        )
        conn.execute("delete from auth.users where id = %s", [member])
        # An owner can leave once another active owner exists.
        conn.execute(
            "update public.memberships set role = 'owner' where user_id = %s", [admin]
        )
        conn.execute("delete from auth.users where id = %s", [owner])
    finally:
        conn.execute("rollback")


def test_one_generated_email_per_lead(conn: psycopg.Connection, seed: Seed) -> None:
    with as_worker(conn, seed.a.id) as c, pytest.raises(errors.UniqueViolation):
        c.execute(
            "insert into public.generated_emails (lead_id, campaign_id, prompt_template_id, prompt_version, model)"
            " values (%s, %s, %s, 1, 'gpt-4o')",
            [
                seed.a.rows["leads"],
                seed.a.rows["campaigns"],
                seed.a.rows["prompt_templates"],
            ],
        )


def test_deleting_a_lead_keeps_its_pending_duplicate(
    conn: psycopg.Connection, seed: Seed
) -> None:
    conn.execute("begin")
    try:
        conn.execute("delete from public.leads where id = %s", [seed.a.rows["leads"]])
        existing = scalar(
            conn,
            "select existing_lead_id from public.lead_duplicates where id = %s",
            [seed.a.rows["lead_duplicates"]],
        )
        assert existing is None
    finally:
        conn.execute("rollback")


# ---------------------------------------------------------------------------
# Sequence steps and link check (202611200006)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("column", ["personalization_3", "subject_2", "subject_5"])
@pytest.mark.parametrize(
    "text", ["Book here: calendly.com/sam/30min", "Specs at www.acme.com"]
)
def test_follow_up_steps_reject_links(
    conn: psycopg.Connection, seed: Seed, column: str, text: str
) -> None:
    with (
        as_user(conn, seed.a.users["member"], seed.a.id) as c,
        pytest.raises(errors.CheckViolation),
    ):
        c.execute(
            sql.SQL("update public.generated_emails set {} = %s where id = %s").format(
                sql.Identifier(column)
            ),
            [text, seed.a.rows["generated_emails"]],
        )


def test_follow_up_steps_allow_plain_text(conn: psycopg.Connection, seed: Seed) -> None:
    with as_user(conn, seed.a.users["member"], seed.a.id) as c:
        updated = c.execute(
            "update public.generated_emails set subject_2 = 'Quick follow-up', personalization_2 = %s where id = %s",
            [
                "Happy to share the 3/4 rollout plan. It cut churn 12.5%.",
                seed.a.rows["generated_emails"],
            ],
        ).rowcount
    assert updated == 1


def test_reply_rejects_bare_domain_link_when_not_interested(
    conn: psycopg.Connection, seed: Seed
) -> None:
    rid = seed.a.rows["replies"]
    pid = seed.a.rows["prompt_templates"]
    setup = [
        (
            f"update public.replies set prompt_template_id = '{pid}', prompt_version = 1, intent = 'not_interested'"
            f" where id = '{rid}'"
        )
    ]
    with (
        as_user(conn, seed.a.users["member"], seed.a.id, setup=setup) as c,
        pytest.raises(errors.CheckViolation),
    ):
        c.execute(
            "update public.replies set draft_body = 'Book: calendly.com/sam' where id = %s",
            [rid],
        )


@pytest.mark.parametrize(("count", "allowed"), [(0, False), (3, True), (6, False)])
def test_campaign_sequence_count_is_1_to_5(
    conn: psycopg.Connection, seed: Seed, count: int, allowed: bool
) -> None:
    stmt = "insert into public.campaigns (name, channel, sequence_count) values ('Seq', 'email', %s)"
    with as_user(conn, seed.a.users["member"], seed.a.id) as c:
        if allowed:
            c.execute(stmt, [count])
        else:
            with pytest.raises(errors.CheckViolation):
                c.execute(stmt, [count])

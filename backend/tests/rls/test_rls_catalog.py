"""Catalog guards: fail the build if any table, policy or function drifts from the tenancy rules."""

from __future__ import annotations

import psycopg

from .conftest import Seed

# Documented exemptions from the tenant_id column (see 202611200002_tables.sql header).
NO_TENANT_ID = {"tenants", "users"}


def _public_tables(conn: psycopg.Connection) -> list[str]:
    return [r[0] for r in conn.execute("select tablename from pg_tables where schemaname = 'public' order by 1")]


def test_every_table_has_rls_enabled_and_forced(conn: psycopg.Connection, seed: Seed) -> None:
    rows = conn.execute(
        """
        select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relkind = 'r'
          and not (c.relrowsecurity and c.relforcerowsecurity)
        """
    ).fetchall()
    assert rows == [], f"tables without ENABLE + FORCE ROW LEVEL SECURITY: {rows}"


def test_every_table_has_policies(conn: psycopg.Connection, seed: Seed) -> None:
    missing = conn.execute(
        """
        select t.tablename from pg_tables t
        where t.schemaname = 'public'
          and not exists (select 1 from pg_policies p where p.schemaname = 'public' and p.tablename = t.tablename)
        """
    ).fetchall()
    assert missing == [], f"tables without any policy: {missing}"


def test_every_tenant_table_has_not_null_tenant_id_defaulting_to_context(conn: psycopg.Connection, seed: Seed) -> None:
    for table in _public_tables(conn):
        if table in NO_TENANT_ID:
            continue
        row = conn.execute(
            """
            select is_nullable, column_default from information_schema.columns
            where table_schema = 'public' and table_name = %s and column_name = 'tenant_id'
            """,
            [table],
        ).fetchone()
        assert row is not None, f"{table} has no tenant_id column"
        assert row[0] == "NO", f"{table}.tenant_id is nullable"
        assert row[1] == "app.tenant_id()", f"{table}.tenant_id default is {row[1]!r}, expected app.tenant_id()"


def test_no_role_can_write_tenant_id(conn: psycopg.Connection, seed: Seed) -> None:
    rows = conn.execute(
        """
        select table_name, grantee, privilege_type from information_schema.column_privileges
        where table_schema = 'public' and column_name = 'tenant_id'
          and privilege_type in ('INSERT', 'UPDATE')
          and grantee in ('authenticated', 'gazi_worker', 'anon', 'service_role')
        """
    ).fetchall()
    assert rows == [], f"tenant_id must only come from app.tenant_id(); writable by: {rows}"


def test_anon_and_service_role_have_no_table_privileges(conn: psycopg.Connection, seed: Seed) -> None:
    rows = conn.execute(
        """
        select table_name, grantee, privilege_type from information_schema.role_table_grants
        where table_schema = 'public' and grantee in ('anon', 'service_role', 'PUBLIC')
        union
        select table_name, grantee, privilege_type from information_schema.column_privileges
        where table_schema = 'public' and grantee in ('anon', 'service_role', 'PUBLIC')
        """
    ).fetchall()
    assert rows == [], f"unexpected grants: {rows}"


def test_no_policy_applies_to_public(conn: psycopg.Connection, seed: Seed) -> None:
    rows = conn.execute(
        "select tablename, policyname from pg_policies where schemaname = 'public' and 'public' = any(roles)"
    ).fetchall()
    assert rows == [], f"policies must name a role explicitly: {rows}"


def test_security_definer_functions_are_locked_down(conn: psycopg.Connection, seed: Seed) -> None:
    rows = conn.execute(
        """
        select p.proname, pg_get_userbyid(p.proowner), p.proconfig,
               has_function_privilege('anon', p.oid, 'execute')
        from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname in ('app', 'public') and p.prosecdef
        """
    ).fetchall()
    assert rows, "expected SECURITY DEFINER functions"
    for name, owner, config, anon_exec in rows:
        assert owner == "gazi_definer", f"{name} is owned by {owner}"
        assert config and 'search_path=""' in config, f"{name} must pin search_path=''"
        assert not anon_exec, f"anon can execute {name}"

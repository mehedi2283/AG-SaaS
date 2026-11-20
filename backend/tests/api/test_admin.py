"""Superuser routes against the real database. A superuser is made the only way there is: SQL as the owner."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import psycopg
import pytest

from tests.api.conftest import Harness, auth, unique_slug
from tests.rls.conftest import Seed

pytestmark = pytest.mark.anyio


@pytest.fixture
def superuser_id(superuser: psycopg.Connection, seed: Seed) -> Iterator[UUID]:
    user = seed.outsider
    superuser.execute("insert into public.platform_admins (user_id) values (%s) on conflict do nothing", [user])
    yield user
    superuser.execute("delete from public.platform_admins where user_id = %s", [user])


async def workspace(h: Harness, seed: Seed) -> UUID:
    r = await h.client.post(
        "/v1/tenants", json={"name": "Echo Ltd", "slug": unique_slug()}, headers=auth(seed.outsider)
    )
    assert r.status_code == 201, r.text
    return UUID(r.json()["tenant_id"])


async def test_non_superusers_get_403_everywhere(api: Harness, seed: Seed) -> None:
    owner = auth(seed.a.users["owner"], seed.a.id, "owner")
    for method, path in [
        ("GET", "/v1/admin/tenants"),
        ("GET", "/v1/admin/users"),
        ("GET", "/v1/admin/activity"),
        ("POST", f"/v1/admin/tenants/{seed.b.id}/status"),
    ]:
        r = await api.client.request(method, path, headers=owner, json={"status": "suspended"})
        assert r.status_code == 403 and r.json()["error_code"] == "not_platform_admin", path
    me = (await api.client.get("/v1/me", headers=owner)).json()
    assert me["is_platform_admin"] is False


async def test_superuser_sees_every_workspace_and_user(api: Harness, seed: Seed, superuser_id: UUID) -> None:
    headers = auth(superuser_id)
    assert (await api.client.get("/v1/me", headers=headers)).json()["is_platform_admin"] is True
    tenants = {t["id"]: t for t in (await api.client.get("/v1/admin/tenants", headers=headers)).json()}
    assert {str(seed.a.id), str(seed.b.id)} <= set(tenants)
    a = tenants[str(seed.a.id)]
    assert a["members"] == 4 and a["owner_email"] == "owner@alpha.example.com"
    users = (await api.client.get("/v1/admin/users", headers=headers)).json()
    assert any(u["email"] == "viewer@bravo.example.com" and u["login_enabled"] for u in users)
    members = (await api.client.get(f"/v1/admin/tenants/{seed.b.id}/members", headers=headers)).json()
    assert [m["role"] for m in members] == ["owner", "admin", "member", "viewer"]


async def test_suspend_and_reactivate(api: Harness, seed: Seed, superuser_id: UUID) -> None:
    tenant_id = await workspace(api, seed)
    headers = auth(superuser_id)
    r = await api.client.post(f"/v1/admin/tenants/{tenant_id}/status", json={"status": "suspended"}, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    # The workspace's own owner is locked out at once.
    r = await api.client.get("/v1/integrations", headers=auth(seed.outsider, tenant_id, "owner"))
    assert r.json() == []
    r = await api.client.post(f"/v1/admin/tenants/{tenant_id}/status", json={"status": "active"}, headers=headers)
    assert r.json()["status"] == "active"
    activity = (await api.client.get("/v1/admin/activity?limit=20", headers=headers)).json()
    changes = [
        a["diff"] for a in activity if a["action"] == "workspace.status_changed" and a["tenant_id"] == str(tenant_id)
    ]
    assert changes == [{"from": "suspended", "to": "active"}, {"from": "active", "to": "suspended"}]


async def test_free_access(api: Harness, seed: Seed, superuser_id: UUID) -> None:
    tenant_id = await workspace(api, seed)
    headers = auth(superuser_id)
    r = await api.client.post(
        f"/v1/admin/tenants/{tenant_id}/free-access", json={"until": "2027-01-31T00:00:00Z"}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["comp_until"].startswith("2027-01-31") and r.json()["access"] == "active"
    r = await api.client.post(
        f"/v1/admin/tenants/{tenant_id}/free-access", json={"until": "2020-01-01T00:00:00Z"}, headers=headers
    )
    assert r.status_code == 422 and r.json()["error_code"] == "invalid_value"
    r = await api.client.post(f"/v1/admin/tenants/{tenant_id}/free-access", json={"until": None}, headers=headers)
    assert r.json()["comp_until"] is None


async def test_member_management_keeps_an_owner(api: Harness, seed: Seed, superuser_id: UUID) -> None:
    tenant_id = await workspace(api, seed)
    headers = auth(superuser_id)
    r = await api.client.delete(f"/v1/admin/tenants/{tenant_id}/members/{seed.outsider}", headers=headers)
    assert r.status_code == 422 and r.json()["error_code"] == "constraint_violation"  # its only owner
    r = await api.client.patch(
        f"/v1/admin/tenants/{tenant_id}/members/{seed.a.users['viewer']}", json={"role": "admin"}, headers=headers
    )
    assert r.status_code == 404  # not a member of that workspace


async def test_disable_and_enable_a_login(api: Harness, seed: Seed, superuser_id: UUID) -> None:
    headers = auth(superuser_id)
    target = seed.b.users["viewer"]
    r = await api.client.post(f"/v1/admin/users/{target}/login", json={"enabled": False}, headers=headers)
    assert r.status_code == 200 and r.json()["login_enabled"] is False
    r = await api.client.post(f"/v1/admin/users/{target}/login", json={"enabled": True}, headers=headers)
    assert r.json()["login_enabled"] is True
    assert api.auth_admin.logins == [(target, False), (target, True)]
    r = await api.client.post(f"/v1/admin/users/{superuser_id}/login", json={"enabled": False}, headers=headers)
    assert r.status_code == 409 and r.json()["error_code"] == "cannot_disable_superuser"

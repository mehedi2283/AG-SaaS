"""Campaigns, senders, CSV import (upload + the real import job) and duplicate review, against the real database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import psycopg
import pytest

from app.core.config import Settings
from app.core.crypto import Crypto, Sealed
from app.db.pool import create_pool
from app.domain.leads.importer import import_leads
from app.domain.senders.service import signature_aad
from app.main import create_app
from tests.api.conftest import FakeAuthAdmin, FakeQueue, auth, unique_slug
from tests.rls.conftest import Seed

pytestmark = pytest.mark.anyio

APOLLO_CSV = (
    b"First Name,Last Name,Title,Company,Email,Person Linkedin Url,# Employees,Seniority\n"
    b"Ana,Silva,Head of Buying,Moda SA,ana@moda.pt,https://linkedin.com/in/ana,120,Head\n"
    b"Ben,Ode,Buyer,Retail Co,ben@retail.co,,45,Senior\n"
    b"Bad,Row,,,not-an-email,,,\n"
    b"Ana,Silva,Head of Buying,Moda SA,ANA@moda.pt,,,\n"
)


@dataclass
class LeadsHarness:
    client: httpx.AsyncClient
    queue: FakeQueue
    pool: asyncpg.Pool
    crypto: Crypto
    imports_dir: Path


@pytest.fixture
async def lapi(settings: Settings, crypto: Crypto, tmp_path: Path) -> AsyncIterator[LeadsHarness]:
    local = settings.model_copy(update={"imports_dir": str(tmp_path)})
    pool = await create_pool(local.database_url.get_secret_value(), max_size=4)
    queue = FakeQueue()
    app = create_app(local, pool=pool, queue=queue, auth_admin=FakeAuthAdmin(), crypto=crypto)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield LeadsHarness(client, queue, pool, crypto, tmp_path)
    await pool.close()


async def workspace(h: LeadsHarness, seed: Seed) -> tuple[UUID, dict[str, str]]:
    r = await h.client.post("/v1/tenants", json={"name": "Foxtrot", "slug": unique_slug()}, headers=auth(seed.outsider))
    assert r.status_code == 201, r.text
    tenant_id = UUID(r.json()["tenant_id"])
    return tenant_id, auth(seed.outsider, tenant_id, "owner")


async def new_campaign(h: LeadsHarness, headers: dict[str, str], **extra: Any) -> dict[str, Any]:
    r = await h.client.post(
        "/v1/campaigns", json={"name": "Spring buyers", "channel": "email", **extra}, headers=headers
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def run_import(h: LeadsHarness, tenant_id: UUID) -> dict[str, Any]:
    job = next(j for j in reversed(h.queue.jobs) if j["function"] == "import_leads")
    result: dict[str, Any] = await import_leads({"pool": h.pool, "job_id": job["job_id"]}, str(tenant_id), *job["args"])
    return result


# ---------------------------------------------------------------------------
# Senders and campaigns
# ---------------------------------------------------------------------------


async def test_sender_signature_is_encrypted(lapi: LeadsHarness, seed: Seed, superuser: psycopg.Connection) -> None:
    tenant_id, headers = await workspace(lapi, seed)
    r = await lapi.client.post(
        "/v1/sender-identities",
        json={
            "display_name": "Rahim Uddin",
            "title": "Export manager",
            "signature": "Rahim Uddin\nExport manager, Gazi Textiles",
            "signature_image_url": "https://cdn.example.com/sig.png",
            "instantly_eaccount": "Rahim@Gazi.example",
            "is_default": True,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["has_signature"] is True and "signature" not in body
    assert body["instantly_eaccount"] == "rahim@gazi.example"
    row = superuser.execute(
        "select s.signature_ciphertext, s.signature_nonce, t.wrapped_dek from public.sender_identities s"
        " join public.tenants t on t.id = s.tenant_id where s.id = %s",
        [body["id"]],
    ).fetchone()
    assert row is not None and b"Rahim" not in row[0]
    plain = lapi.crypto.decrypt(row[2], Sealed(row[0], row[1]), signature_aad(tenant_id, UUID(body["id"])))
    assert plain.decode().startswith("Rahim Uddin")


async def test_campaign_gets_default_sequence_and_sender(lapi: LeadsHarness, seed: Seed) -> None:
    _, headers = await workspace(lapi, seed)
    sender = (
        await lapi.client.post(
            "/v1/sender-identities", json={"display_name": "Sam", "is_default": True}, headers=headers
        )
    ).json()
    campaign = await new_campaign(lapi, headers, sequence_count=3, timezone="Asia/Dhaka")
    assert campaign["sender_identity_id"] == sender["id"] and campaign["sequence_count"] == 3
    steps = (await lapi.client.get(f"/v1/campaigns/{campaign['id']}/steps", headers=headers)).json()
    assert [s["delay_days"] for s in steps] == [0, 3, 4]
    assert steps[-1]["angle"].startswith("Breakup")

    r = await lapi.client.put(
        f"/v1/campaigns/{campaign['id']}/steps",
        json={"steps": [{"step": 1, "delay_days": 0, "angle": "Hook"}, {"step": 2, "delay_days": 0, "angle": "x"}]},
        headers=headers,
    )
    assert r.status_code == 422  # later steps need a delay

    r = await lapi.client.post(
        "/v1/campaigns", json={"name": "x", "channel": "email", "timezone": "Mars/Base"}, headers=headers
    )
    assert r.status_code == 422


async def test_viewers_cannot_create_campaigns(lapi: LeadsHarness, seed: Seed) -> None:
    r = await lapi.client.post(
        "/v1/campaigns",
        json={"name": "x", "channel": "email"},
        headers=auth(seed.a.users["viewer"], seed.a.id, "viewer"),
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------


async def test_csv_import_end_to_end(lapi: LeadsHarness, seed: Seed, superuser: psycopg.Connection) -> None:
    tenant_id, headers = await workspace(lapi, seed)
    campaign = await new_campaign(lapi, headers)
    r = await lapi.client.post(
        f"/v1/lead-imports?campaign_id={campaign['id']}&filename=apollo-export.csv",
        content=APOLLO_CSV,
        headers={**headers, "content-type": "text/csv"},
    )
    assert r.status_code == 202, r.text
    imp = r.json()
    assert imp["status"] == "uploaded" and imp["original_filename"] == "apollo-export.csv"
    assert lapi.queue.jobs[-1]["job_id"] == f"import:{imp['id']}" and lapi.queue.jobs[-1]["queue"] == "gazi:imports"
    stored = list(lapi.imports_dir.joinpath(str(tenant_id)).iterdir())
    assert len(stored) == 1 and stored[0].read_bytes() == APOLLO_CSV

    result = await run_import(lapi, tenant_id)
    assert result == {"status": "completed_with_errors", "total": 4, "imported": 2, "duplicates": 1, "failed": 1}
    imp = (await lapi.client.get(f"/v1/lead-imports/{imp['id']}", headers=headers)).json()
    assert imp["status"] == "completed_with_errors" and imp["pending_duplicates"] == 1
    assert imp["errors"] == [{"row": 4, "error": "missing or invalid email"}]

    page = (await lapi.client.get(f"/v1/leads?campaign_id={campaign['id']}", headers=headers)).json()
    assert page["total"] == 2
    ana = next(lead for lead in page["items"] if lead["email"] == "ana@moda.pt")
    assert ana["first_name"] == "Ana" and ana["company_size"] == 120 and ana["linkedin_url"].endswith("/ana")

    # Running the job again (reconciler, retry) changes nothing.
    assert (await run_import(lapi, tenant_id)) == {"skipped": "completed_with_errors"}

    # A second campaign gets the same people: they're duplicates for review, not new leads.
    second = await new_campaign(lapi, headers, name="Autumn")
    await lapi.client.post(
        f"/v1/lead-imports?campaign_id={second['id']}",
        content=APOLLO_CSV,
        headers={**headers, "content-type": "text/csv"},
    )
    result = await run_import(lapi, tenant_id)
    assert result["imported"] == 0 and result["duplicates"] == 3
    second_import = (await lapi.client.get(f"/v1/lead-imports?campaign_id={second['id']}", headers=headers)).json()[0]
    dups = (await lapi.client.get(f"/v1/lead-imports/{second_import['id']}/duplicates", headers=headers)).json()
    assert {d["reason"] for d in dups} == {"exists_in_tenant", "repeated_in_file"}
    assert any(d["existing_campaign"] == "Spring buyers" for d in dups)

    r = await lapi.client.post(
        f"/v1/lead-imports/{second_import['id']}/duplicates/resolve",
        json={"resolution": "add_anyway", "all_pending": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    # Ana is in the file twice: the second copy can't be added to the same campaign again.
    assert r.json() == {"resolved": 3, "added": 2, "updated": 0, "skipped": 0, "not_possible": 1}
    page = (await lapi.client.get(f"/v1/leads?campaign_id={second['id']}", headers=headers)).json()
    assert page["total"] == 2  # one email appears once per campaign


async def test_upload_rejects_files_without_emails(lapi: LeadsHarness, seed: Seed) -> None:
    _, headers = await workspace(lapi, seed)
    campaign = await new_campaign(lapi, headers)
    r = await lapi.client.post(
        f"/v1/lead-imports?campaign_id={campaign['id']}",
        content=b"Name,Company\nAna,Moda\n",
        headers={**headers, "content-type": "text/csv"},
    )
    assert r.status_code == 422 and r.json()["error_code"] == "no_email_column"
    assert lapi.queue.jobs == [] and not any(lapi.imports_dir.rglob("*.csv"))


async def test_upload_size_limit(lapi: LeadsHarness, seed: Seed, settings: Settings) -> None:
    _, headers = await workspace(lapi, seed)
    campaign = await new_campaign(lapi, headers)
    big = b"Email\n" + b"x@y.co\n" * (settings.max_import_bytes // 7 + 10)
    r = await lapi.client.post(
        f"/v1/lead-imports?campaign_id={campaign['id']}", content=big, headers={**headers, "content-type": "text/csv"}
    )
    assert r.status_code == 413 and not any(lapi.imports_dir.rglob("*.csv"))


async def test_other_tenants_campaign_is_not_found(lapi: LeadsHarness, seed: Seed) -> None:
    _, headers = await workspace(lapi, seed)
    r = await lapi.client.post(
        f"/v1/lead-imports?campaign_id={seed.a.rows['campaigns']}",
        content=APOLLO_CSV,
        headers={**headers, "content-type": "text/csv"},
    )
    assert r.status_code == 404

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, Response, status

from app.api.deps import get_billing, tenant_db
from app.core.auth import TenantContext, current_tenant, require_role
from app.core.config import Settings, app_settings
from app.domain.billing import service
from app.domain.billing.schemas import BillingOut, PackChange, RedirectOut
from app.providers.stripe import BillingProvider

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("")
async def read_billing(
    _: TenantContext = Depends(current_tenant),
    conn: asyncpg.Connection = Depends(tenant_db),
    settings: Settings = Depends(app_settings),
    billing: BillingProvider | None = Depends(get_billing),
) -> BillingOut:
    return await service.get_billing(conn, settings, billing)


@router.post("/checkout")
async def start_checkout(
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
    settings: Settings = Depends(app_settings),
    billing: BillingProvider | None = Depends(get_billing),
) -> RedirectOut:
    """A Stripe Checkout page for the workspace's plan (with the free trial, the first time)."""
    return await service.start_checkout(conn, ctx, settings, billing)


@router.post("/portal")
async def open_portal(
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
    settings: Settings = Depends(app_settings),
    billing: BillingProvider | None = Depends(get_billing),
) -> RedirectOut:
    """The Stripe customer portal: card, invoices, cancel."""
    return await service.open_portal(conn, ctx, settings, billing)


@router.post("/token-packs", status_code=status.HTTP_202_ACCEPTED)
async def add_token_pack(
    body: PackChange,
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
    settings: Settings = Depends(app_settings),
    billing: BillingProvider | None = Depends(get_billing),
) -> Response:
    """Add one monthly token pack (charged now, prorated). The new allowance shows once Stripe confirms."""
    await service.change_pack(conn, ctx, settings, billing, body.price_id, add=True)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.delete("/token-packs/{price_id}", status_code=status.HTTP_202_ACCEPTED)
async def remove_token_pack(
    price_id: str,
    ctx: TenantContext = Depends(require_role("admin")),
    conn: asyncpg.Connection = Depends(tenant_db),
    settings: Settings = Depends(app_settings),
    billing: BillingProvider | None = Depends(get_billing),
) -> Response:
    """Remove one token pack (prorated credit)."""
    await service.change_pack(conn, ctx, settings, billing, price_id, add=False)
    return Response(status_code=status.HTTP_202_ACCEPTED)

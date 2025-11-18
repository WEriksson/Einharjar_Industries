from decimal import Decimal

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .db import get_db
from .models import InventoryLot, Item
from .settings_service import get_or_create_settings

router = APIRouter()


@router.get("/inventory", response_class=HTMLResponse)
async def inventory_view(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Inventory overview, aggregated by item:
    - One row per item
    - Quantity = sum of quantity_remaining across all lots
    - Cost = sum(unit_cost * quantity_remaining) across all lots
    - Summary totals at the top
    """

    stmt = (
        select(InventoryLot)
        .options(selectinload(InventoryLot.item))
        .where(InventoryLot.quantity_remaining > 0)
    )
    res = await db.execute(stmt)
    lots = res.scalars().all()

    aggregated = {}
    total_units = 0
    total_cost = Decimal("0")
    total_volume = 0.0
    total_shipping = 0.0

    for lot in lots:
        qty = int(lot.quantity_remaining or 0)
        if qty <= 0:
            continue
        if not lot.item:
            continue

        item = lot.item
        unit_total_cost = Decimal(str(lot.unit_total_cost))

        # Global totals
        total_units += qty
        total_cost += unit_total_cost * qty

        shipping_per_unit = getattr(lot, "shipping_cost_per_unit", None)
        if shipping_per_unit is not None:
            total_shipping += qty * float(shipping_per_unit)

        if item.volume_m3:
            total_volume += qty * item.volume_m3

        # Aggregation per item
        entry = aggregated.get(item.id)
        if not entry:
            entry = {
                "item": item,
                "quantity": 0,
                "total_cost": Decimal("0"),
            }
            aggregated[item.id] = entry

        entry["quantity"] += qty
        entry["total_cost"] += unit_total_cost * qty

    items = []
    for entry in aggregated.values():
        qty = entry["quantity"]
        total_cost_entry = entry["total_cost"]
        avg_cost = total_cost_entry / qty if qty else Decimal("0")
        items.append(
            {
                "item": entry["item"],
                "quantity": qty,
                "total_cost": total_cost_entry,
                "avg_cost": avg_cost,
            }
        )

    items.sort(key=lambda e: (e["item"].name or "").lower())
    summary = {
        "total_items": len(items),
        "total_units": total_units,
        "total_cost": total_cost,
        "total_volume": total_volume,
        "total_shipping": total_shipping,
    }

    settings = await get_or_create_settings(db)

    return request.app.state.templates.TemplateResponse(
        "inventory.html",
        {
            "request": request,
            "items": items,
            "summary": summary,
            "settings": settings,
            "current_page": "inventory",
        },
    )


@router.get("/inventory/item/{item_id}", response_class=HTMLResponse)
async def inventory_item_detail(
    item_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Detailed per-lot breakdown for a specific item."""

    stmt = (
        select(InventoryLot)
        .options(
            selectinload(InventoryLot.item),
            selectinload(InventoryLot.batch),
        )
        .where(InventoryLot.item_id == item_id)
        .order_by(InventoryLot.acquired_at.desc(), InventoryLot.id.desc())
    )
    res = await db.execute(stmt)
    lots = res.scalars().all()

    item = lots[0].item if lots else None
    if item is None:
        item_res = await db.execute(select(Item).where(Item.id == item_id))
        item = item_res.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    lot_rows = []
    total_qty = 0
    total_cost = Decimal("0")

    for lot in lots:
        qty_remaining = int(lot.quantity_remaining or 0)
        qty_total = int(lot.quantity_total or 0)
        unit_total_cost = Decimal(str(lot.unit_total_cost))
        total_cost_for_lot = unit_total_cost * qty_remaining

        total_qty += qty_remaining
        total_cost += total_cost_for_lot

        lot_rows.append(
            {
                "lot": lot,
                "qty_total": qty_total,
                "qty_remaining": qty_remaining,
                "unit_cost": unit_total_cost,
                "total_cost": total_cost_for_lot,
            }
        )

    summary = {
        "lot_count": len(lot_rows),
        "total_qty": total_qty,
        "total_cost": total_cost,
    }

    settings = await get_or_create_settings(db)

    return request.app.state.templates.TemplateResponse(
        "inventory_item_detail.html",
        {
            "request": request,
            "item": item,
            "lots": lot_rows,
            "summary": summary,
            "settings": settings,
            "current_page": "inventory",
        },
    )

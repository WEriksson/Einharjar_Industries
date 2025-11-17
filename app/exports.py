from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, asc

from .db import get_db
from .models import Item, InventoryLot, InventoryEvent
from .utils_parsing import parse_transactions, parse_janice_rows


router = APIRouter()


@router.get("/export", response_class=HTMLResponse)
async def export_form(request: Request):
    return request.app.state.templates.TemplateResponse(
        "export.html",
        {
            "request": request,
            "results": None,
            "current_page": "export",
        },
    )


@router.post("/export", response_class=HTMLResponse)
async def handle_export(
    request: Request,
    transactions: str = Form(...),
    event_type: str = Form("sale"),  # 'sale' or 'industry'
    note: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    txs = parse_transactions(transactions)
    results: list[dict] = []

    for tx in txs:
        item_name = tx["item_name"].strip()
        qty = tx["qty"]
        total_value = tx["total_cost"]  # for sales: total ISK gained
        if qty <= 0:
            results.append(
                {
                    "item_name": item_name,
                    "qty": qty,
                    "status": "Skipped (non-positive quantity)",
                    "ok": False,
                    "unit_price": None,
                }
            )
            continue

        unit_price = total_value / qty if total_value else None

        # Find item
        stmt_item = select(Item).where(Item.name == item_name)
        res_item = await db.execute(stmt_item)
        item = res_item.scalar_one_or_none()
        if not item:
            results.append(
                {
                    "item_name": item_name,
                    "qty": qty,
                    "status": "Item not found in inventory",
                    "ok": False,
                    "unit_price": unit_price,
                }
            )
            continue

        # Fetch lots for this item in FIFO order
        stmt_lots = (
            select(InventoryLot)
            .where(
                InventoryLot.item_id == item.id,
                InventoryLot.quantity_remaining > 0,
            )
            .order_by(asc(InventoryLot.acquired_at), asc(InventoryLot.id))
        )
        res_lots = await db.execute(stmt_lots)
        lots = list(res_lots.scalars())

        total_available = sum(l.quantity_remaining for l in lots)
        if total_available < qty:
            results.append(
                {
                    "item_name": item_name,
                    "qty": qty,
                    "status": f"Not enough in inventory (have {total_available})",
                    "ok": False,
                    "unit_price": unit_price,
                }
            )
            continue

        # Use the EVE time from the wallet line (tx["time"]), convert to naive UTC
        tx_time = tx.get("time")
        if tx_time is None:
            from datetime import datetime
            tx_time = datetime.now(timezone.utc)
        eve_time = tx_time.astimezone(timezone.utc).replace(tzinfo=None)

        remaining = qty
        for lot in lots:
            if remaining <= 0:
                break
            take = min(lot.quantity_remaining, remaining)
            if take <= 0:
                continue

            lot.quantity_remaining -= take
            remaining -= take

            event = InventoryEvent(
                event_type=event_type,
                eve_time=eve_time,
                item_id=item.id,
                lot_id=lot.id,
                quantity=take,
                unit_price=unit_price,
                note=note,
            )
            db.add(event)

        await db.commit()

        results.append(
            {
                "item_name": item_name,
                "qty": qty,
                "status": "OK",
                "ok": True,
                "unit_price": unit_price,
            }
        )

    return request.app.state.templates.TemplateResponse(
        "export.html",
        {
            "request": request,
            "results": results if results else None,
            "current_page": "export",
        },
    )

@router.post("/export/janice", response_class=HTMLResponse)
async def handle_export_janice(
    request: Request,
    janice_data: str = Form(...),
    price_source: str = Form("sell"),  # 'buy' or 'sell' for sale price
    event_type: str = Form("sale"),    # 'sale' or 'industry'
    note: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    rows = parse_janice_rows(janice_data)
    results: list[dict] = []

    for r in rows:
        item_name = r["item_name"].strip()
        qty = r["qty"]
        if qty <= 0:
            results.append(
                {
                    "item_name": item_name,
                    "qty": qty,
                    "status": "Skipped (non-positive quantity)",
                    "ok": False,
                    "unit_price": None,
                }
            )
            continue

        if price_source == "buy":
            unit_price = r["jita_buy"]
        else:
            unit_price = r["jita_sell"]

        total_available = 0

        # Find item
        stmt_item = select(Item).where(Item.name == item_name)
        res_item = await db.execute(stmt_item)
        item = res_item.scalar_one_or_none()
        if not item:
            results.append(
                {
                    "item_name": item_name,
                    "qty": qty,
                    "status": "Item not found in inventory",
                    "ok": False,
                    "unit_price": unit_price,
                }
            )
            continue

        # Get lots for this item
        stmt_lots = (
            select(InventoryLot)
            .where(
                InventoryLot.item_id == item.id,
                InventoryLot.quantity_remaining > 0,
            )
            .order_by(asc(InventoryLot.acquired_at), asc(InventoryLot.id))
        )
        res_lots = await db.execute(stmt_lots)
        lots = list(res_lots.scalars())

        total_available = sum(l.quantity_remaining for l in lots)
        if total_available < qty:
            results.append(
                {
                    "item_name": item_name,
                    "qty": qty,
                    "status": f"Not enough in inventory (have {total_available})",
                    "ok": False,
                    "unit_price": unit_price,
                }
            )
            continue

        # No timestamp from Janice, so use "now" in EVE/UTC
        eve_time = datetime.now(timezone.utc).replace(tzinfo=None)

        remaining = qty
        for lot in lots:
            if remaining <= 0:
                break
            take = min(lot.quantity_remaining, remaining)
            if take <= 0:
                continue

            lot.quantity_remaining -= take
            remaining -= take

            event = InventoryEvent(
                event_type=event_type,
                eve_time=eve_time,
                item_id=item.id,
                lot_id=lot.id,
                quantity=take,
                unit_price=unit_price,
                note=note or f"Janice {price_source}",
            )
            db.add(event)

        await db.commit()

        results.append(
            {
                "item_name": item_name,
                "qty": qty,
                "status": "OK",
                "ok": True,
                "unit_price": unit_price,
            }
        )

    return request.app.state.templates.TemplateResponse(
        "export.html",
        {
            "request": request,
            "results": results if results else None,
            "current_page": "export",
        },
    )

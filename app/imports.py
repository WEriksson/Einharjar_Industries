from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .db import get_db, Base, engine
from .models import Item, InventoryLot, ImportBatch, InventoryEvent
from .utils_parsing import parse_transactions, aggregate_by_item, parse_janice_rows


router = APIRouter()


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@router.get("/", response_class=HTMLResponse)
async def import_form(request: Request):
    return request.app.state.templates.TemplateResponse(
        "import.html",
        {
            "request": request,
            "items": None,
            "saved": False,
            "current_page": "imports",
        },
    )



@router.post("/", response_class=HTMLResponse)
async def handle_import(
    request: Request,
    transactions: str = Form(...),
    save_to_inventory: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    txs = parse_transactions(transactions)
    agg = aggregate_by_item(txs)

    display_items = []

    # --- Build display summary (aggregated per item) ---
    for item_name, data in agg.items():
        qty = data["qty"]
        total_cost = data["total_cost"]
        if qty <= 0:
            continue

        unit_cost = total_cost / qty

        display_items.append(
            {
                "item_name": item_name,
                "qty": qty,
                "unit_cost": unit_cost,
                "total_cost": total_cost,
            }
        )

    # --- Save lots + events (one per transaction line) ---
    if save_to_inventory and txs:
        batch = ImportBatch(created_at=datetime.utcnow(), note="Manual import")
        db.add(batch)
        await db.flush()

        for tx in txs:
            item_name = tx["item_name"]
            qty = tx["qty"]
            total_cost = tx["total_cost"]
            if qty <= 0:
                continue

            unit_cost = total_cost / qty

            # Get or create Item by name
            stmt = select(Item).where(Item.name == item_name)
            res = await db.execute(stmt)
            item = res.scalar_one_or_none()
            if not item:
                item = Item(name=item_name, volume_m3=None)
                db.add(item)
                await db.flush()

                # EVE time from wallet line; fall back to now if missing
                tx_time = tx.get("time")
                if tx_time is None:
                    tx_time = datetime.now(timezone.utc)
                dt_eve = tx_time.astimezone(timezone.utc).replace(tzinfo=None)


            lot = InventoryLot(
                item_id=item.id,
                quantity_total=qty,
                quantity_remaining=qty,
                unit_cost=unit_cost,
                acquired_at=dt_eve,   # internal; not shown in UI
                source="Manual import",
                batch_id=batch.id,
            )
            db.add(lot)
            await db.flush()  # get lot.id for event

            event = InventoryEvent(
                event_type="import",
                eve_time=dt_eve,
                item_id=item.id,
                lot_id=lot.id,
                quantity=qty,
                unit_price=unit_cost,
                note="Manual import",
            )
            db.add(event)

        await db.commit()

        return request.app.state.templates.TemplateResponse(
            "import.html",
            {
               "request": request,
                "items": display_items if display_items else None,
                "saved": save_to_inventory,
                "current_page": "imports",
            },
        )

@router.post("/import/janice", response_class=HTMLResponse)
async def handle_import_janice(
    request: Request,
    janice_data: str = Form(...),
    price_source: str = Form("buy"),      # 'buy' or 'sell'
    save_to_inventory: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    rows = parse_janice_rows(janice_data)
    display_items = []

    if not rows:
        return request.app.state.templates.TemplateResponse(
            "import.html",
            {
                "request": request,
                "items": None,
                "saved": False,
                "current_page": "imports",
                "janice_message": "No valid Janice rows found.",
            },
        )

    batch = None
    if save_to_inventory:
        batch = ImportBatch(created_at=datetime.utcnow(), note=f"Janice import ({price_source})")
        db.add(batch)
        await db.flush()

    for r in rows:
        item_name = r["item_name"]
        qty = r["qty"]
        if qty <= 0:
            continue

        if price_source == "sell":
            unit_cost = r["jita_sell"]
        else:
            unit_cost = r["jita_buy"]

        total_cost = unit_cost * qty

        # get or create item
        stmt = select(Item).where(Item.name == item_name)
        res = await db.execute(stmt)
        item = res.scalar_one_or_none()
        if not item:
            item = Item(name=item_name, volume_m3=r["unit_volume"])
            db.add(item)
            await db.flush()
        else:
            # If we don't have volume yet, fill it from Janice
            if item.volume_m3 is None:
                item.volume_m3 = r["unit_volume"]

        # create lot + event
        if save_to_inventory:
            dt_now = datetime.utcnow()

            lot = InventoryLot(
                item_id=item.id,
                quantity_total=qty,
                quantity_remaining=qty,
                unit_cost=unit_cost,
                acquired_at=dt_now,
                source=f"Janice import ({price_source})",
                batch_id=batch.id,
            )
            db.add(lot)
            await db.flush()

            event = InventoryEvent(
                event_type="import",
                eve_time=dt_now,  # no EVE timestamp in Janice, so use "now" UTC
                item_id=item.id,
                lot_id=lot.id,
                quantity=qty,
                unit_price=unit_cost,
                note=f"Janice {price_source}",
            )
            db.add(event)

        display_items.append(
            {
                "item_name": item_name,
                "qty": qty,
                "unit_cost": unit_cost,
                "total_cost": total_cost,
            }
        )

    if save_to_inventory:
        await db.commit()

    return request.app.state.templates.TemplateResponse(
        "import.html",
        {
            "request": request,
            "items": display_items if display_items else None,
            "saved": save_to_inventory,
            "current_page": "imports",
            "janice_message": None,
        },
    )

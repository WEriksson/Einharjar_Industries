from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .db import get_db
from .models import InventoryLot, Item

router = APIRouter()


@router.get("/inventory", response_class=HTMLResponse)
async def inventory_view(request: Request, db: AsyncSession = Depends(get_db)):
    stmt = (
        select(InventoryLot, Item)
        .join(Item, Item.id == InventoryLot.item_id)
        .order_by(InventoryLot.item_id.asc(), InventoryLot.acquired_at.asc())
    )
    res = await db.execute(stmt)
    rows = res.all()

    lots = []
    for lot, item in rows:
        lots.append(
            {
                "id": lot.id,
                "item_name": item.name,
                "qty_remaining": lot.quantity_remaining,
                "unit_cost": lot.unit_cost,
                "source": lot.source,
            }
        )

    return request.app.state.templates.TemplateResponse(
        "inventory.html",
        {
            "request": request,
            "lots": lots,
            "current_page": "inventory",
        },
    )

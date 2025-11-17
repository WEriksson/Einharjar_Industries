from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from .db import get_db
from .models import InventoryEvent, Item

router = APIRouter()


@router.get("/log", response_class=HTMLResponse)
async def log_view(request: Request, db: AsyncSession = Depends(get_db)):
    # Show latest events first
    stmt = (
        select(InventoryEvent, Item)
        .join(Item, Item.id == InventoryEvent.item_id)
        .order_by(desc(InventoryEvent.eve_time))
        .limit(200)
    )
    res = await db.execute(stmt)
    rows = res.all()

    events = []
    for event, item in rows:
        events.append(
            {
                "id": event.id,
                "event_type": event.event_type,
                "eve_time": event.eve_time,  # EVE/UTC
                "item_name": item.name,
                "quantity": event.quantity,
                "unit_price": event.unit_price,
                "note": event.note,
            }
        )

    return request.app.state.templates.TemplateResponse(
        "log.html",
        {
            "request": request,
            "events": events,
            "current_page": "log",
        },
    )
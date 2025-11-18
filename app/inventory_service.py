from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ImportBatch, InventoryEvent, InventoryLot, Item


class InsufficientInventoryError(Exception):
    """Raised when a FIFO consumption request exceeds available inventory."""

    def __init__(self, *, item_id: int, requested: int, available: int) -> None:
        self.item_id = item_id
        self.requested = requested
        self.available = available
        super().__init__(
            f"Insufficient inventory for item {item_id}: requested {requested}, available {available}."
        )


async def create_lot_from_import(
    db: AsyncSession,
    *,
    item: Item,
    quantity: int,
    unit_cost: float,
    acquired_at: Optional[datetime] = None,
    source: Optional[str] = None,
    batch: Optional[ImportBatch] = None,
    note: Optional[str] = None,
    eve_time: Optional[datetime] = None,
) -> InventoryLot:
    """Persist an inventory lot and matching import event."""

    if quantity <= 0:
        raise ValueError("Quantity must be positive for inventory imports.")

    acquired = acquired_at or datetime.utcnow()
    event_time = eve_time or acquired

    lot = InventoryLot(
        item_id=item.id,
        quantity_total=quantity,
        quantity_remaining=quantity,
        unit_cost=unit_cost,
        acquired_at=acquired,
        source=source,
        batch_id=batch.id if batch else None,
    )
    db.add(lot)
    await db.flush()

    event = InventoryEvent(
        event_type="import",
        eve_time=event_time,
        item_id=item.id,
        lot_id=lot.id,
        quantity=quantity,
        unit_price=unit_cost,
        note=note,
    )
    db.add(event)

    return lot


async def consume_lot_fifo(
    db: AsyncSession,
    *,
    item: Item,
    quantity: int,
    eve_time: Optional[datetime] = None,
    event_type: str = "sale",
    note: Optional[str] = None,
    unit_price: Optional[float] = None,
    allow_partial: bool = False,
) -> Dict[str, Any]:
    """Consume inventory lots in FIFO order and record events."""

    if quantity <= 0:
        return {
            "events": [],
            "consumed": 0,
            "requested": quantity,
            "remaining_request": 0,
            "total_available_before": 0,
        }

    stmt_lots = (
        select(InventoryLot)
        .where(
            InventoryLot.item_id == item.id,
            InventoryLot.quantity_remaining > 0,
        )
        .order_by(asc(InventoryLot.acquired_at), asc(InventoryLot.id))
    )
    res_lots = await db.execute(stmt_lots)
    lots: List[InventoryLot] = list(res_lots.scalars())

    total_available = sum(l.quantity_remaining for l in lots)
    if not allow_partial and total_available < quantity:
        raise InsufficientInventoryError(
            item_id=item.id, requested=quantity, available=total_available
        )

    event_time = eve_time or datetime.utcnow()

    remaining = quantity
    consumed = 0
    events: List[InventoryEvent] = []

    for lot in lots:
        if remaining <= 0:
            break
        take = min(lot.quantity_remaining, remaining)
        if take <= 0:
            continue

        lot.quantity_remaining -= take
        if lot.quantity_remaining < 0:
            raise AssertionError("Inventory lot quantity cannot become negative.")

        remaining -= take
        consumed += take

        event = InventoryEvent(
            event_type=event_type,
            eve_time=event_time,
            item_id=item.id,
            lot_id=lot.id,
            quantity=take,
            unit_price=unit_price,
            note=note,
        )
        db.add(event)
        events.append(event)

    return {
        "events": events,
        "consumed": consumed,
        "requested": quantity,
        "remaining_request": max(remaining, 0),
        "total_available_before": total_available,
    }

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


from .db import get_db
from .models import (
    EveCharacter,
    EveCorporation,
    Item,
    InventoryLot,
    InventoryEvent,
    ImportBatch,
    EsiWalletSyncState,
    EsiWalletQueueEntry,
)
from .esi_client import esi_get, get_or_create_item_from_type_id
from .settings_service import get_or_create_settings

router = APIRouter()


def _parse_eve_time(ts: str) -> datetime:
    """
    Parse an EVE/ESI timestamp like '2025-11-17T19:22:00Z' to naive UTC datetime.
    """
    # Ensure it has UTC offset
    if ts.endswith("Z"):
        ts = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts)
    # Store naive UTC in DB (same as we did for other eve_time fields)
    return dt.replace(tzinfo=None)


async def _get_wallet_sync_state(
    db: AsyncSession,
    character: EveCharacter,
) -> Optional[EsiWalletSyncState]:
    stmt = select(EsiWalletSyncState).where(
        EsiWalletSyncState.source_kind == "character",
        EsiWalletSyncState.character_id == character.id,
    )
    res = await db.execute(stmt)
    return res.scalar_one_or_none()


async def _save_wallet_sync_state(
    db: AsyncSession,
    character: EveCharacter,
    last_transaction_id: int,
    state: Optional[EsiWalletSyncState],
) -> None:
    if state is None:
        state = EsiWalletSyncState(
            source_kind="character",
            character_id=character.id,
            last_transaction_id=last_transaction_id,
        )
        db.add(state)
    else:
        state.last_transaction_id = last_transaction_id

    await db.commit()


async def _sync_wallet_for_character(
    db: AsyncSession,
    *,
    character: EveCharacter,
) -> Dict[str, Any]:
    """Process wallet transactions for a single character respecting scan toggles."""

    stats = {
        "character_id": character.id,
        "character_name": character.character_name,
        "new_transactions": 0,
        "queued_imports": 0,
        "auto_sales": 0,
        "unmatched_sale_units": 0,
        "skipped_buys_disabled": 0,
        "skipped_sells_disabled": 0,
        "scanning_disabled": False,
    }

    if not character.wallet_scan_buys and not character.wallet_scan_sells:
        stats["scanning_disabled"] = True
        return stats

    state = await _get_wallet_sync_state(db, character)
    last_id = state.last_transaction_id if state else None

    path = f"/latest/characters/{character.character_id}/wallet/transactions/"
    tx_data = await esi_get(db, path, params=None, character=character, public=False)

    if not isinstance(tx_data, list):
        raise RuntimeError("Unexpected wallet transactions format from ESI")

    txs = sorted(tx_data, key=lambda t: t.get("transaction_id", 0))

    new_last_id = last_id or 0

    for tx in txs:
        tx_id = int(tx["transaction_id"])
        if last_id is not None and tx_id <= last_id:
            continue

        qty = int(tx["quantity"])
        if qty <= 0:
            continue

        is_buy = bool(tx["is_buy"])
        type_id = int(tx["type_id"])
        eve_time = _parse_eve_time(tx["date"])

        item = await get_or_create_item_from_type_id(db, type_id)

        if is_buy:
            if not character.wallet_scan_buys:
                stats["skipped_buys_disabled"] += 1
                if tx_id > new_last_id:
                    new_last_id = tx_id
                continue

            created = await _enqueue_wallet_tx(
                db,
                character=character,
                tx=tx,
                item=item,
                eve_time=eve_time,
            )
            if created:
                stats["queued_imports"] += 1
                stats["new_transactions"] += 1
        else:
            if not character.wallet_scan_sells:
                stats["skipped_sells_disabled"] += 1
                if tx_id > new_last_id:
                    new_last_id = tx_id
                continue

            sale_stats = await _record_sale_from_tx(
                db,
                character=character,
                item=item,
                qty=qty,
                unit_price=float(tx["unit_price"]),
                eve_time=eve_time,
            )
            stats["auto_sales"] += 1
            stats["unmatched_sale_units"] += sale_stats["unmatched"]
            stats["new_transactions"] += 1

        if tx_id > new_last_id:
            new_last_id = tx_id

    progressed = last_id is None or new_last_id > last_id
    if progressed:
        await _save_wallet_sync_state(db, character, new_last_id, state)
    else:
        await db.commit()

    return stats


async def _record_import_from_tx(
    db: AsyncSession,
    *,
    character: EveCharacter,
    item: Item,
    qty: int,
    unit_price: float,
    eve_time: datetime,
    batch: Optional[ImportBatch],
) -> Tuple[ImportBatch, InventoryLot]:
    """
    Create import batch (if needed), inventory lot and event for a buy transaction.
    """
    if batch is None:
        batch = ImportBatch(
            created_at=datetime.utcnow(),
            note=f"Wallet sync for {character.character_name}",
        )
        db.add(batch)
        await db.flush()

    lot = InventoryLot(
        item_id=item.id,
        quantity_total=qty,
        quantity_remaining=qty,
        unit_cost=unit_price,
        acquired_at=eve_time,
        source=f"Wallet buy ({character.character_name})",
        batch_id=batch.id,
    )
    db.add(lot)
    await db.flush()

    event = InventoryEvent(
        event_type="import",
        eve_time=eve_time,
        item_id=item.id,
        lot_id=lot.id,
        quantity=qty,
        unit_price=unit_price,
        note="Wallet buy",
    )
    db.add(event)

    return batch, lot


async def _record_sale_from_tx(
    db: AsyncSession,
    *,
    character: EveCharacter,
    item: Item,
    qty: int,
    unit_price: float,
    eve_time: datetime,
) -> Dict[str, Any]:
    """
    Consume inventory FIFO for a sale transaction and record events.
    Returns stats: how much was actually matched/consumed.
    """
    # Get lots with remaining qty for this item, FIFO by acquired_at then id
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
    remaining = qty
    consumed = 0

    for lot in lots:
        if remaining <= 0:
            break
        take = min(lot.quantity_remaining, remaining)
        if take <= 0:
            continue

        lot.quantity_remaining -= take
        remaining -= take
        consumed += take

        event = InventoryEvent(
            event_type="sale",
            eve_time=eve_time,
            item_id=item.id,
            lot_id=lot.id,
            quantity=take,
            unit_price=unit_price,
            note="Wallet sell",
        )
        db.add(event)

    # We don't raise if we don't have enough; we just consume what we can.
    # You can inspect 'unmatched' in the return value to see missing inventory.
    return {
        "requested": qty,
        "consumed": consumed,
        "unmatched": max(qty - consumed, 0),
        "total_available_before": total_available,
    }


async def sync_character_wallet_once(db: AsyncSession) -> Dict[str, Any]:
    """Fetch recent wallet transactions for every linked character."""

    stmt = select(EveCharacter).order_by(
        EveCharacter.is_default_trader.desc(),
        EveCharacter.id.asc(),
    )
    res = await db.execute(stmt)
    characters = res.scalars().all()

    if not characters:
        raise HTTPException(status_code=400, detail="No EVE characters linked yet.")

    per_character: List[Dict[str, Any]] = []
    totals = {
        "processed_characters": 0,
        "synced_characters": 0,
        "new_transactions": 0,
        "queued_imports": 0,
        "auto_sales": 0,
        "unmatched_sale_units": 0,
        "skipped_buys_disabled": 0,
        "skipped_sells_disabled": 0,
    }

    for ch in characters:
        stats = await _sync_wallet_for_character(db, character=ch)
        per_character.append(stats)
        totals["processed_characters"] += 1
        if not stats["scanning_disabled"]:
            totals["synced_characters"] += 1
        totals["new_transactions"] += stats["new_transactions"]
        totals["queued_imports"] += stats["queued_imports"]
        totals["auto_sales"] += stats["auto_sales"]
        totals["unmatched_sale_units"] += stats["unmatched_sale_units"]
        totals["skipped_buys_disabled"] += stats["skipped_buys_disabled"]
        totals["skipped_sells_disabled"] += stats["skipped_sells_disabled"]

    return {
        "characters": per_character,
        "totals": totals,
    }


async def _enqueue_wallet_tx(
    db: AsyncSession,
    *,
    character: EveCharacter,
    tx: dict,
    item: Item,
    eve_time: datetime,
) -> bool:
    """
    Create a queue entry if it doesn't exist yet.
    Returns True if a new entry was created, False if already queued.
    """
    tx_id = int(tx["transaction_id"])
    is_buy = bool(tx["is_buy"])
    qty = int(tx["quantity"])
    unit_price = float(tx["unit_price"])
    location_id = tx.get("location_id")
    location_name = None  # we can resolve via ESI later if we want

    direction = "import" if is_buy else "sale"

    # Check if already in queue
    stmt = select(EsiWalletQueueEntry).where(
        EsiWalletQueueEntry.source_kind == "character",
        EsiWalletQueueEntry.character_id == character.id,
        EsiWalletQueueEntry.transaction_id == tx_id,
    )
    res = await db.execute(stmt)
    existing = res.scalar_one_or_none()
    if existing:
        return False

    entry = EsiWalletQueueEntry(
        source_kind="character",
        character_id=character.id,
        transaction_id=tx_id,
        direction=direction,
        item_id=item.id,
        quantity=qty,
        unit_price=unit_price,
        location_id=location_id,
        location_name=location_name,
        eve_time=eve_time,
        status="pending",
    )
    db.add(entry)
    # No commit here; caller will commit
    return True

@router.api_route("/wallet/sync-once", methods=["GET", "POST"], response_class=HTMLResponse)
async def wallet_sync_once(request: Request, db: AsyncSession = Depends(get_db)):
    stats = await sync_character_wallet_once(db)

    settings = await get_or_create_settings(db)

    res_chars = await db.execute(select(EveCharacter).order_by(EveCharacter.character_name))
    characters = res_chars.scalars().all()

    res_corps = await db.execute(select(EveCorporation).order_by(EveCorporation.corporation_name))
    corporations = res_corps.scalars().all()

    totals = stats["totals"]
    character_stats = stats["characters"]

    msg_parts = [
        (
            f"Wallet sync ran for {totals['processed_characters']} characters "
            f"({totals['synced_characters']} with scanning enabled)."
        )
    ]

    for entry in character_stats:
        name = entry["character_name"]
        if entry["scanning_disabled"]:
            msg_parts.append(
                f"{name}: skipped because both 'Scan buys' and 'Scan sells' are disabled."
            )
            continue

        line = []
        if entry["new_transactions"] > 0:
            line.append(
                f"processed {entry['new_transactions']} new transactions "
                f"({entry['queued_imports']} buys queued, {entry['auto_sales']} sales applied)"
            )
            if entry["unmatched_sale_units"] > 0:
                line.append(
                    f"{entry['unmatched_sale_units']} sale units had no matching inventory"
                )
        else:
            line.append("no new transactions")

        if entry["skipped_buys_disabled"]:
            line.append(
                f"skipped {entry['skipped_buys_disabled']} buy rows (Scan buys disabled)"
            )
        if entry["skipped_sells_disabled"]:
            line.append(
                f"skipped {entry['skipped_sells_disabled']} sell rows (Scan sells disabled)"
            )

        msg_parts.append(f"{name}: " + ", ".join(line) + ".")

    msg = " ".join(msg_parts)


    return request.app.state.templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": settings,
            "characters": characters,
            "corporations": corporations,
            "current_page": "settings",
            "message": msg,
        },
    )

@router.get("/wallet/queue", response_class=HTMLResponse)
async def wallet_queue(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    settings = await get_or_create_settings(db)

    res_chars = await db.execute(select(EveCharacter).order_by(EveCharacter.character_name))
    characters = res_chars.scalars().all()

    res_corps = await db.execute(select(EveCorporation).order_by(EveCorporation.corporation_name))
    corporations = res_corps.scalars().all()

    # Only show pending entries for now, for any character
    stmt_q = (
    select(EsiWalletQueueEntry)
    .options(
        selectinload(EsiWalletQueueEntry.item),
        selectinload(EsiWalletQueueEntry.character),
        selectinload(EsiWalletQueueEntry.corporation),
        )
        .where(EsiWalletQueueEntry.status == "pending")
        .order_by(EsiWalletQueueEntry.eve_time.desc(), EsiWalletQueueEntry.id.desc())
    )
    res_q = await db.execute(stmt_q)
    entries = res_q.scalars().all()

    return request.app.state.templates.TemplateResponse(
        "wallet_queue.html",
        {
            "request": request,
            "settings": settings,
            "characters": characters,
            "corporations": corporations,
            "entries": entries,
            "current_page": "wallet_queue",
            "message": None,
        },
    )


@router.post("/wallet/queue/apply", response_class=HTMLResponse)
async def wallet_queue_apply(
    request: Request,
    db: AsyncSession = Depends(get_db),
    entry_ids: list[int] = Form([]),
    action: str = Form(...),  # 'apply' or 'ignore'
):
    if not entry_ids:
        # Nothing selected; just go back
        return RedirectResponse("/wallet/queue", status_code=302)

    # Load entries
    stmt = select(EsiWalletQueueEntry).where(EsiWalletQueueEntry.id.in_(entry_ids))
    res = await db.execute(stmt)
    entries = res.scalars().all()

    applied = 0
    ignored = 0
    unmatched_sales = 0

    # We'll reuse a single batch for all imports in this POST
    batch = None

    for e in entries:
        if e.status != "pending":
            continue

        if action == "ignore":
            e.status = "ignored"
            e.applied_at = datetime.utcnow()
            ignored += 1
            continue

        # action == "apply"
        # Need the character & item
        char = e.character
        item = e.item

        if e.direction == "import":
            batch, _lot = await _record_import_from_tx(
                db,
                character=char,
                item=item,
                qty=e.quantity,
                unit_price=e.unit_price,
                eve_time=e.eve_time,
                batch=batch,
            )
        else:
            sale_stats = await _record_sale_from_tx(
                db,
                character=char,
                item=item,
                qty=e.quantity,
                unit_price=e.unit_price,
                eve_time=e.eve_time,
            )
            unmatched_sales += sale_stats["unmatched"]

        e.status = "applied"
        e.applied_at = datetime.utcnow()
        applied += 1

    await db.commit()

    msg_parts = []
    if applied:
        msg_parts.append(f"Applied {applied} wallet entries.")
    if ignored:
        msg_parts.append(f"Ignored {ignored} wallet entries.")
    if unmatched_sales:
        msg_parts.append(f"{unmatched_sales} sale units had no matching inventory.")

    message = " ".join(msg_parts) if msg_parts else "No changes."

    # Re-render queue page with message
    settings = await get_or_create_settings(db)

    res_chars = await db.execute(select(EveCharacter).order_by(EveCharacter.character_name))
    characters = res_chars.scalars().all()

    res_corps = await db.execute(select(EveCorporation).order_by(EveCorporation.corporation_name))
    corporations = res_corps.scalars().all()

    stmt_q = (
        select(EsiWalletQueueEntry)
        .where(EsiWalletQueueEntry.status == "pending")
        .order_by(EsiWalletQueueEntry.eve_time.desc(), EsiWalletQueueEntry.id.desc())
    )
    res_q = await db.execute(stmt_q)
    entries = res_q.scalars().all()

    return request.app.state.templates.TemplateResponse(
        "wallet_queue.html",
        {
            "request": request,
            "settings": settings,
            "characters": characters,
            "corporations": corporations,
            "entries": entries,
            "current_page": "wallet_queue",
            "message": message,
        },
    )
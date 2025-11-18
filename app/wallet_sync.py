from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


from .db import get_db
from .models import (
    EveCharacter,
    EveCorporation,
    Item,
    InventoryLot,
    ImportBatch,
    EsiWalletSyncState,
    EsiWalletQueueEntry,
)
from .esi_client import esi_get, get_or_create_item_from_type_id
from .settings_service import get_or_create_settings
from .inventory_service import create_lot_from_import, consume_lot_fifo

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
    last_transaction_id: Optional[int],
    state: Optional[EsiWalletSyncState],
    *,
    sync_status: str,
    sync_message: Optional[str],
) -> EsiWalletSyncState:
    if state is None:
        state = EsiWalletSyncState(
            source_kind="character",
            character_id=character.id,
        )
        db.add(state)

    if last_transaction_id is not None:
        state.last_transaction_id = last_transaction_id

    state.last_sync_at = datetime.utcnow()
    state.last_sync_status = sync_status
    trimmed_msg = (sync_message or "").strip()
    state.last_sync_message = trimmed_msg[:512] if trimmed_msg else None

    await db.commit()
    await db.refresh(state)
    return state


def _build_character_sync_detail(stats: Dict[str, Any]) -> str:
    if stats.get("scanning_disabled"):
        return "Skipped because both 'Scan buys' and 'Scan sells' are disabled."

    line_parts: List[str] = []
    new_tx = stats.get("new_transactions", 0)
    queued = stats.get("queued_imports", 0)
    auto_sales = stats.get("auto_sales", 0)
    unmatched = stats.get("unmatched_sale_units", 0)

    if new_tx > 0:
        line_parts.append(
            f"processed {new_tx} new transactions ({queued} buys queued, {auto_sales} sales applied)"
        )
        if unmatched > 0:
            line_parts.append(f"{unmatched} sale units had no matching inventory")
    else:
        line_parts.append("no new transactions")

    if stats.get("skipped_buys_disabled"):
        line_parts.append(
            f"skipped {stats['skipped_buys_disabled']} buy rows (Scan buys disabled)"
        )
    if stats.get("skipped_sells_disabled"):
        line_parts.append(
            f"skipped {stats['skipped_sells_disabled']} sell rows (Scan sells disabled)"
        )

    return ", ".join(line_parts) + "."


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

    state = await _get_wallet_sync_state(db, character)
    last_id = state.last_transaction_id if state else None

    if not character.wallet_scan_buys and not character.wallet_scan_sells:
        stats["scanning_disabled"] = True
        stats["sync_status"] = "skipped"
        detail = _build_character_sync_detail(stats)
        stats["sync_detail"] = detail
        await _save_wallet_sync_state(
            db,
            character,
            last_transaction_id=None,
            state=state,
            sync_status="skipped",
            sync_message=detail,
        )
        return stats

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
    detail = _build_character_sync_detail(stats)
    stats["sync_status"] = "ok"
    stats["sync_detail"] = detail

    await _save_wallet_sync_state(
        db,
        character,
        last_transaction_id=new_last_id if progressed else None,
        state=state,
        sync_status="ok",
        sync_message=detail,
    )

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
    """Create import batch (if needed) and persist a wallet-driven lot via the shared service."""
    if batch is None:
        batch = ImportBatch(
            created_at=datetime.utcnow(),
            note=f"Wallet sync for {character.character_name}",
        )
        db.add(batch)
        await db.flush()

    lot = await create_lot_from_import(
        db,
        item=item,
        quantity=qty,
        unit_cost=unit_price,
        acquired_at=eve_time,
        eve_time=eve_time,
        source=f"Wallet buy ({character.character_name})",
        batch=batch,
        note="Wallet buy",
    )

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
    result = await consume_lot_fifo(
        db,
        item=item,
        quantity=qty,
        eve_time=eve_time,
        event_type="sale",
        note="Wallet sell",
        unit_price=unit_price,
        allow_partial=True,
    )

    return {
        "requested": qty,
        "consumed": result["consumed"],
        "unmatched": result["remaining_request"],
        "total_available_before": result["total_available_before"],
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
        try:
            stats = await _sync_wallet_for_character(db, character=ch)
        except Exception as exc:  # noqa: BLE001 - we want to capture all failures per character
            await db.rollback()
            err_msg = f"Sync failed: {exc}"
            stats = {
                "character_id": ch.id,
                "character_name": ch.character_name,
                "new_transactions": 0,
                "queued_imports": 0,
                "auto_sales": 0,
                "unmatched_sale_units": 0,
                "skipped_buys_disabled": 0,
                "skipped_sells_disabled": 0,
                "scanning_disabled": False,
                "sync_status": "error",
                "sync_detail": err_msg + ".",
            }

            state = await _get_wallet_sync_state(db, ch)
            await _save_wallet_sync_state(
                db,
                character=ch,
                last_transaction_id=None,
                state=state,
                sync_status="error",
                sync_message=err_msg,
            )

        per_character.append(stats)
        totals["processed_characters"] += 1
        if stats.get("sync_status") == "ok":
            totals["synced_characters"] += 1
        totals["new_transactions"] += stats.get("new_transactions", 0)
        totals["queued_imports"] += stats.get("queued_imports", 0)
        totals["auto_sales"] += stats.get("auto_sales", 0)
        totals["unmatched_sale_units"] += stats.get("unmatched_sale_units", 0)
        totals["skipped_buys_disabled"] += stats.get("skipped_buys_disabled", 0)
        totals["skipped_sells_disabled"] += stats.get("skipped_sells_disabled", 0)

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

    if not is_buy:
        # Safety guard; sells should never reach the queue layer.
        return False

    qty = int(tx["quantity"])
    unit_price = float(tx["unit_price"])
    location_id = tx.get("location_id")
    location_name = None  # we can resolve via ESI later if we want

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
        direction="import",
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


async def _fetch_wallet_queue_data(
    db: AsyncSession,
    *,
    character_id: Optional[int] = None,
) -> Tuple[List[EsiWalletQueueEntry], List[EveCharacter]]:
    """Return pending wallet queue entries (buys only) and characters that currently have entries."""

    stmt_entries = (
        select(EsiWalletQueueEntry)
        .options(
            selectinload(EsiWalletQueueEntry.item),
            selectinload(EsiWalletQueueEntry.character),
            selectinload(EsiWalletQueueEntry.corporation),
        )
        .where(
            EsiWalletQueueEntry.status == "pending",
            EsiWalletQueueEntry.direction == "import",
        )
        .order_by(EsiWalletQueueEntry.eve_time.desc(), EsiWalletQueueEntry.id.desc())
    )

    if character_id is not None:
        stmt_entries = stmt_entries.where(EsiWalletQueueEntry.character_id == character_id)

    res_entries = await db.execute(stmt_entries)
    entries = list(res_entries.scalars().unique())

    stmt_characters = (
        select(EveCharacter)
        .join(EsiWalletQueueEntry, EsiWalletQueueEntry.character_id == EveCharacter.id)
        .where(
            EsiWalletQueueEntry.status == "pending",
            EsiWalletQueueEntry.direction == "import",
        )
        .order_by(EveCharacter.character_name.asc())
    )
    res_queue_chars = await db.execute(stmt_characters)
    queue_characters = list(res_queue_chars.scalars().unique())

    return entries, queue_characters

@router.api_route("/wallet/sync-once", methods=["GET", "POST"], response_class=HTMLResponse)
async def wallet_sync_once(request: Request, db: AsyncSession = Depends(get_db)):
    stats = await sync_character_wallet_once(db)

    settings = await get_or_create_settings(db)

    res_chars = await db.execute(
        select(EveCharacter)
        .options(selectinload(EveCharacter.wallet_sync_state))
        .order_by(EveCharacter.character_name)
    )
    characters = res_chars.scalars().all()

    res_corps = await db.execute(select(EveCorporation).order_by(EveCorporation.corporation_name))
    corporations = res_corps.scalars().all()

    totals = stats["totals"]
    character_stats = stats["characters"]

    msg_parts = [
        (
            f"Wallet sync ran for {totals['processed_characters']} characters "
            f"({totals['synced_characters']} succeeded)."
        )
    ]

    for entry in character_stats:
        name = entry["character_name"]
        detail = entry.get("sync_detail")
        status = entry.get("sync_status")

        if detail:
            prefix = f"{name}: "
            if status == "error" and not detail.lower().startswith("sync failed"):
                detail = f"Sync failed: {detail}"
            msg_parts.append(prefix + detail)
        else:
            fallback = _build_character_sync_detail(entry)
            msg_parts.append(f"{name}: {fallback}")

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
    character_id: Optional[int] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    settings = await get_or_create_settings(db)

    res_chars = await db.execute(select(EveCharacter).order_by(EveCharacter.character_name))
    characters = res_chars.scalars().all()

    res_corps = await db.execute(select(EveCorporation).order_by(EveCorporation.corporation_name))
    corporations = res_corps.scalars().all()

    entries, queue_characters = await _fetch_wallet_queue_data(db, character_id=character_id)

    if character_id is not None and all(ch.id != character_id for ch in queue_characters):
        extra = next((c for c in characters if c.id == character_id), None)
        if extra:
            queue_characters.append(extra)

    return request.app.state.templates.TemplateResponse(
        "wallet_queue.html",
        {
            "request": request,
            "settings": settings,
            "characters": characters,
            "corporations": corporations,
            "entries": entries,
            "queue_characters": queue_characters,
            "selected_character_id": character_id,
            "current_page": "wallet_queue",
            "message": None,
        },
    )


@router.post("/wallet/queue/apply", response_class=HTMLResponse)
async def wallet_queue_apply(
    request: Request,
    db: AsyncSession = Depends(get_db),
    entry_ids: List[int] = Form([]),
    action: str = Form(...),  # 'apply' or 'ignore'
    selected_character_id: Optional[int] = Form(None),
):
    if not entry_ids:
        settings = await get_or_create_settings(db)

        res_chars = await db.execute(select(EveCharacter).order_by(EveCharacter.character_name))
        characters = res_chars.scalars().all()

        res_corps = await db.execute(
            select(EveCorporation).order_by(EveCorporation.corporation_name)
        )
        corporations = res_corps.scalars().all()

        entries, queue_characters = await _fetch_wallet_queue_data(
            db, character_id=selected_character_id
        )

        if (
            selected_character_id is not None
            and all(ch.id != selected_character_id for ch in queue_characters)
        ):
            extra = next((c for c in characters if c.id == selected_character_id), None)
            if extra:
                queue_characters.append(extra)

        return request.app.state.templates.TemplateResponse(
            "wallet_queue.html",
            {
                "request": request,
                "settings": settings,
                "characters": characters,
                "corporations": corporations,
                "entries": entries,
                "queue_characters": queue_characters,
                "selected_character_id": selected_character_id,
                "current_page": "wallet_queue",
                "message": "No entries selected.",
            },
        )

    # Load entries
    stmt = (
        select(EsiWalletQueueEntry)
        .options(
            selectinload(EsiWalletQueueEntry.item),
            selectinload(EsiWalletQueueEntry.character),
            selectinload(EsiWalletQueueEntry.corporation),
        )
        .where(EsiWalletQueueEntry.id.in_(entry_ids))
    )
    res = await db.execute(stmt)
    entries = res.scalars().all()

    applied = 0
    ignored = 0
    unmatched_sales = 0
    applied_characters: set[int] = set()

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

        if e.direction != "import":
            # Only imports should exist in queue, but guard anyway.
            e.status = "ignored"
            e.applied_at = datetime.utcnow()
            ignored += 1
            continue

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
        if char:
            applied_characters.add(char.id)

    await db.commit()

    msg_parts = []
    if applied:
        msg = f"Applied {applied} wallet entries."
        if applied_characters:
            plural = "s" if len(applied_characters) != 1 else ""
            msg += f" ({len(applied_characters)} character{plural})"
        msg_parts.append(msg)
    if ignored:
        msg_parts.append(f"Ignored {ignored} wallet entries.")
    if unmatched_sales:
        msg_parts.append(f"{unmatched_sales} sale units had no matching inventory.")

    message = " ".join(msg_parts) if msg_parts else "No changes."

    settings = await get_or_create_settings(db)

    res_chars = await db.execute(select(EveCharacter).order_by(EveCharacter.character_name))
    characters = res_chars.scalars().all()

    res_corps = await db.execute(select(EveCorporation).order_by(EveCorporation.corporation_name))
    corporations = res_corps.scalars().all()

    entries, queue_characters = await _fetch_wallet_queue_data(
        db, character_id=selected_character_id
    )

    if (
        selected_character_id is not None
        and all(ch.id != selected_character_id for ch in queue_characters)
    ):
        extra = next((c for c in characters if c.id == selected_character_id), None)
        if extra:
            queue_characters.append(extra)

    return request.app.state.templates.TemplateResponse(
        "wallet_queue.html",
        {
            "request": request,
            "settings": settings,
            "characters": characters,
            "corporations": corporations,
            "entries": entries,
            "queue_characters": queue_characters,
            "selected_character_id": selected_character_id,
            "current_page": "wallet_queue",
            "message": message,
        },
    )
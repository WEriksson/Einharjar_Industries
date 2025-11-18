import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .esi_client import ESIClientError, esi_get
from .models import EveCharacter, FitItem, Item, MarketOrder, MarketScan
from .settings_service import get_or_create_settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/market/scans", response_class=HTMLResponse)
async def market_scans_page(
    request: Request,
    message: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    staging_scan = await _get_latest_scan(db, "staging")
    jita_scan = await _get_latest_scan(db, "jita")
    has_fit_items = await _has_any_fit_items(db)

    flash_message = message or request.query_params.get("message")

    return request.app.state.templates.TemplateResponse(
        "market_scans.html",
        {
            "request": request,
            "current_page": "market_scans",
            "staging_scan": staging_scan,
            "jita_scan": jita_scan,
            "flash_message": flash_message,
            "has_fit_items": has_fit_items,
        },
    )


@router.post("/market/scans/run")
async def run_market_scans(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    type_map, missing_count = await _get_fit_item_type_map(db)
    if not type_map:
        msg = "No fit items with known type IDs to scan yet."
        return RedirectResponse(_scans_redirect_url(msg), status_code=303)

    settings = await get_or_create_settings(db)
    summaries: List[str] = []

    staging_summary = await _run_staging_scan(db, settings=settings, type_map=type_map)
    summaries.append(f"Staging: {staging_summary}")

    jita_summary = await _run_jita_scan(db, settings=settings, type_map=type_map)
    summaries.append(f"Jita: {jita_summary}")

    if missing_count:
        summaries.append(f"Note: {missing_count} fit items are missing eve_type_id and were skipped.")

    message = " | ".join(summaries)
    return RedirectResponse(_scans_redirect_url(message), status_code=303)


async def _run_staging_scan(
    db: AsyncSession,
    *,
    settings,
    type_map: Dict[int, int],
) -> str:
    structure_id = settings.staging_structure_id
    system_id = settings.staging_system_id
    configured_region_id = settings.staging_region_id
    if not structure_id or not system_id:
        return "Skipped (staging structure/system not configured)."

    resolved_region_id = configured_region_id or await _resolve_region_for_system(db, system_id)
    if not resolved_region_id:
        logger.warning(
            "Staging system %s has no region mapping; defaulting region_id to 0 for scan storage.",
            system_id,
        )
    region_id = resolved_region_id or 0

    character = await _get_default_trader(db)
    if not character or not character.refresh_token:
        return "Skipped (no default trader with ESI access)."

    scan = await _create_scan_record(
        db,
        location_kind="staging",
        region_id=region_id,
        system_id=system_id,
        location_id=structure_id,
    )

    try:
        raw_orders = await _fetch_structure_orders(
            db,
            structure_id=structure_id,
            character=character,
            interesting_type_ids=set(type_map.keys()),
        )
        inserted, item_count = await _finalize_scan_success(
            db,
            scan_id=scan.id,
            orders=raw_orders,
            type_map=type_map,
            success_prefix="Fetched staging orders",
        )
        suffix = " (region unresolved)" if not resolved_region_id else ""
        return f"Success ({inserted} orders / {item_count} items){suffix}."
    except Exception as exc:  # noqa: BLE001 - want to surface all failures as scan errors
        logger.exception("Staging market scan failed: %s", exc)
        await _rollback_if_needed(db)
        await _finalize_scan_error(db, scan_id=scan.id, message=f"Staging scan failed: {exc}")
        return "Error (see scan log)."


async def _run_jita_scan(
    db: AsyncSession,
    *,
    settings,
    type_map: Dict[int, int],
) -> str:
    region_id = settings.jita_region_id
    location_id = settings.jita_location_id
    system_id = settings.jita_system_id

    if not region_id or not location_id:
        return "Skipped (Jita region/station not configured)."

    scan = await _create_scan_record(
        db,
        location_kind="jita",
        region_id=region_id,
        system_id=system_id,
        location_id=location_id,
    )

    try:
        raw_orders = await _fetch_jita_orders(
            db,
            region_id=region_id,
            station_id=location_id,
            interesting_type_ids=set(type_map.keys()),
        )
        inserted, item_count = await _finalize_scan_success(
            db,
            scan_id=scan.id,
            orders=raw_orders,
            type_map=type_map,
            success_prefix="Fetched Jita orders",
        )
        return f"Success ({inserted} orders / {item_count} items)."
    except Exception as exc:  # noqa: BLE001 - want to surface all failures as scan errors
        logger.exception("Jita market scan failed: %s", exc)
        await _rollback_if_needed(db)
        await _finalize_scan_error(db, scan_id=scan.id, message=f"Jita scan failed: {exc}")
        return "Error (see scan log)."


async def _get_latest_scan(db: AsyncSession, location_kind: str) -> Optional[MarketScan]:
    stmt = (
        select(MarketScan)
        .where(MarketScan.location_kind == location_kind)
        .order_by(MarketScan.created_at.desc())
        .limit(1)
    )
    res = await db.execute(stmt)
    return res.scalar_one_or_none()


async def _has_any_fit_items(db: AsyncSession) -> bool:
    stmt = select(FitItem.id).limit(1)
    res = await db.execute(stmt)
    return res.scalar_one_or_none() is not None


async def _get_fit_item_type_map(db: AsyncSession) -> Tuple[Dict[int, int], int]:
    stmt = (
        select(Item.id, Item.eve_type_id)
        .join(FitItem, FitItem.item_id == Item.id)
        .distinct()
    )
    res = await db.execute(stmt)
    rows = res.all()

    type_map: Dict[int, int] = {}
    missing_type = 0
    for item_id, eve_type_id in rows:
        if eve_type_id is None:
            missing_type += 1
            continue
        type_map[int(eve_type_id)] = int(item_id)

    return type_map, missing_type


async def _resolve_region_for_system(db: AsyncSession, system_id: Optional[int]) -> Optional[int]:
    if not system_id:
        return None
    try:
        data = await esi_get(
            db,
            f"/latest/universe/systems/{system_id}/",
            params=None,
            character=None,
            public=True,
        )
        region_id = data.get("region_id")
        return int(region_id) if region_id is not None else None
    except ESIClientError as exc:
        logger.warning("Failed to resolve region for system %s: %s", system_id, exc)
        return None


async def _get_default_trader(db: AsyncSession) -> Optional[EveCharacter]:
    stmt = select(EveCharacter).order_by(
        EveCharacter.is_default_trader.desc(),
        EveCharacter.id.asc(),
    )
    res = await db.execute(stmt)
    return res.scalars().first()


async def _create_scan_record(
    db: AsyncSession,
    *,
    location_kind: str,
    region_id: int,
    system_id: Optional[int],
    location_id: Optional[int],
) -> MarketScan:
    scan = MarketScan(
        location_kind=location_kind,
        region_id=region_id,
        system_id=system_id,
        location_id=location_id,
        status="pending",
    )
    db.add(scan)
    await db.commit()
    await db.refresh(scan)
    return scan


async def _fetch_structure_orders(
    db: AsyncSession,
    *,
    structure_id: int,
    character: EveCharacter,
    interesting_type_ids: set[int],
) -> List[dict]:
    page = 1
    orders: List[dict] = []
    while True:
        data = await esi_get(
            db,
            f"/latest/markets/structures/{structure_id}/",
            params={"page": page},
            character=character,
            public=False,
        )
        if not isinstance(data, list):
            break
        filtered = [row for row in data if row.get("type_id") in interesting_type_ids]
        orders.extend(filtered)
        if len(data) < 1000:
            break
        page += 1
    return orders


async def _fetch_jita_orders(
    db: AsyncSession,
    *,
    region_id: int,
    station_id: int,
    interesting_type_ids: set[int],
) -> List[dict]:
    orders: List[dict] = []
    for type_id in interesting_type_ids:
        page = 1
        while True:
            data = await esi_get(
                db,
                f"/latest/markets/{region_id}/orders/",
                params={
                    "type_id": type_id,
                    "order_type": "all",
                    "page": page,
                },
                character=None,
                public=True,
            )
            if not isinstance(data, list):
                break
            filtered = [row for row in data if row.get("location_id") == station_id]
            orders.extend(filtered)
            if len(data) < 1000:
                break
            page += 1
    return orders


async def _finalize_scan_success(
    db: AsyncSession,
    *,
    scan_id: int,
    orders: List[dict],
    type_map: Dict[int, int],
    success_prefix: str,
) -> Tuple[int, int]:
    stmt = select(MarketScan).where(MarketScan.id == scan_id)
    res = await db.execute(stmt)
    scan = res.scalar_one()

    inserted = 0
    touched_types: set[int] = set()

    for order in orders:
        type_id = order.get("type_id")
        if type_id is None:
            continue
        item_id = type_map.get(int(type_id))
        if not item_id:
            continue

        db.add(
            MarketOrder(
                scan_id=scan.id,
                item_id=item_id,
                is_buy_order=bool(order.get("is_buy_order")),
                price=float(order.get("price", 0.0)),
                volume_remain=_int_or_zero(order.get("volume_remain")),
                volume_total=_int_or_zero(order.get("volume_total")),
                issued_at=_parse_esi_datetime(order.get("issued")),
                duration=_int_or_none(order.get("duration")),
                order_id=int(order.get("order_id")),
                min_volume=_int_or_none(order.get("min_volume")),
            )
        )
        inserted += 1
        touched_types.add(int(type_id))

    scan.status = "success"
    scan.message = f"{success_prefix} ({inserted} orders / {len(touched_types)} items)"
    await db.commit()
    await db.refresh(scan)
    return inserted, len(touched_types)


async def _finalize_scan_error(db: AsyncSession, *, scan_id: int, message: str) -> None:
    stmt = select(MarketScan).where(MarketScan.id == scan_id)
    res = await db.execute(stmt)
    scan = res.scalar_one()
    scan.status = "error"
    scan.message = message[:250]
    await db.commit()
    await db.refresh(scan)


async def _rollback_if_needed(db: AsyncSession) -> None:
    if db.in_transaction():
        await db.rollback()


def _parse_esi_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.replace(tzinfo=None)
    except ValueError:
        return None


def _int_or_zero(value: Optional[int]) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: Optional[int]) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scans_redirect_url(message: str) -> str:
    params = urlencode({"message": message})
    return f"/market/scans?{params}"

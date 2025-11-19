from datetime import datetime
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .db import get_db
from .eve_types_service import apply_eve_type_data_to_items
from .fit_availability import (
    compute_fit_availability_summaries,
    compute_fit_detail_data,
    merge_missing_items,
)
from .models import Fit, FitItem, Item, MarketScan, WatchlistItem
from .settings_service import get_or_create_settings

router = APIRouter()


def parse_eft_fit(eft_text: str) -> Dict[str, int]:
    """Parse EFT format text into aggregated item quantities, including hull."""
    items: Dict[str, int] = {}
    if not eft_text:
        return items

    header_consumed = False
    for raw_line in eft_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.endswith("/offline"):
            line = line[: -len("/offline")].strip()
            if not line:
                continue

        if not header_consumed:
            header_consumed = True
            if line.startswith("[") and "]" in line:
                closing_idx = line.find("]")
                header_contents = line[1:closing_idx]
                hull_name = header_contents.split(",", 1)[0].strip()
                if hull_name:
                    items[hull_name] = items.get(hull_name, 0) + 1
                continue

        # Skip section markers like "[Empty High slot]"
        if line.startswith("[") and line.endswith("]"):
            continue

        segments = [seg.strip() for seg in line.split(",") if seg.strip()]
        if not segments:
            continue

        for segment in segments:
            quantity = 1
            item_name = segment
            if " x" in segment:
                maybe_name, maybe_qty = segment.rsplit(" x", 1)
                if maybe_qty.strip().isdigit():
                    item_name = maybe_name.strip()
                    quantity = int(maybe_qty.strip())

            item_name = item_name.strip()
            if not item_name:
                continue
            items[item_name] = items.get(item_name, 0) + quantity

    return items


def _extract_eft_header(eft_text: str) -> Tuple[Optional[str], Optional[str]]:
    for raw_line in eft_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and "]" in line:
            closing_idx = line.find("]")
            header_contents = line[1:closing_idx]
            parts = [seg.strip() for seg in header_contents.split(",", 1)]
            hull = parts[0] if parts else None
            fit_name = parts[1] if len(parts) > 1 else None
            return (hull or None, fit_name or None)
    return (None, None)


def _derive_fit_name_from_eft(eft_text: str) -> str:
    hull, fit_name = _extract_eft_header(eft_text)
    if fit_name:
        return fit_name
    if hull:
        return hull
    return "Unnamed fit"


async def _get_fit(db: AsyncSession, fit_id: int) -> Fit:
    stmt = (
        select(Fit)
        .where(Fit.id == fit_id)
        .options(
            selectinload(Fit.fit_items)
            .selectinload(FitItem.item)
            .selectinload(Item.eve_type)
        )
    )
    res = await db.execute(stmt)
    fit = res.scalar_one_or_none()
    if not fit:
        raise HTTPException(status_code=404, detail="Fit not found")
    return fit


async def _apply_eft_to_fit(
    db: AsyncSession,
    *,
    fit: Fit,
    eft_text: str,
    parsed_items: Optional[Dict[str, int]] = None,
) -> bool:
    items = parsed_items if parsed_items is not None else parse_eft_fit(eft_text or "")
    if not items:
        return False

    if "fit_items" not in fit.__dict__:
        await db.refresh(fit, attribute_names=["fit_items"])

    items_needing_type: Dict[str, Item] = {}

    item_names = list(items.keys())
    existing_items: Dict[str, Item] = {}
    if item_names:
        stmt_items = select(Item).where(Item.name.in_(item_names))
        res_items = await db.execute(stmt_items)
        existing_items = {item.name: item for item in res_items.scalars().all()}

    fit_items_by_item_id = {fi.item_id: fi for fi in fit.fit_items}
    for name, qty in items.items():
        if qty <= 0:
            continue

        item = existing_items.get(name)
        if not item:
            item = Item(name=name)
            db.add(item)
            await db.flush()
            existing_items[name] = item

        if not item.eve_type_id or (item.volume_m3 is None or item.volume_m3 == 0):
            items_needing_type[name] = item

        fit_item = fit_items_by_item_id.get(item.id)
        if fit_item:
            fit_item.quantity_per_fit += qty
        else:
            new_fit_item = FitItem(
                fit_id=fit.id,
                item_id=item.id,
                quantity_per_fit=qty,
            )
            db.add(new_fit_item)
            fit_items_by_item_id[item.id] = new_fit_item

    if items_needing_type:
        await apply_eve_type_data_to_items(db, items_needing_type)

    cleaned_eft = (eft_text or "").strip()
    if cleaned_eft:
        fit.eft_text = cleaned_eft
    fit.updated_at = datetime.utcnow()
    return True


def _guess_fit_hull_name(fit: Fit) -> Optional[str]:
    for fi in fit.fit_items:
        item = fi.item
        if not item:
            continue
        eve_type = getattr(item, "eve_type", None)
        if eve_type and getattr(eve_type, "category_id", None) == 6:
            return item.name
    for fi in fit.fit_items:
        if fi.quantity_per_fit == 1 and fi.item:
            return fi.item.name
    return None


@router.get("/fits", response_class=HTMLResponse)
async def list_fits(request: Request, db: AsyncSession = Depends(get_db)):
    stmt_fits = (
        select(Fit)
        .options(
            selectinload(Fit.fit_items)
            .selectinload(FitItem.item)
            .selectinload(Item.eve_type)
        )
        .order_by(Fit.is_active.desc(), Fit.name.asc())
    )
    res_fits = await db.execute(stmt_fits)
    fits = res_fits.scalars().all()

    settings = await get_or_create_settings(db)
    availability_summaries = await compute_fit_availability_summaries(db, fits, settings)

    stmt_watch = (
        select(WatchlistItem)
        .options(selectinload(WatchlistItem.item))
        .order_by(WatchlistItem.is_active.desc(), WatchlistItem.created_at.asc())
    )
    res_watch = await db.execute(stmt_watch)
    watchlist = res_watch.scalars().all()

    latest_staging_scan = await _get_latest_scan(db, "staging")
    latest_jita_scan = await _get_latest_scan(db, "jita")
    combined_missing = merge_missing_items(availability_summaries.values())

    return request.app.state.templates.TemplateResponse(
        "fits.html",
        {
            "request": request,
            "fits": fits,
            "availability_summaries": availability_summaries,
            "watchlist": watchlist,
            "current_page": "fits",
            "latest_staging_scan": latest_staging_scan,
            "latest_jita_scan": latest_jita_scan,
            "combined_missing": combined_missing,
        },
    )


@router.get("/fits/new", response_class=HTMLResponse)
async def new_fit_form(request: Request):
    return request.app.state.templates.TemplateResponse(
        "fit_new.html",
        {
            "request": request,
            "current_page": "fits",
            "error": None,
            "form_values": {"eft_text": "", "target_copies": 0},
        },
    )


@router.post("/fits/new")
async def create_fit(
    request: Request,
    eft_text: str = Form(...),
    target_copies: int = Form(0),
    db: AsyncSession = Depends(get_db),
):
    eft_text = (eft_text or "").strip()
    target_copies = max(0, target_copies or 0)
    if not eft_text:
        return request.app.state.templates.TemplateResponse(
            "fit_new.html",
            {
                "request": request,
                "current_page": "fits",
                "error": "Paste an EFT fit first.",
                "form_values": {"eft_text": eft_text, "target_copies": target_copies},
            },
            status_code=400,
        )

    parsed_items = parse_eft_fit(eft_text)
    if not parsed_items:
        return request.app.state.templates.TemplateResponse(
            "fit_new.html",
            {
                "request": request,
                "current_page": "fits",
                "error": "No recognizable EFT items found.",
                "form_values": {"eft_text": eft_text, "target_copies": target_copies},
            },
            status_code=400,
        )

    fit_name = _derive_fit_name_from_eft(eft_text).strip() or "Unnamed fit"
    fit = Fit(
        name=fit_name,
        target_copies=target_copies,
        is_active=True,
        eft_text=eft_text,
    )
    db.add(fit)
    await db.flush()

    await _apply_eft_to_fit(db, fit=fit, eft_text=eft_text, parsed_items=parsed_items)
    await db.commit()
    return RedirectResponse(f"/fits/{fit.id}", status_code=302)


@router.get("/fits/{fit_id}", response_class=HTMLResponse)
async def fit_detail(request: Request, fit_id: int, db: AsyncSession = Depends(get_db)):
    fit = await _get_fit(db, fit_id)
    settings = await get_or_create_settings(db)
    availability_summary, item_rows = await compute_fit_detail_data(db, fit, settings)
    hull_name = _guess_fit_hull_name(fit)

    return request.app.state.templates.TemplateResponse(
        "fit_detail.html",
        {
            "request": request,
            "fit": fit,
            "item_rows": item_rows,
            "availability_summary": availability_summary,
            "hull_name": hull_name,
            "current_page": "fits",
        },
    )


@router.post("/fits/{fit_id}/edit")
async def update_fit(
    fit_id: int,
    name: str = Form(...),
    target_copies: int = Form(0),
    is_active: Optional[bool] = Form(False),
    db: AsyncSession = Depends(get_db),
):
    fit = await _get_fit(db, fit_id)
    fit.name = name.strip()
    fit.target_copies = max(0, target_copies or 0)
    fit.is_active = bool(is_active)
    fit.updated_at = datetime.utcnow()
    await db.commit()
    return RedirectResponse(f"/fits/{fit.id}", status_code=302)


@router.post("/fits/{fit_id}/target")
async def update_fit_target(
    fit_id: int,
    target_copies: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    fit = await _get_fit(db, fit_id)
    fit.target_copies = max(0, target_copies or 0)
    fit.updated_at = datetime.utcnow()
    await db.commit()
    return RedirectResponse(f"/fits/{fit.id}", status_code=302)


@router.post("/fits/{fit_id}/items/add")
async def add_fit_item(
    fit_id: int,
    item_id: int = Form(...),
    quantity_per_fit: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    fit = await _get_fit(db, fit_id)
    stmt_item = select(Item).where(Item.id == item_id)
    res_item = await db.execute(stmt_item)
    item = res_item.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=400, detail="Item not found")

    if quantity_per_fit <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be positive")

    fit_item = FitItem(
        fit_id=fit.id,
        item_id=item.id,
        quantity_per_fit=quantity_per_fit,
    )
    db.add(fit_item)
    await db.commit()
    return RedirectResponse(f"/fits/{fit.id}", status_code=302)


@router.post("/fits/{fit_id}/items/{fit_item_id}/delete")
async def delete_fit_item(fit_id: int, fit_item_id: int, db: AsyncSession = Depends(get_db)):
    fit = await _get_fit(db, fit_id)
    stmt = select(FitItem).where(FitItem.id == fit_item_id, FitItem.fit_id == fit.id)
    res = await db.execute(stmt)
    fit_item = res.scalar_one_or_none()
    if not fit_item:
        raise HTTPException(status_code=404, detail="Fit item not found")
    await db.delete(fit_item)
    await db.commit()
    return RedirectResponse(f"/fits/{fit.id}", status_code=302)


@router.post("/fits/{fit_id}/import-eft")
async def import_eft_fit(
    fit_id: int,
    eft_text: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    fit = await _get_fit(db, fit_id)
    changed = await _apply_eft_to_fit(db, fit=fit, eft_text=eft_text)
    if changed:
        await db.commit()
    else:
        await db.rollback()
    return RedirectResponse(f"/fits/{fit.id}", status_code=302)


@router.post("/fits/{fit_id}/delete")
async def delete_fit(fit_id: int, db: AsyncSession = Depends(get_db)):
    fit = await _get_fit(db, fit_id)
    await db.delete(fit)
    await db.commit()
    return RedirectResponse("/fits", status_code=302)


async def _get_latest_scan(db: AsyncSession, location_kind: str) -> Optional[MarketScan]:
    stmt = (
        select(MarketScan)
        .where(MarketScan.location_kind == location_kind)
        .order_by(MarketScan.created_at.desc())
        .limit(1)
    )
    res = await db.execute(stmt)
    return res.scalar_one_or_none()


@router.post("/watchlist/add")
async def add_watchlist_item(
    item_id: int = Form(...),
    target_quantity: int = Form(0),
    note: Optional[str] = Form(None),
    is_active: Optional[bool] = Form(False),
    db: AsyncSession = Depends(get_db),
):
    stmt_item = select(Item).where(Item.id == item_id)
    res_item = await db.execute(stmt_item)
    item = res_item.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=400, detail="Item not found")

    stmt_existing = select(WatchlistItem).where(WatchlistItem.item_id == item.id)
    res_existing = await db.execute(stmt_existing)
    existing = res_existing.scalar_one_or_none()

    if existing:
        existing.target_quantity = target_quantity or 0
        existing.note = note or None
        existing.is_active = bool(is_active)
    else:
        watch_entry = WatchlistItem(
            item_id=item.id,
            target_quantity=target_quantity or 0,
            note=note or None,
            is_active=bool(is_active),
        )
        db.add(watch_entry)

    await db.commit()
    return RedirectResponse("/fits", status_code=302)


@router.post("/watchlist/{watchlist_id}/deactivate")
async def deactivate_watchlist_item(watchlist_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(WatchlistItem).where(WatchlistItem.id == watchlist_id)
    res = await db.execute(stmt)
    entry = res.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    entry.is_active = False
    await db.commit()
    return RedirectResponse("/fits", status_code=302)

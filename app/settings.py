from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .db import get_db
from .models import EveCharacter, EveCorporation
from .settings_service import get_or_create_settings, get_or_create_default_user

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request, db: AsyncSession = Depends(get_db)):
    # Ensure a default user & settings exist
    await get_or_create_default_user(db)
    settings = await get_or_create_settings(db)

    # These will be empty until we add SSO, but wiring them now is fine
    res_chars = await db.execute(
        select(EveCharacter)
        .options(selectinload(EveCharacter.wallet_sync_state))
        .order_by(EveCharacter.character_name)
    )
    characters = res_chars.scalars().all()

    res_corps = await db.execute(select(EveCorporation).order_by(EveCorporation.corporation_name))
    corporations = res_corps.scalars().all()

    return request.app.state.templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": settings,
            "characters": characters,
            "corporations": corporations,
            "current_page": "settings",
            "message": None,
        },
    )


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(
    request: Request,
    db: AsyncSession = Depends(get_db),
    staging_system_id: int | None = Form(None),
    staging_system_name: str | None = Form(None),
    staging_structure_id: int | None = Form(None),
    staging_structure_name: str | None = Form(None),
    jita_system_id: int | None = Form(None),
    jita_location_id: int | None = Form(None),
    market_scan_interval_minutes: int | None = Form(None),
):
    form_data = await request.form()

    settings = await get_or_create_settings(db)

    settings.staging_system_id = staging_system_id
    settings.staging_system_name = staging_system_name or None
    settings.staging_structure_id = staging_structure_id
    settings.staging_structure_name = staging_structure_name or None

    # For now we assume Jita region = 10000002 (The Forge)
    settings.jita_region_id = 10000002
    settings.jita_system_id = jita_system_id
    settings.jita_location_id = jita_location_id

    if market_scan_interval_minutes and market_scan_interval_minutes > 0:
        settings.market_scan_interval_minutes = market_scan_interval_minutes

    res_chars = await db.execute(
        select(EveCharacter)
        .options(selectinload(EveCharacter.wallet_sync_state))
        .order_by(EveCharacter.character_name)
    )
    characters = res_chars.scalars().all()

    for ch in characters:
        buys_key = f"char_scan_buys_{ch.id}"
        sells_key = f"char_scan_sells_{ch.id}"

        ch.wallet_scan_buys = buys_key in form_data
        ch.wallet_scan_sells = sells_key in form_data

    await db.commit()
    await db.refresh(settings)

    res_corps = await db.execute(select(EveCorporation).order_by(EveCorporation.corporation_name))
    corporations = res_corps.scalars().all()

    return request.app.state.templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": settings,
            "characters": characters,
            "corporations": corporations,
            "current_page": "settings",
            "message": "Settings saved (including wallet scan toggles).",
        },
    )

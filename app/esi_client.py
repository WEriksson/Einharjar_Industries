import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .models import EveCharacter, AppSettings, Item

# ESI base + token endpoints
ESI_BASE_URL = "https://esi.evetech.net"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"

# Env config
CLIENT_ID = os.getenv("EVE_CLIENT_ID")
CLIENT_SECRET = os.getenv("EVE_CLIENT_SECRET")
ESI_USER_AGENT = os.getenv("ESI_USER_AGENT", "EinharjarIndustriesTool/0.1 (ESI)")
ESI_COMPAT_DATE = os.getenv("ESI_COMPAT_DATE")  # optional override

# In-memory access token cache per character
# key: EveCharacter.id, value: {"access_token": str, "expires_at": datetime}
_token_cache: Dict[int, Dict[str, Any]] = {}


async def _get_compat_date(db: AsyncSession) -> Optional[str]:
    """
    Determine X-Compatibility-Date.
    Prefer value from AppSettings; fall back to env if set.
    """
    if ESI_COMPAT_DATE:
        return ESI_COMPAT_DATE

    stmt = select(AppSettings).where(AppSettings.id == 1)
    res = await db.execute(stmt)
    settings = res.scalar_one_or_none()
    if settings and settings.compatibility_date:
        return settings.compatibility_date

    return None


async def get_access_token(db: AsyncSession, character: EveCharacter) -> str:
    """
    Get a valid access token for a character.
    Uses in-memory cache and refresh_token from DB.
    """
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("EVE_CLIENT_ID and EVE_CLIENT_SECRET must be set for ESI calls")

    if character.id in _token_cache:
        cached = _token_cache[character.id]
        expires_at: datetime = cached["expires_at"]
        # Small safety margin of 60 seconds
        if expires_at - timedelta(seconds=60) > datetime.now(timezone.utc):
            return cached["access_token"]

    if not character.refresh_token:
        raise RuntimeError(f"Character {character.character_name} has no refresh token stored")

    # Refresh access token
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": character.refresh_token,
            },
            auth=(CLIENT_ID, CLIENT_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()

    access_token = data["access_token"]
    refresh_token = data.get("refresh_token", character.refresh_token)
    expires_in = int(data.get("expires_in", 1200))  # seconds; default 20 min

    # Update DB with new refresh token if changed
    if refresh_token != character.refresh_token:
        character.refresh_token = refresh_token
        db.add(character)
        await db.commit()
        await db.refresh(character)

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    _token_cache[character.id] = {
        "access_token": access_token,
        "expires_at": expires_at,
    }
    return access_token


async def esi_get(
    db: AsyncSession,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    character: Optional[EveCharacter] = None,
    public: bool = False,
) -> Any:
    """
    Generic GET helper for ESI.
    - path should start with "/": e.g. "/latest/characters/{id}/wallet/transactions/"
    - if public=True: no auth header is sent
    - otherwise: use the given character's access token
    """
    if not path.startswith("/"):
        raise ValueError("ESI path must start with '/'")

    url = f"{ESI_BASE_URL}{path}"
    headers = {
        "User-Agent": ESI_USER_AGENT,
        "Accept": "application/json",
    }

    compat_date = await _get_compat_date(db)
    if compat_date:
        headers["X-Compatibility-Date"] = compat_date

    if not public:
        if character is None:
            raise RuntimeError("character must be provided for authenticated ESI calls")
        access_token = await get_access_token(db, character)
        headers["Authorization"] = f"Bearer {access_token}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=headers, params=params)
        # Basic handling for rate-limit / error-limit (could be extended later)
        if resp.status_code in (420, 429):
            # In a background job we'd probably sleep & retry; here just raise
            raise RuntimeError(
                f"Hit ESI rate/error limit: {resp.status_code} {resp.text}"
            )
        resp.raise_for_status()
        return resp.json()

async def get_or_create_item_from_type_id(
    db: AsyncSession,
    type_id: int,
) -> Item:
    """
    Resolve an Item row from an EVE type_id.

    - First try eve_type_id (preferred).
    - If not found, fetch type info from ESI (public).
    - Then try by name:
      - If an item with that name exists and eve_type_id is NULL, attach type_id to it.
      - If an item with that name exists and eve_type_id != type_id, raise.
      - Otherwise, create a new item row.
    """
    # 1) Try existing item by eve_type_id
    stmt = select(Item).where(Item.eve_type_id == type_id)
    res = await db.execute(stmt)
    item = res.scalar_one_or_none()
    if item:
        return item

    # 2) Fetch type info from ESI
    data = await esi_get(
        db,
        f"/latest/universe/types/{type_id}/",
        params=None,
        character=None,
        public=True,
    )
    name = data.get("name") or f"Type {type_id}"
    volume = data.get("volume")  # packaged volume (packaged volume for ships, etc.)

    # 3) Try to find existing item by name
    stmt_by_name = select(Item).where(Item.name == name)
    res_by_name = await db.execute(stmt_by_name)
    existing_by_name = res_by_name.scalar_one_or_none()

    if existing_by_name:
        # If it doesn't have eve_type_id yet, attach it
        if existing_by_name.eve_type_id is None:
            existing_by_name.eve_type_id = type_id
            # Optionally update volume if we have it
            if volume is not None:
                existing_by_name.volume_m3 = volume
            db.add(existing_by_name)
            await db.flush()
            return existing_by_name
        # If it has a different eve_type_id, something is inconsistent
        if existing_by_name.eve_type_id != type_id:
            raise RuntimeError(
                f"Item name '{name}' already exists with different eve_type_id "
                f"({existing_by_name.eve_type_id} != {type_id})."
            )
        # Same type_id, just return it
        return existing_by_name

    # 4) No existing item; create a new one
    item = Item(
        name=name,
        eve_type_id=type_id,
        volume_m3=volume,
    )
    db.add(item)
    await db.flush()
    return item
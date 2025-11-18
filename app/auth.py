import logging
import os
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .db import get_db
from .models import EveCharacter, EveCorporation, EveCorpLink
from .settings_service import get_or_create_default_user
from .esi_client import esi_get

router = APIRouter()
logger = logging.getLogger(__name__)

# --- Config from environment ---

CLIENT_ID = os.getenv("EVE_CLIENT_ID")
CLIENT_SECRET = os.getenv("EVE_CLIENT_SECRET")
CALLBACK_URL = os.getenv("EVE_CALLBACK_URL")
SCOPES = os.getenv(
    "EVE_SCOPES",
    "esi-wallet.read_character_wallet.v1",
)

if not CLIENT_ID or not CLIENT_SECRET or not CALLBACK_URL:
    # Fail fast so it's obvious something is missing
    raise RuntimeError("EVE_CLIENT_ID, EVE_CLIENT_SECRET, and EVE_CALLBACK_URL must be set")

AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"


# --- Helpers ---

async def _fetch_character_corp_info(
    db: AsyncSession,
    access_token: str,
    character_id: int,
) -> tuple[int | None, str | None]:
    """Fetch corporation info for a character without performing discovery."""

    cdata = await esi_get(
        db,
        f"/latest/characters/{character_id}/",
        character=None,
        public=False,
        access_token_override=access_token,
        force_refresh=True,
    )
    corp_id = cdata.get("corporation_id")
    corp_name = None

    if corp_id is not None:
        corp_resp = await esi_get(
            db,
            f"/latest/corporations/{corp_id}/",
            character=None,
            public=True,
            force_refresh=True,
        )
        corp_name = corp_resp.get("name")

    return corp_id, corp_name


# --- Routes ---

@router.get("/auth/login", response_class=HTMLResponse)
async def auth_login(request: Request):
    """
    Start EVE SSO flow: redirect to login.eveonline.com.
    """
    state = secrets.token_urlsafe(32)

    params = {
        "response_type": "code",
        "redirect_uri": CALLBACK_URL,
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "state": state,
    }
    url = f"{AUTH_URL}?{urlencode(params)}"

    resp = RedirectResponse(url)
    # Store state in a short-lived cookie
    resp.set_cookie(
        "eve_auth_state",
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        # set secure=True when you run behind HTTPS
    )
    return resp


@router.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Handle SSO callback: exchange code -> tokens, verify, store character.
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    cookie_state = request.cookies.get("eve_auth_state")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    if not state or not cookie_state or state != cookie_state:
        raise HTTPException(status_code=400, detail="Invalid SSO state")

    # Exchange code for tokens
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_resp = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": CALLBACK_URL,
                },
                auth=(CLIENT_ID, CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()

            access_token = token_data["access_token"]
            refresh_token = token_data.get("refresh_token")
    except httpx.HTTPError as exc:
        logger.error("Token exchange failed (status=%s)", getattr(exc.response, "status_code", "n/a"))
        raise HTTPException(status_code=400, detail="Token exchange failed.") from exc

    # Verify token to get character info
    try:
        v = await esi_get(
            db,
            "/verify",
            character=None,
            public=False,
            access_token_override=access_token,
            force_refresh=True,
        )
    except Exception as exc:
        logger.error("Verify call failed: %s", exc)
        raise HTTPException(status_code=400, detail="Verify call failed.") from exc

    character_id = int(v["CharacterID"])
    character_name = v["CharacterName"]
    scopes = v.get("Scopes", "")
    owner_hash = v.get("CharacterOwnerHash")

    # Fetch corp info via ESI
    corp_id, corp_name = await _fetch_character_corp_info(db, access_token, character_id)

    # Attach to default local user
    user = await get_or_create_default_user(db)

    # Upsert EveCharacter
    stmt = select(EveCharacter).where(EveCharacter.character_id == character_id)
    res = await db.execute(stmt)
    char = res.scalar_one_or_none()

    if char is None:
        # Is this the first character for this user?
        res_all = await db.execute(
            select(EveCharacter.id)
            .where(EveCharacter.user_id == user.id)
            .limit(1)
        )
        has_any = res_all.scalar_one_or_none() is not None

        char = EveCharacter(
            user_id=user.id,
            character_id=character_id,
            character_name=character_name,
            corporation_id=corp_id,
            corporation_name=corp_name,
            refresh_token=refresh_token,
            scopes=scopes,
            owner_hash=owner_hash,
            is_main=not has_any,
            is_default_trader=not has_any,
        )
        db.add(char)
        await db.flush()
    else:
        char.character_name = character_name
        char.corporation_id = corp_id
        char.corporation_name = corp_name
        char.refresh_token = refresh_token
        char.scopes = scopes
        char.owner_hash = owner_hash

    # Upsert corporation + link
    if corp_id is not None and corp_name:
        stmt_c = select(EveCorporation).where(EveCorporation.corporation_id == corp_id)
        res_c = await db.execute(stmt_c)
        corp = res_c.scalar_one_or_none()
        if corp is None:
            corp = EveCorporation(
                corporation_id=corp_id,
                corporation_name=corp_name,
            )
            db.add(corp)
            await db.flush()

        # Link char <-> corp
        stmt_link = select(EveCorpLink).where(
            EveCorpLink.character_id == char.id,
            EveCorpLink.corporation_id == corp.id,
        )
        res_link = await db.execute(stmt_link)
        link = res_link.scalar_one_or_none()
        if link is None:
            link = EveCorpLink(
                character_id=char.id,
                corporation_id=corp.id,
                has_wallet_access=False,
                has_structure_access=False,
                wallet_divisions=None,
            )
            db.add(link)

    await db.commit()

    # Redirect back to settings
    resp = RedirectResponse("/settings", status_code=302)
    resp.delete_cookie("eve_auth_state")
    return resp

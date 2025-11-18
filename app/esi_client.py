import asyncio
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional, Tuple

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
ESI_USER_AGENT = os.getenv(
    "ESI_USER_AGENT",
    "EinharjarIndustries/0.1 (contact: TODO@example.com)",
)
ESI_COMPAT_DATE = os.getenv("ESI_COMPAT_DATE")  # optional override

# In-memory access token cache per character
# key: EveCharacter.id, value: {"access_token": str, "expires_at": datetime}
_token_cache: Dict[int, Dict[str, Any]] = {}

_http_client: Optional[httpx.AsyncClient] = None
_http_client_lock = asyncio.Lock()

_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = asyncio.Lock()

_error_pause_until: Optional[datetime] = None
_error_lock = asyncio.Lock()
ERROR_LIMIT_THRESHOLD = 2
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 0.5

logger = logging.getLogger(__name__)


class ESIClientError(RuntimeError):
    def __init__(self, *, path: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.status_code = status_code


class ESIThrottleError(ESIClientError):
    pass


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        async with _http_client_lock:
            if _http_client is None:
                timeout = httpx.Timeout(20.0, connect=10.0)
                _http_client = httpx.AsyncClient(
                    base_url=ESI_BASE_URL,
                    timeout=timeout,
                    headers={
                        "User-Agent": ESI_USER_AGENT,
                        "Accept": "application/json",
                    },
                )
    return _http_client


def _parse_http_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.replace(tzinfo=None)
    except Exception:
        return None


def _cache_key(path: str, params: Optional[Dict[str, Any]], auth_identity: str) -> str:
    from json import dumps

    serialized_params = dumps(params or {}, sort_keys=True, default=str)
    return f"GET|{path}|{serialized_params}|{auth_identity}"


async def _get_cached_response(key: str) -> Optional[Dict[str, Any]]:
    async with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        expires_at: Optional[datetime] = entry.get("expires_at")
        if expires_at and expires_at <= datetime.utcnow():
            _cache.pop(key, None)
            return None
        return entry


async def _store_cache_response(
    key: str,
    *,
    data: Any,
    expires_at: Optional[datetime],
    etag: Optional[str],
):
    if expires_at is None or expires_at <= datetime.utcnow():
        return
    async with _cache_lock:
        _cache[key] = {
            "data": data,
            "expires_at": expires_at,
            "etag": etag,
        }


async def _maybe_wait_for_error_window() -> None:
    while True:
        async with _error_lock:
            if _error_pause_until is None:
                return
            now = datetime.utcnow()
            if now >= _error_pause_until:
                _reset_error_pause()
                return
            delay = (_error_pause_until - now).total_seconds()
        await asyncio.sleep(delay)


def _reset_error_pause() -> None:
    global _error_pause_until
    _error_pause_until = None


async def _schedule_error_pause(seconds: int, *, reason: str) -> None:
    global _error_pause_until
    async with _error_lock:
        pause_until = datetime.utcnow() + timedelta(seconds=max(seconds, 1))
        _error_pause_until = pause_until
        logger.warning("ESI throttle engaged (%s); pausing until %s", reason, pause_until.isoformat())


def _extract_error_headers(resp: httpx.Response) -> Tuple[Optional[int], Optional[int]]:
    remain = resp.headers.get("X-ESI-Error-Limit-Remain")
    reset = resp.headers.get("X-ESI-Error-Limit-Reset")
    try:
        remain_val = int(remain) if remain is not None else None
        reset_val = int(reset) if reset is not None else None
        return remain_val, reset_val
    except ValueError:
        return None, None


def _auth_identity(public: bool, character: Optional[EveCharacter], access_token: Optional[str]) -> str:
    if public:
        return "public"
    if character is not None:
        return f"character:{character.id}"
    if access_token:
        digest = hashlib.sha256(access_token.encode()).hexdigest()[:12]
        return f"token:{digest}"
    return "unknown"


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


async def _request_with_retries(
    *,
    method: str,
    path: str,
    params: Optional[Dict[str, Any]],
    headers: Dict[str, str],
    cache_entry: Optional[Dict[str, Any]],
) -> httpx.Response:
    client = await _get_http_client()

    for attempt in range(1, MAX_RETRIES + 1):
        await _maybe_wait_for_error_window()
        try:
            resp = await client.request(method, path, params=params, headers=headers)
        except httpx.RequestError as exc:
            if attempt == MAX_RETRIES:
                logger.error("ESI request failed after retries %s %s: %s", method, path, exc)
                raise ESIClientError(path=path, status_code=-1, message=str(exc)) from exc
            await asyncio.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            continue

        remain, reset = _extract_error_headers(resp)
        if remain is not None and reset is not None and remain <= ERROR_LIMIT_THRESHOLD:
            await _schedule_error_pause(reset, reason="error-limit low")

        if resp.status_code == 304 and cache_entry is not None:
            return resp

        if resp.status_code == 420:
            await _schedule_error_pause(reset or 60, reason="error-limit hit")
            raise ESIThrottleError(
                path=path,
                status_code=resp.status_code,
                message="ESI error-limit reached",
            )

        if resp.status_code == 429:
            await _schedule_error_pause(reset or 10, reason="rate limited")
            if attempt == MAX_RETRIES:
                raise ESIThrottleError(
                    path=path,
                    status_code=resp.status_code,
                    message="ESI rate limit reached",
                )
            await asyncio.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            continue

        if 500 <= resp.status_code < 600:
            if attempt == MAX_RETRIES:
                logger.error(
                    "ESI request failed after retries %s %s (status %s)",
                    method,
                    path,
                    resp.status_code,
                )
                raise ESIClientError(
                    path=path,
                    status_code=resp.status_code,
                    message=f"ESI server error {resp.status_code}",
                )
            await asyncio.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            continue

        if resp.status_code >= 400:
            raise ESIClientError(
                path=path,
                status_code=resp.status_code,
                message=resp.text or "ESI request failed",
            )

        return resp

    raise ESIClientError(path=path, status_code=-1, message="ESI request retry loop exhausted")


async def esi_get(
    db: AsyncSession,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    character: Optional[EveCharacter] = None,
    public: bool = False,
    *,
    access_token_override: Optional[str] = None,
    force_refresh: bool = False,
) -> Any:
    """Centralized GET helper with caching, retries, and auth handling."""

    if not path.startswith("/"):
        raise ValueError("ESI path must start with '/' for consistency")

    headers: Dict[str, str] = {}

    compat_date = await _get_compat_date(db)
    if compat_date:
        headers["X-Compatibility-Date"] = compat_date

    auth_identity = _auth_identity(public, character, access_token_override)

    if not public:
        token: Optional[str]
        if access_token_override:
            token = access_token_override
        else:
            if character is None:
                raise RuntimeError("character must be provided for authenticated ESI calls")
            token = await get_access_token(db, character)
        headers["Authorization"] = f"Bearer {token}"

    cache_key = _cache_key(path, params, auth_identity)
    cache_entry: Optional[Dict[str, Any]] = None
    if not force_refresh:
        cache_entry = await _get_cached_response(cache_key)
        if cache_entry and cache_entry.get("data") is not None:
            return cache_entry["data"]
        if cache_entry and cache_entry.get("etag"):
            headers["If-None-Match"] = cache_entry["etag"]

    response = await _request_with_retries(
        method="GET",
        path=path,
        params=params,
        headers=headers,
        cache_entry=cache_entry,
    )

    if response.status_code == 304 and cache_entry:
        expires_at = _parse_http_date(response.headers.get("Expires")) or cache_entry.get("expires_at")
        await _store_cache_response(
            cache_key,
            data=cache_entry["data"],
            expires_at=expires_at,
            etag=cache_entry.get("etag"),
        )
        return cache_entry["data"]

    data = response.json()
    expires_at = _parse_http_date(response.headers.get("Expires"))
    etag = response.headers.get("ETag")

    logger.debug(
        "ESI GET %s params=%s status=%s error_remain=%s",
        path,
        params,
        response.status_code,
        response.headers.get("X-ESI-Error-Limit-Remain"),
    )

    if expires_at is not None:
        await _store_cache_response(
            cache_key,
            data=data,
            expires_at=expires_at,
            etag=etag,
        )

    return data

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
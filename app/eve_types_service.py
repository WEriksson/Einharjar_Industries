from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import EveType, Item


def _clean_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    cleaned = name.strip()
    return cleaned or None


async def resolve_type_id_for_name(db: AsyncSession, name: str) -> Optional[int]:
    """Return the type ID for a single EVE item name using SDE data."""
    cleaned = _clean_name(name)
    if not cleaned:
        return None

    stmt = select(EveType.type_id).where(EveType.name == cleaned)
    res = await db.execute(stmt)
    type_id = res.scalar_one_or_none()
    if type_id is not None:
        return int(type_id)

    stmt_ci = select(EveType.type_id).where(func.lower(EveType.name) == cleaned.lower())
    res_ci = await db.execute(stmt_ci)
    type_id_ci = res_ci.scalar_one_or_none()
    return int(type_id_ci) if type_id_ci is not None else None


async def resolve_type_ids_for_names(db: AsyncSession, names: Iterable[str]) -> Dict[str, int]:
    """Resolve many names at once, minimizing queries."""
    cleaned_to_originals: Dict[str, List[str]] = {}
    for original in names:
        cleaned = _clean_name(original)
        if not cleaned:
            continue
        cleaned_to_originals.setdefault(cleaned, []).append(original)

    if not cleaned_to_originals:
        return {}

    result: Dict[str, int] = {}
    pending_exact = set(cleaned_to_originals.keys())

    stmt_exact = select(EveType.name, EveType.type_id).where(EveType.name.in_(pending_exact))
    res_exact = await db.execute(stmt_exact)
    for row_name, type_id in res_exact.all():
        pending_exact.discard(row_name)
        for original in cleaned_to_originals.get(row_name, []):
            result[original] = int(type_id)

    if not pending_exact:
        return result

    lower_targets: Dict[str, List[str]] = {}
    for name in pending_exact:
        lower_targets.setdefault(name.lower(), []).append(name)

    stmt_ci = select(EveType.name, EveType.type_id).where(func.lower(EveType.name).in_(lower_targets.keys()))
    res_ci = await db.execute(stmt_ci)
    for row_name, type_id in res_ci.all():
        key = row_name.lower()
        for cleaned in lower_targets.get(key, []):
            for original in cleaned_to_originals.get(cleaned, []):
                if original not in result:
                    result[original] = int(type_id)

    return result


async def fetch_eve_types_by_ids(db: AsyncSession, type_ids: Iterable[int]) -> Dict[int, EveType]:
    ids = {int(tid) for tid in type_ids if tid is not None}
    if not ids:
        return {}
    stmt = select(EveType).where(EveType.type_id.in_(ids))
    res = await db.execute(stmt)
    return {etype.type_id: etype for etype in res.scalars()}


async def apply_eve_type_data_to_items(db: AsyncSession, items_by_name: Dict[str, Item]) -> None:
    """Populate eve_type_id and volume_m3 on Items when SDE data exists."""
    if not items_by_name:
        return

    names = list(items_by_name.keys())
    name_to_type_id = await resolve_type_ids_for_names(db, names)
    if not name_to_type_id:
        return

    type_rows = await fetch_eve_types_by_ids(db, set(name_to_type_id.values()))

    for name, item in items_by_name.items():
        type_id = name_to_type_id.get(name)
        if not type_id:
            continue
        if not item.eve_type_id:
            item.eve_type_id = type_id
        evetype = type_rows.get(type_id)
        if evetype and (item.volume_m3 is None or item.volume_m3 == 0):
            item.volume_m3 = evetype.volume_m3
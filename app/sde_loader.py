"""Utility to load EVE type data from the YAML SDE into the local database."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from .db import Base, DATABASE_URL
from .models import EveType

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DEFAULT_SDE_PATH = Path(__file__).resolve().parent.parent / "sde" / "types.yaml"
CHUNK_SIZE = 1000


async def load_type_ids(file_path: Path | str | None = None) -> None:
    path = Path(file_path or os.getenv("SDE_TYPE_IDS_PATH", DEFAULT_SDE_PATH))
    if not path.exists():
        raise FileNotFoundError(f"SDE file not found: {path}")

    logger.info("Loading SDE types from %s", path)

    engine = create_async_engine(DATABASE_URL, future=True)
    async_session = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    with path.open("r", encoding="utf-8") as fh:
        data: Dict[Any, Any] = yaml.safe_load(fh) or {}

    processed = 0

    async with async_session() as session:
        for chunk in _chunks(data.items(), CHUNK_SIZE):
            rows = []
            for type_id_raw, payload in chunk:
                try:
                    type_id = int(type_id_raw)
                except (TypeError, ValueError):
                    continue

                name_dict = payload.get("name") or {}
                english_name = name_dict.get("en")
                if not english_name:
                    continue

                rows.append(
                    {
                        "type_id": type_id,
                        "name": english_name,
                        "group_id": payload.get("groupID"),
                        "category_id": payload.get("categoryID"),
                        "market_group_id": payload.get("marketGroupID"),
                        "is_published": payload.get("published"),
                        "volume_m3": payload.get("volume"),
                    }
                )

            if not rows:
                continue

            stmt = insert(EveType).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=[EveType.type_id],
                set_={
                    "name": stmt.excluded.name,
                    "group_id": stmt.excluded.group_id,
                    "category_id": stmt.excluded.category_id,
                    "market_group_id": stmt.excluded.market_group_id,
                    "is_published": stmt.excluded.is_published,
                    "volume_m3": stmt.excluded.volume_m3,
                },
            )
            await session.execute(stmt)
            processed += len(rows)

        await session.commit()

    logger.info("Finished loading %s type rows", processed)


def _chunks(items: Iterable[Any], size: int) -> Iterable[List[Any]]:
    bucket: List[Any] = []
    for item in items:
        bucket.append(item)
        if len(bucket) >= size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket


async def _main() -> None:
    await load_type_ids()


if __name__ == "__main__":
    asyncio.run(_main())

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from datetime import datetime

from .models import AppSettings, AppUser


async def get_or_create_default_user(db: AsyncSession) -> AppUser:
    stmt = select(AppUser).limit(1)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    if user:
        return user

    user = AppUser(display_name="Default user")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def get_or_create_settings(db: AsyncSession) -> AppSettings:
    # Ensure new columns exist for older databases
    await db.execute(text("""
        ALTER TABLE app_settings
        ADD COLUMN IF NOT EXISTS staging_region_id INTEGER
    """))
    await db.commit()

    stmt = select(AppSettings).where(AppSettings.id == 1)
    res = await db.execute(stmt)
    s = res.scalar_one_or_none()
    if s:
        return s

    s = AppSettings(
        id=1,
        # sensible defaults for now
        market_scan_interval_minutes=15,
        compatibility_date=datetime.utcnow().date().isoformat(),
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s

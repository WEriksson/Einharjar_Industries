from decimal import Decimal

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    ForeignKey,
    BigInteger,
    Boolean,
    UniqueConstraint,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship
from datetime import datetime

from .db import Base


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, index=True, nullable=False)
    eve_type_id = Column(Integer, unique=True, index=True, nullable=True)
    volume_m3 = Column(Float, nullable=True)

    # NEW: backrefs for relationships in InventoryLot / InventoryEvent
    lots = relationship("InventoryLot", back_populates="item")
    events = relationship("InventoryEvent", back_populates="item")



class ImportBatch(Base):
    __tablename__ = "import_batches"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    note = Column(String, nullable=True)

    lots = relationship("InventoryLot", back_populates="batch")


class InventoryLot(Base):
    """
    A single FIFO lot of items you imported or bought.
    quantity_remaining will be consumed in FIFO order later
    when you sell/use items.
    """
    __tablename__ = "inventory_lots"

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)

    quantity_total = Column(BigInteger, nullable=False)
    quantity_remaining = Column(BigInteger, nullable=False)

    unit_cost = Column(Float, nullable=False)  # full cost per unit
    acquired_at = Column(DateTime, default=datetime.utcnow, index=True)
    source = Column(String, nullable=True)
    batch_id = Column(Integer, ForeignKey("import_batches.id"), nullable=True)

    item = relationship("Item", back_populates="lots")
    batch = relationship("ImportBatch", back_populates="lots")

    @hybrid_property
    def unit_total_cost(self) -> Decimal:
        """Return per-unit total cost (unit_cost already includes shipping)."""
        if self.unit_cost is None:
            return Decimal("0")
        return Decimal(str(self.unit_cost))

    @unit_total_cost.expression
    def unit_total_cost(cls):
        return cls.unit_cost


class InventoryEvent(Base):
    """
    Log of imports/exports/usage using EVE time (UTC).
    We'll use this for your import/export log view.
    """
    __tablename__ = "inventory_events"

    id = Column(Integer, primary_key=True)
    event_type = Column(String, nullable=False)  # 'import', 'export', 'industry'
    eve_time = Column(DateTime, nullable=False)  # EVE/UTC time
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    lot_id = Column(Integer, ForeignKey("inventory_lots.id"), nullable=True)

    quantity = Column(BigInteger, nullable=False)
    unit_price = Column(Float, nullable=True)   # buy price / sell price / unit cost
    note = Column(String, nullable=True)

    item = relationship("Item", back_populates="events")
    lot = relationship("InventoryLot")

class AppUser(Base):
    """
    Local user for this app. For now you'll probably only ever have one row.
    """
    __tablename__ = "app_users"

    id = Column(Integer, primary_key=True)
    display_name = Column(String, nullable=True)

    characters = relationship("EveCharacter", back_populates="user")


class EveCharacter(Base):
    """
    An EVE character linked via SSO.
    """
    __tablename__ = "eve_characters"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("app_users.id"), nullable=False)

    character_id = Column(Integer, unique=True, index=True, nullable=False)
    character_name = Column(String, nullable=False)

    corporation_id = Column(Integer, nullable=True)
    corporation_name = Column(String, nullable=True)

    refresh_token = Column(String, nullable=True)  # store encrypted later
    scopes = Column(String, nullable=True)         # space-separated scopes string
    owner_hash = Column(String, nullable=True)

    is_main = Column(Boolean, default=False)
    is_default_trader = Column(Boolean, default=False)

    is_default_wallet = Column(Boolean, nullable=False, default=False)
    wallet_scan_buys = Column(Boolean, nullable=False, default=True)
    wallet_scan_sells = Column(Boolean, nullable=False, default=True)

    user = relationship("AppUser", back_populates="characters")
    corp_links = relationship("EveCorpLink", back_populates="character")
    wallet_sync_state = relationship(
        "EsiWalletSyncState",
        back_populates="character",
        uselist=False,
    )


class EveCorporation(Base):
    """
    Corporation linked via one or more characters (for corp trading).
    """
    __tablename__ = "eve_corporations"

    id = Column(Integer, primary_key=True)
    corporation_id = Column(Integer, unique=True, index=True, nullable=False)
    corporation_name = Column(String, nullable=False)

    wallet_scan_buys = Column(Boolean, nullable=False, default=True)
    wallet_scan_sells = Column(Boolean, nullable=False, default=True)

    default_division_id = Column(Integer, nullable=True)

    links = relationship("EveCorpLink", back_populates="corporation")


class EveCorpLink(Base):
    """
    Link between a character and a corporation, describing what we use this character for.
    """
    __tablename__ = "eve_corp_links"

    id = Column(Integer, primary_key=True)
    character_id = Column(Integer, ForeignKey("eve_characters.id"), nullable=False)
    corporation_id = Column(Integer, ForeignKey("eve_corporations.id"), nullable=False)

    # Flags describing what we use this character for (ESI-wise)
    has_wallet_access = Column(Boolean, default=False)
    has_structure_access = Column(Boolean, default=False)

    # For now, store corp wallet divisions as comma-separated string; refine later if needed.
    wallet_divisions = Column(String, nullable=True)

    character = relationship("EveCharacter", back_populates="corp_links")
    corporation = relationship("EveCorporation", back_populates="links")


class AppSettings(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)

    # Market / location settings
    staging_system_id = Column(Integer, nullable=True)
    staging_system_name = Column(String, nullable=True)

    # EVE structure IDs are 64-bit, so use BigInteger
    staging_structure_id = Column(BigInteger, nullable=True)
    staging_structure_name = Column(String, nullable=True)

    jita_region_id = Column(Integer, nullable=True)
    jita_system_id = Column(Integer, nullable=True)
    # Station / structure IDs can also be 64-bit
    jita_location_id = Column(BigInteger, nullable=True)


    # Automation settings
    auto_import_source = Column(String, nullable=True)  # 'character', 'corporation', 'off'
    auto_import_character_id = Column(Integer, ForeignKey("eve_characters.id"), nullable=True)
    auto_import_corp_id = Column(Integer, ForeignKey("eve_corporations.id"), nullable=True)
    auto_import_corp_division_id = Column(Integer, nullable=True)

    auto_export_source = Column(String, nullable=True)  # same as above
    auto_export_character_id = Column(Integer, ForeignKey("eve_characters.id"), nullable=True)
    auto_export_corp_id = Column(Integer, ForeignKey("eve_corporations.id"), nullable=True)
    auto_export_corp_division_id = Column(Integer, nullable=True)

    market_scan_interval_minutes = Column(Integer, nullable=True, default=15)

    # ESI compatibility (for X-Compatibility-Date header later)
    compatibility_date = Column(String, nullable=True)  # e.g. '2025-11-17'


class EsiWalletSyncState(Base):
    __tablename__ = "esi_wallet_sync_state"
    id = Column(Integer, primary_key=True)
    source_kind = Column(String, nullable=False)  # 'character' or 'corp'
    character_id = Column(Integer, ForeignKey("eve_characters.id"), nullable=True)
    corporation_id = Column(Integer, ForeignKey("eve_corporations.id"), nullable=True)
    division_id = Column(Integer, nullable=True)
    last_transaction_id = Column(BigInteger, nullable=True)
    last_sync_at = Column(DateTime, nullable=True)
    last_sync_status = Column(String(32), nullable=True)
    last_sync_message = Column(String(512), nullable=True)

    character = relationship("EveCharacter", back_populates="wallet_sync_state")

class EsiWalletQueueEntry(Base):
    __tablename__ = "esi_wallet_queue"

    id = Column(Integer, primary_key=True)

    source_kind = Column(String, nullable=False)  # 'character' or 'corp'
    character_id = Column(Integer, ForeignKey("eve_characters.id"), nullable=True)
    corporation_id = Column(Integer, ForeignKey("eve_corporations.id"), nullable=True)

    transaction_id = Column(BigInteger, nullable=False)

    direction = Column(String, nullable=False)  # 'import' or 'sale'
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False)

    location_id = Column(BigInteger, nullable=True)
    location_name = Column(String, nullable=True)

    eve_time = Column(DateTime, nullable=False)

    status = Column(String, nullable=False, default="pending")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    applied_at = Column(DateTime, nullable=True)

    # make these eager to avoid async lazy-load in templates
    item = relationship("Item", lazy="joined")
    character = relationship("EveCharacter", lazy="joined")
    corporation = relationship("EveCorporation", lazy="joined")

    __table_args__ = (
        UniqueConstraint(
            "source_kind", "character_id", "corporation_id", "transaction_id",
            name="uq_wallet_queue_source_tx",
        ),
    )
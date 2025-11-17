from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    ForeignKey,
    BigInteger,
)
from sqlalchemy.orm import relationship
from datetime import datetime

from .db import Base


class Item(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
    type_id = Column(Integer, unique=True, index=True, nullable=True)
    name = Column(String, unique=True, index=True, nullable=False)
    volume_m3 = Column(Float, nullable=True)

    lots = relationship("InventoryLot", back_populates="item")


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

    item = relationship("Item")
    lot = relationship("InventoryLot")

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .models import Fit, FitItem, InventoryLot, Item, MarketOrder, MarketScan

@dataclass
class MarketStats:
    quantity: int
    min_price: Optional[Decimal]


@dataclass
class FitItemAvailabilityRow:
    item: Item
    required_per_fit: int
    inventory_qty: int
    my_stock_copies_for_this_item: int
    staging_market_qty: int
    staging_min_price: Optional[Decimal]
    jita_min_price: Optional[Decimal]
    deficit_for_target: int


@dataclass
class FitAvailabilitySummary:
    fit: Fit
    target_copies: int
    my_stock_copies: int
    staging_copies: int
    total_copies_possible: int
    staging_price_per_fit: Optional[Decimal]
    jita_price_per_fit: Optional[Decimal]
    status: str
    status_category: str
    latest_staging_scan: Optional[MarketScan]
    latest_jita_scan: Optional[MarketScan]


class _FitAvailabilityData:
    def __init__(
        self,
        *,
        fit_items_by_fit: Dict[int, List[FitItem]],
        inventory_qty_by_item: Dict[int, int],
        staging_stats: Dict[int, MarketStats],
        jita_stats: Dict[int, MarketStats],
        latest_staging_scan: Optional[MarketScan],
        latest_jita_scan: Optional[MarketScan],
    ) -> None:
        self.fit_items_by_fit = fit_items_by_fit
        self.inventory_qty_by_item = inventory_qty_by_item
        self.staging_stats = staging_stats
        self.jita_stats = jita_stats
        self.latest_staging_scan = latest_staging_scan
        self.latest_jita_scan = latest_jita_scan

    @classmethod
    async def build(cls, db: AsyncSession, fits: List[Fit]) -> "_FitAvailabilityData":
        if not fits:
            return cls(
                fit_items_by_fit={},
                inventory_qty_by_item={},
                staging_stats={},
                jita_stats={},
                latest_staging_scan=None,
                latest_jita_scan=None,
            )

        fit_ids = [fit.id for fit in fits]
        stmt = (
            select(FitItem)
            .options(selectinload(FitItem.item))
            .where(FitItem.fit_id.in_(fit_ids))
        )
        res = await db.execute(stmt)
        fit_items = res.scalars().all()

        fit_items_by_fit: Dict[int, List[FitItem]] = {}
        item_ids: set[int] = set()
        for fi in fit_items:
            if not fi.item_id or fi.quantity_per_fit <= 0:
                continue
            if fi.item is None:
                continue
            fit_items_by_fit.setdefault(fi.fit_id, []).append(fi)
            item_ids.add(fi.item_id)

        inventory_qty_by_item: Dict[int, int] = {}
        if item_ids:
            stmt_inv = (
                select(
                    InventoryLot.item_id,
                    func.coalesce(func.sum(InventoryLot.quantity_remaining), 0).label("qty"),
                )
                .where(
                    InventoryLot.item_id.in_(item_ids),
                    InventoryLot.quantity_remaining > 0,
                )
                .group_by(InventoryLot.item_id)
            )
            res_inv = await db.execute(stmt_inv)
            inventory_qty_by_item = {item_id: int(qty or 0) for item_id, qty in res_inv.all()}

        latest_staging_scan, staging_stats = await _load_market_stats(
            db, "staging", item_ids, use_buy_orders=False
        )
        latest_jita_scan, jita_stats = await _load_market_stats(
            db, "jita", item_ids, use_buy_orders=False
        )

        return cls(
            fit_items_by_fit=fit_items_by_fit,
            inventory_qty_by_item=inventory_qty_by_item,
            staging_stats=staging_stats,
            jita_stats=jita_stats,
            latest_staging_scan=latest_staging_scan,
            latest_jita_scan=latest_jita_scan,
        )

    def build_item_rows(self, fit: Fit) -> List[FitItemAvailabilityRow]:
        rows: List[FitItemAvailabilityRow] = []
        target = fit.target_copies or 0
        for fi in self.fit_items_by_fit.get(fit.id, []):
            item = fi.item
            if not item:
                continue
            required = fi.quantity_per_fit
            inventory_qty = self.inventory_qty_by_item.get(fi.item_id, 0)
            my_stock_copies = inventory_qty // required if required else 0

            staging_entry = self.staging_stats.get(fi.item_id)
            staging_qty = staging_entry.quantity if staging_entry else 0
            staging_price = staging_entry.min_price if staging_entry else None

            jita_entry = self.jita_stats.get(fi.item_id)
            jita_price = jita_entry.min_price if jita_entry else None

            deficit = 0
            if target > 0 and required > 0:
                deficit = max(target * required - inventory_qty, 0)

            rows.append(
                FitItemAvailabilityRow(
                    item=item,
                    required_per_fit=required,
                    inventory_qty=inventory_qty,
                    my_stock_copies_for_this_item=my_stock_copies,
                    staging_market_qty=staging_qty,
                    staging_min_price=staging_price,
                    jita_min_price=jita_price,
                    deficit_for_target=deficit,
                )
            )
        return rows


def _calculate_status(target: int, stock: int, market: int) -> tuple[str, str]:
    if target <= 0:
        return "No target", "none"
    if stock >= target:
        return "OK", "stock"
    if stock + market >= target:
        return "OK", "market"
    return (f"Short ({stock + market}/{target})", "short")


async def compute_fit_availability_summaries(
    db: AsyncSession, fits: List[Fit]
) -> Dict[int, FitAvailabilitySummary]:
    data = await _FitAvailabilityData.build(db, fits)
    summaries: Dict[int, FitAvailabilitySummary] = {}
    for fit in fits:
        summaries[fit.id] = _build_summary_for_fit(fit, data)
    return summaries


async def compute_fit_item_rows(
    db: AsyncSession, fit: Fit
) -> List[FitItemAvailabilityRow]:
    data = await _FitAvailabilityData.build(db, [fit])
    return data.build_item_rows(fit)


async def compute_fit_detail_data(
    db: AsyncSession, fit: Fit
) -> tuple[FitAvailabilitySummary, List[FitItemAvailabilityRow]]:
    data = await _FitAvailabilityData.build(db, [fit])
    summary = _build_summary_for_fit(fit, data)
    rows = data.build_item_rows(fit)
    return summary, rows


def _build_summary_for_fit(fit: Fit, data: _FitAvailabilityData) -> FitAvailabilitySummary:
    rows = data.build_item_rows(fit)
    if rows:
        my_stock_copies = min(row.my_stock_copies_for_this_item for row in rows)
        staging_copies = min(
            (row.staging_market_qty // row.required_per_fit) if row.required_per_fit else 0
            for row in rows
        )
    else:
        my_stock_copies = 0
        staging_copies = 0

    total_copies_possible = my_stock_copies + staging_copies

    staging_price = _aggregate_price(rows, source="staging")
    jita_price = _aggregate_price(rows, source="jita")

    status, status_category = _calculate_status(
        fit.target_copies or 0, my_stock_copies, staging_copies
    )

    return FitAvailabilitySummary(
        fit=fit,
        target_copies=fit.target_copies or 0,
        my_stock_copies=my_stock_copies,
        staging_copies=staging_copies,
        total_copies_possible=total_copies_possible,
        staging_price_per_fit=staging_price,
        jita_price_per_fit=jita_price,
        status=status,
        status_category=status_category,
        latest_staging_scan=data.latest_staging_scan,
        latest_jita_scan=data.latest_jita_scan,
    )


def _aggregate_price(rows: List[FitItemAvailabilityRow], *, source: str) -> Optional[Decimal]:
    if not rows:
        return None
    total = Decimal("0")
    for row in rows:
        price: Optional[Decimal]
        if source == "staging":
            price = row.staging_min_price
        else:
            price = row.jita_min_price
        if price is None:
            return None
        qty = Decimal(row.required_per_fit)
        total += qty * price
    return total


async def _load_market_stats(
    db: AsyncSession,
    location_kind: str,
    item_ids: Iterable[int],
    *,
    use_buy_orders: bool,
) -> tuple[Optional[MarketScan], Dict[int, MarketStats]]:
    stmt_scan = (
        select(MarketScan)
        .where(MarketScan.location_kind == location_kind)
        .order_by(MarketScan.created_at.desc())
        .limit(1)
    )
    res_scan = await db.execute(stmt_scan)
    scan = res_scan.scalar_one_or_none()
    if not scan:
        return None, {}

    ids_list = list(item_ids)
    if not ids_list:
        return scan, {}

    price_fn = func.max if use_buy_orders else func.min
    stmt_orders = (
        select(
            MarketOrder.item_id,
            func.coalesce(func.sum(MarketOrder.volume_remain), 0).label("qty"),
            price_fn(MarketOrder.price).label("price_value"),
        )
        .where(
            MarketOrder.scan_id == scan.id,
            MarketOrder.is_buy_order == use_buy_orders,
            MarketOrder.item_id.in_(ids_list),
        )
        .group_by(MarketOrder.item_id)
    )
    res_orders = await db.execute(stmt_orders)

    stats: Dict[int, MarketStats] = {}
    for item_id, qty, price_value in res_orders.all():
        stats[int(item_id)] = MarketStats(
            quantity=int(qty or 0),
            min_price=Decimal(str(price_value)) if price_value is not None else None,
        )
    return scan, stats
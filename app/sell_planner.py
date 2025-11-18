from decimal import Decimal, ROUND_HALF_UP
import re
from typing import List, Tuple, Optional, Dict, Any

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .db import get_db
from .models import Item, InventoryLot

router = APIRouter()


def parse_sell_planner_input(raw: str) -> List[Tuple[str, int]]:
    """
    Parse user input for sell planner.

    Expected formats (per line):
      - "Item name<TAB>Quantity"
      - "Item name   Quantity"
    Thousands separators in quantity (spaces or commas) are stripped.
    """
    entries: List[Tuple[str, int]] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        # Prefer tab-separated: "name<TAB>qty"
        parts = re.split(r"\t+", line)
        if len(parts) >= 2:
            name = parts[0].strip()
            qty_str = parts[1].strip()
        else:
            # Fallback: "name   qty"
            m = re.match(r"(.+?)\s+([\d ,]+)$", line)
            if not m:
                continue
            name = m.group(1).strip()
            qty_str = m.group(2)

        qty_str = qty_str.replace(" ", "").replace(",", "")
        try:
            qty = int(qty_str)
        except ValueError:
            continue

        if qty <= 0:
            continue

        entries.append((name, qty))

    return entries


def eve_round_price(price: Decimal) -> Decimal:
    """
    Approximate EVE market rounding:
    - Round to nearest 1 000 ISK.
    Example: 1 234 567 -> 1 235 000
    """
    if price <= 0:
        return Decimal("0")
    thousand = Decimal("1000")
    return (price / thousand).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * thousand


@router.get("/sell-planner", response_class=HTMLResponse)
async def sell_planner_form(request: Request) -> HTMLResponse:
    """
    Show empty sell planner form.
    """
    return request.app.state.templates.TemplateResponse(
        "sell_planner.html",
        {
            "request": request,
            "current_page": "sell_planner",
            "results": None,
            "summary": None,
            "raw_lines": "",
            "target_profit_percent": 10.0,
            "sales_tax_percent": 3.6,
            "ssc_surcharge_percent": 0.5,
            "broker_fee_percent": 1.0,
            "message": None,
        },
    )


@router.post("/sell-planner", response_class=HTMLResponse)
async def sell_planner_calculate(
    request: Request,
    raw_lines: str = Form(...),
    target_profit_percent: float = Form(10.0),
    sales_tax_percent: float = Form(3.6),
    ssc_surcharge_percent: float = Form(0.5),
    broker_fee_percent: float = Form(1.0),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """
    Take a list of items + quantities, look at current inventory lots (FIFO),
    compute cost basis and required sell price per item.
    """
    entries = parse_sell_planner_input(raw_lines)

    if not entries:
        return request.app.state.templates.TemplateResponse(
            "sell_planner.html",
            {
                "request": request,
                "current_page": "sell_planner",
                "results": [],
                "summary": None,
                "raw_lines": raw_lines,
                "target_profit_percent": target_profit_percent,
                "sales_tax_percent": sales_tax_percent,
                "ssc_surcharge_percent": ssc_surcharge_percent,
                "broker_fee_percent": broker_fee_percent,
                "message": "No valid lines found. Use 'Item name<TAB>Quantity' per line.",
            },
        )

    # Percentages -> decimals
    fee_rate = Decimal(
        str((sales_tax_percent + ssc_surcharge_percent + broker_fee_percent) / 100.0)
    )
    profit_margin = Decimal(str(target_profit_percent / 100.0))

    results: List[Dict[str, Any]] = []
    total_cost_all = Decimal("0")
    total_sell_value_all = Decimal("0")
    total_profit_all = Decimal("0")
    total_qty_all = 0

    for name, requested_qty in entries:
        # Find the item by name
        stmt_item = select(Item).where(Item.name == name)
        res_item = await db.execute(stmt_item)
        item: Optional[Item] = res_item.scalar_one_or_none()

        if not item:
            results.append(
                {
                    "item_name": name,
                    "requested_qty": requested_qty,
                    "available_qty": 0,
                    "covered_qty": 0,
                    "avg_cost_per_unit": None,
                    "total_cost": None,
                    "required_unit_price": None,
                    "required_total_price": None,
                    "profit_isk": None,
                    "note": "Item not found in DB (name mismatch?)",
                }
            )
            continue

        # Fetch remaining lots for this item (FIFO by id)
        stmt_lots = (
            select(InventoryLot)
            .where(
                InventoryLot.item_id == item.id,
                InventoryLot.quantity_remaining > 0,
            )
            .order_by(InventoryLot.id.asc())
        )
        res_lots = await db.execute(stmt_lots)
        lots = list(res_lots.scalars().all())

        available_qty = sum(int(l.quantity_remaining) for l in lots)
        remaining_to_cover = requested_qty
        total_cost = Decimal("0")
        total_qty = 0

        for lot in lots:
            if remaining_to_cover <= 0:
                break
            lot_remaining = int(lot.quantity_remaining)
            if lot_remaining <= 0:
                continue

            take = min(remaining_to_cover, lot_remaining)
            if take <= 0:
                continue

            # unit_total_cost should already include shipping if you added it at import
            unit_total_cost = Decimal(str(lot.unit_total_cost))
            total_cost += unit_total_cost * take
            total_qty += take
            remaining_to_cover -= take

        if total_qty == 0:
            results.append(
                {
                    "item_name": item.name,
                    "requested_qty": requested_qty,
                    "available_qty": available_qty,
                    "covered_qty": 0,
                    "avg_cost_per_unit": None,
                    "total_cost": None,
                    "required_unit_price": None,
                    "required_total_price": None,
                    "profit_isk": None,
                    "note": "No remaining quantity in inventory",
                }
            )
            continue

        avg_cost_unit = (total_cost / total_qty).quantize(Decimal("0.01"))

        # Required sell price formula:
        # Net revenue after fees = cost * (1 + profit_margin)
        # Net = (1 - fee_rate) * SellPrice
        # => SellPrice = cost*(1+profit) / (1-fees)
        if fee_rate >= Decimal("1"):
            required_unit_raw = avg_cost_unit * (Decimal("1") + profit_margin)
        else:
            required_unit_raw = avg_cost_unit * (Decimal("1") + profit_margin) / (
                Decimal("1") - fee_rate
            )

        required_unit_price = eve_round_price(required_unit_raw)
        required_total_price = required_unit_price * total_qty

        net_revenue = required_total_price * (Decimal("1") - fee_rate)
        profit_isk = net_revenue - total_cost

        total_cost_all += total_cost
        total_sell_value_all += required_total_price
        total_profit_all += profit_isk
        total_qty_all += total_qty

        results.append(
            {
                "item_name": item.name,
                "requested_qty": requested_qty,
                "available_qty": available_qty,
                "covered_qty": total_qty,
                "avg_cost_per_unit": avg_cost_unit,
                "total_cost": total_cost,
                "required_unit_price": required_unit_price,
                "required_total_price": required_total_price,
                "profit_isk": profit_isk,
                "note": None
                if remaining_to_cover <= 0
                else f"Short by {remaining_to_cover} units",
            }
        )

    summary = {
        "total_cost": total_cost_all,
        "total_sell_value": total_sell_value_all,
        "total_profit": total_profit_all,
        "total_qty": total_qty_all,
    }

    return request.app.state.templates.TemplateResponse(
        "sell_planner.html",
        {
            "request": request,
            "current_page": "sell_planner",
            "results": results,
            "summary": summary,
            "raw_lines": raw_lines,
            "target_profit_percent": target_profit_percent,
            "sales_tax_percent": sales_tax_percent,
            "ssc_surcharge_percent": ssc_surcharge_percent,
            "broker_fee_percent": broker_fee_percent,
            "message": None,
        },
    )

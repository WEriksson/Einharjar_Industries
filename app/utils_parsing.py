import re
from typing import List, Dict
from datetime import datetime, timezone


def parse_isk_field(text: str) -> float:
    if text is None:
        return 0.0
    text = text.replace("ISK", "").strip()
    text = text.replace("\u00A0", "").replace(" ", "")
    if not text:
        return 0.0
    return float(text)

def parse_transactions(raw: str) -> List[dict]:
    """
    Parse pasted market transaction lines into a list of dicts:
    {
        "item_name": str,
        "qty": int,
        "total_cost": float (positive ISK),
        "time": datetime (EVE/UTC time from wallet line)
    }
    """
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue

        # parts[0] is like "2025.11.16 11:51" (EVE / UTC)
        time_str = parts[0].strip()
        try:
            dt = datetime.strptime(time_str, "%Y.%m.%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            # If it ever fails, fall back to "now" in UTC
            dt = datetime.now(timezone.utc)

        try:
            raw_qty = parts[1].strip()
            raw_qty = raw_qty.replace("\u00A0", "").replace(" ", "")
            qty = int(raw_qty)
        except ValueError:
            continue

        item_name = parts[2].strip()
        total_str = parts[4]
        total_isk = parse_isk_field(total_str)
        total_cost = abs(total_isk)

        rows.append(
            {
                "item_name": item_name,
                "qty": qty,
                "total_cost": total_cost,
                "time": dt,  # IMPORTANT
            }
        )

    return rows

def aggregate_by_item(transactions: List[dict]) -> Dict[str, dict]:
    """
    Aggregate parsed transactions by item name.
    Returns {item_name: {"qty": int, "total_cost": float}}.
    """
    agg: Dict[str, dict] = {}
    for t in transactions:
        name = t["item_name"]
        if name not in agg:
            agg[name] = {"qty": 0, "total_cost": 0.0}
        agg[name]["qty"] += t["qty"]
        agg[name]["total_cost"] += t["total_cost"]
    return agg

def parse_janice_rows(raw: str) -> List[dict]:
    """
    Parse Janice output rows like:
    Chromium    9641    0.05    6479.04    6630.00

    Returns a list of dicts:
    {
        "item_name": str,
        "qty": int,
        "unit_volume": float,
        "jita_buy": float,
        "jita_sell": float,
    }
    """
    rows: List[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        # Try tab-separated first
        parts = line.split("\t")
        if len(parts) < 5:
            # Fallback: split on 2+ spaces
            parts = re.split(r"\s{2,}", line)

        if len(parts) < 5:
            continue

        name = parts[0].strip()
        if not name:
            continue

        try:
            raw_qty = parts[1].strip()
            raw_qty = raw_qty.replace("\u00A0", "").replace(" ", "")
            qty = int(raw_qty)

            unit_vol = float(parts[2].replace("\u00A0", "").replace(" ", ""))
            jita_buy = float(parts[3].replace("\u00A0", "").replace(" ", ""))
            jita_sell = float(parts[4].replace("\u00A0", "").replace(" ", ""))
        except ValueError:
            continue

        rows.append(
            {
                "item_name": name,
                "qty": qty,
                "unit_volume": unit_vol,
                "jita_buy": jita_buy,
                "jita_sell": jita_sell,
            }
        )

    return rows

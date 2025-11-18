from decimal import Decimal, InvalidOperation

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from .imports import router as imports_router, init_db as init_db_imports
from .inventory import router as inventory_router
from .log import router as log_router
from .exports import router as exports_router
from .settings import router as settings_router
from .auth import router as auth_router
from .wallet_sync import router as wallet_router
from .sell_planner import router as sell_planner_router
from .fits import router as fits_router
from .market_scan import router as market_scan_router




app = FastAPI(title="EVE Market Tool v2")


def format_isk(value, decimals=2):
  """Format ISK amounts with spaces as thousands separators."""
  if value is None:
    value = Decimal(0)

  try:
    number = Decimal(str(value))
  except (InvalidOperation, TypeError, ValueError):
    return str(value)

  try:
    decimals_int = max(0, int(decimals))
  except (TypeError, ValueError):
    decimals_int = 2

  formatted = f"{number:,.{decimals_int}f}"
  return formatted.replace(",", " ")


templates = Jinja2Templates(directory="templates")
templates.env.filters["isk"] = format_isk
app.state.templates = templates

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(imports_router, tags=["imports"])
app.include_router(exports_router, tags=["exports"])
app.include_router(inventory_router, tags=["inventory"])
app.include_router(log_router, tags=["log"])
app.include_router(settings_router, tags=["settings"])
app.include_router(auth_router, tags=["auth"])
app.include_router(wallet_router, tags=["wallet"])
app.include_router(sell_planner_router, tags=["sell_planner"])
app.include_router(fits_router, tags=["fits"])
app.include_router(market_scan_router, tags=["market_scans"])




@app.on_event("startup")
async def on_startup():
  await init_db_imports()

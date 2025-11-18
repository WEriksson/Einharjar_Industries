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



app = FastAPI(title="EVE Market Tool v2")

templates = Jinja2Templates(directory="templates")
app.state.templates = templates

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(imports_router, tags=["imports"])
app.include_router(exports_router, tags=["exports"])
app.include_router(inventory_router, tags=["inventory"])
app.include_router(log_router, tags=["log"])
app.include_router(settings_router, tags=["settings"])
app.include_router(auth_router, tags=["auth"])
app.include_router(wallet_router, tags=["wallet"])




@app.on_event("startup")
async def on_startup():
  await init_db_imports()

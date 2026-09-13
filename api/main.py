"""FastAPI 入口：日志配置 + 路由 + 静态前端 + 启动时恢复上次 Vault。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.routes import APP_VERSION, router, vault_manager
from config.settings import settings
from core import llm
from core.indexer.auto_wiki_ai import WikiValidationError
from core.log import configure_logging, log_event
from core.vault.raw_guardian import RawGuardError
from core.vault.vault_manager import VaultError

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log_event("APP_START", version=APP_VERSION)
    # 记住上次打开的 Vault（不硬编码路径；来自用户配置）
    last = settings.get("VAULT_PATH")
    if last:
        try:
            vault_manager.open(last)
        except VaultError as e:
            log_event("VAULT_REOPEN_FAIL", path=last, error=str(e))
    yield


app = FastAPI(title="LLMwiki 智能伴学系统", version=APP_VERSION, lifespan=lifespan)
app.include_router(router)


@app.exception_handler(VaultError)
async def vault_error_handler(_: Request, exc: VaultError):
    return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})


@app.exception_handler(llm.LLMNotConfigured)
async def llm_not_configured_handler(_: Request, exc: llm.LLMNotConfigured):
    return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})


@app.exception_handler(RawGuardError)
async def raw_guard_handler(_: Request, exc: RawGuardError):
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": str(exc), "kind": "RAW_GUARD"},
    )


@app.exception_handler(WikiValidationError)
async def wiki_validation_handler(_: Request, exc: WikiValidationError):
    return JSONResponse(
        status_code=400,
        content={"success": False, "error": str(exc), "warnings": exc.warnings},
    )


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

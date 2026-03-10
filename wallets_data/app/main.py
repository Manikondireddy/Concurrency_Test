import logging
import time
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import inspect, text
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import config
from .database import Base, engine
from .logging_utils import RequestIdFilter, request_id_var
from .routes.auth_routes import router as auth_router
from .routes.auth_routes import users_router
from .routes.wallet_routes import router as wallet_router

logger = logging.getLogger(__name__)

# Configure a dedicated logger for application code without overriding uvicorn's logging config.
_app_logger = logging.getLogger("wallets_data")
_app_logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
_app_logger.propagate = False
if not _app_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] request_id=%(request_id)s %(message)s")
    )
    _handler.addFilter(RequestIdFilter())
    _app_logger.addHandler(_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        if config.AUTO_CREATE_TABLES:
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Database metadata ensured (AUTO_CREATE_TABLES enabled)")
        else:
            logger.info("AUTO_CREATE_TABLES disabled; expecting schema managed via migrations")

        def _schema_check(sync_conn) -> None:
            inspector = inspect(sync_conn)
            if not inspector.has_table("wallets"):
                return
            columns = {col["name"] for col in inspector.get_columns("wallets")}
            if "version" not in columns:
                message = (
                    "Schema mismatch: wallets.version missing; run Alembic migrations "
                    "(alembic -c wallets_data/alembic.ini upgrade head)"
                )
                if config.IS_PRODUCTION:
                    raise RuntimeError(message)
                _app_logger.warning(message)
                if config.AUTO_CREATE_TABLES:
                    # Development convenience: bring an existing dev DB up to date when using create_all.
                    sync_conn.execute(text("ALTER TABLE wallets ADD COLUMN version INTEGER NOT NULL DEFAULT 0"))
                    _app_logger.warning("Applied dev schema fix: added wallets.version column")

        await conn.run_sync(_schema_check)
    logger.info("Wallet Ledger service starting")

    yield

    await engine.dispose()
    logger.info("Wallet Ledger service stopped")


app = FastAPI(title="Wallet Ledger System", version="1.0.0", lifespan=lifespan)

if config.CORS_ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ALLOW_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

if config.ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = uuid.uuid4().hex[:12]
    token = request_id_var.set(request_id)
    start = time.perf_counter()

    try:
        response = await call_next(request)

        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "Request completed request_id=%s method=%s path=%s status_code=%s duration_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response
    except Exception:
        duration_ms = (time.perf_counter() - start) * 1000
        logger.exception(
            "Unhandled error request_id=%s method=%s path=%s duration_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            duration_ms,
        )
        error_response = JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
        )
        error_response.headers["X-Request-ID"] = request_id
        return error_response
    finally:
        request_id_var.reset(token)


@app.get("/health/live", status_code=status.HTTP_200_OK)
async def live_health():
    return {"status": "ok"}


@app.get("/health/ready", status_code=status.HTTP_200_OK)
async def ready_health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Avoid noisy 404s in logs when browsing in a browser.
    return JSONResponse(status_code=status.HTTP_204_NO_CONTENT, content=None)


app.include_router(wallet_router)
app.include_router(auth_router)
app.include_router(users_router)

from contextlib import asynccontextmanager

from app.api.api_router import api_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.rate_limit import limiter, setup_rate_limiting
from app.db.session import AsyncSessionLocal, engine
from app.models.auth import Role
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import func, select
from starlette.middleware.cors import CORSMiddleware

# Setup logging
setup_logging()

IS_PRODUCTION = settings.ENVIRONMENT == "PRODUCTION"
CORS_ORIGINS = settings.CORS_ORIGINS

if "*" in CORS_ORIGINS:
    # Browsers refuse "*" together with credentials, and it would let any website call the API as the user.
    raise RuntimeError("CORS_ORIGINS must list explicit origins, not '*'")
if IS_PRODUCTION and any("localhost" in o or "127.0.0.1" in o for o in CORS_ORIGINS):
    logger.warning(
        f"CORS_ORIGINS contains a localhost origin in production: {CORS_ORIGINS}"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application starting up...")
    try:
        async with AsyncSessionLocal() as session:
            system_roles = (
                await session.execute(
                    select(func.count(Role.id)).where(Role.church_id.is_(None))
                )
            ).scalar() or 0
        if system_roles == 0:
            logger.warning(
                "No system roles found: nobody can be authorised. Run `python -m scripts.run` once."
            )
    except (
        Exception
    ) as exc:  # the app still starts; /health/ready reports the database state
        logger.error(f"Database check at startup failed: {exc}")
    yield
    await engine.dispose()
    logger.info("Application shut down")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=None if IS_PRODUCTION else f"{settings.API_V1_STR}/openapi.json",
    docs_url=(
        None if IS_PRODUCTION else "/docs"
    ),  # don't publish the API map in production
    redoc_url=None if IS_PRODUCTION else "/redoc",
    lifespan=lifespan,
)

# Order matters: the middleware added LAST is the outermost. Rate limiting is added first and CORS
# after it, so a 429 response still carries CORS headers (otherwise the browser reports a CORS error).
setup_rate_limiting(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],  # includes Authorization and X-Church-Id
    expose_headers=[
        "X-Total-Count"
    ],  # list endpoints return the total here; browsers hide it otherwise
)

app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/health")
@limiter.exempt  # load balancers poll this; it must never be rate limited
async def health_check():
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/health/ready")
@limiter.exempt
async def readiness_check():
    """Readiness: the database answers."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(select(1))
    except Exception as exc:
        logger.error(f"Readiness check failed: {exc}")
        return JSONResponse(
            status_code=503, content={"status": "unavailable", "database": "down"}
        )
    return {"status": "ok", "database": "up"}

import sys
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from loguru import logger

from app.core.config import settings
from app.api.api_router import api_router
from app.core.logging import setup_logging
from app.core.rate_limit import setup_rate_limiting

# Setup structured logging
setup_logging()

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# Setup rate limiting
setup_rate_limiting(app)

# Set all CORS enabled origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"], # Update this in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_STR)

@app.on_event("startup")
async def startup_event():
    logger.info("Application starting up...")

@app.get("/health")
def health_check():
    return {"status": "ok"}

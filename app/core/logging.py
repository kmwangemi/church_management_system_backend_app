import sys
from loguru import logger
from app.core.config import settings

def setup_logging():
    # Remove default handler
    logger.remove()
    # Add console handler
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="DEBUG" if settings.ENVIRONMENT == "DEVELOPMENT" else "INFO",
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )
    # Add file handler for errors
    logger.add(
        "logs/error.log",
        rotation="10 MB",
        retention="10 days",
        level="ERROR",
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )
    logger.info("Structured logging initialized")

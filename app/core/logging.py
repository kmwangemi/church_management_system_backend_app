import sys

from app.core.config import settings
from loguru import logger


def setup_logging():
    is_dev = settings.ENVIRONMENT == "DEVELOPMENT"
    # Remove default handler
    logger.remove()
    # Add console handler
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="DEBUG" if is_dev else "INFO",
        enqueue=True,
        backtrace=True,
        # diagnose prints variable VALUES in tracebacks (passwords, tokens, member data).
        # Development only.
        diagnose=is_dev,
    )
    # Add file handler for errors (persisted on disk, so never include variable values)
    logger.add(
        "logs/error.log",
        rotation="10 MB",
        retention="10 days",
        level="ERROR",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )
    logger.info("Logging initialized")

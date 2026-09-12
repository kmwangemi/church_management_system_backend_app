from typing import Any, Dict, Optional

from loguru import logger

from app.db.session import AsyncSessionLocal
from app.models.audit import AuditLog


async def log_action(
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    user_id: Optional[str] = None,
    church_id: Optional[str] = None,
    metadata_json: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
):
    """
    Logs an action asynchronously into the database.
    Designed to be used with FastAPI BackgroundTasks.
    """
    try:
        async with AsyncSessionLocal() as session:
            audit = AuditLog(
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                user_id=user_id,
                church_id=church_id,
                metadata_json=metadata_json,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            session.add(audit)
            await session.commit()
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")

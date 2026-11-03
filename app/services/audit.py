from typing import Any, Dict, Optional

from loguru import logger

from app.db.session import AsyncSessionLocal
from app.models.audit import AuditLog


async def log_action(
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    church_id: Optional[str] = None,
    metadata_json: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    old_value: Optional[Dict[str, Any]] = None,
    new_value: Optional[Dict[str, Any]] = None,
    *,
    # Deprecated names from the old signature. Still accepted so existing callers
    # (including positional ones, since the first eight parameters keep their
    # original order) keep working. Migrate callers to the new names over time.
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    user_id: Optional[str] = None,
):
    """
    Logs an action asynchronously into the database.
    Designed to be used with FastAPI BackgroundTasks.

    `actor_user_id` is the *User* (login) who did it, not a Member id.
    For updates pass `old_value` / `new_value` so the change can be reviewed later.
    """
    try:
        async with AsyncSessionLocal() as session:
            audit = AuditLog(
                action=action,
                entity_type=entity_type or resource_type,
                entity_id=entity_id or resource_id,
                actor_user_id=actor_user_id or user_id,
                church_id=church_id,
                old_value=old_value,
                new_value=new_value,
                metadata_json=metadata_json,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            session.add(audit)
            await session.commit()
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")


def queue_audit(
    background_tasks,
    request,
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    church_id: Optional[str] = None,
    old_value: Optional[Dict[str, Any]] = None,
    new_value: Optional[Dict[str, Any]] = None,
    metadata_json: Optional[Dict[str, Any]] = None,
) -> None:
    """Schedules log_action with the request's IP / user agent. Never put PII in the values."""
    background_tasks.add_task(
        log_action,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_user_id=actor_user_id,
        church_id=church_id,
        old_value=old_value,
        new_value=new_value,
        metadata_json=metadata_json,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
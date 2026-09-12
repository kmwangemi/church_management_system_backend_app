from typing import List

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.group import Group
from app.schemas.group import GroupCreate, GroupResponse
from app.services.audit import log_action

router = APIRouter()


@router.get("/", response_model=List[GroupResponse])
async def get_groups(current_user: CurrentUser, session: DbSession):
    """
    Get all groups.
    """
    stmt = select(Group)
    result = await session.execute(stmt)
    return result.scalars().all()


@router.post("/", response_model=GroupResponse)
async def create_group(
    request: Request,
    background_tasks: BackgroundTasks,
    group_in: GroupCreate,
    current_user: CurrentUser,
    session: DbSession,
):
    """
    Create new group.
    """
    new_group = Group(**group_in.dict())
    session.add(new_group)
    await session.commit()
    await session.refresh(new_group)
    background_tasks.add_task(
        log_action,
        action="CREATE_GROUP",
        resource_type="GROUP",
        resource_id=new_group.id,
        user_id=current_user.id,
        church_id=new_group.church_id,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
    )
    return new_group


@router.delete("/{group_id}")
async def delete_group(
    group_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    """
    Delete group.
    """
    stmt = select(Group).where(Group.id == group_id)
    result = await session.execute(stmt)
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    org_id = group.church_id
    await session.delete(group)
    await session.commit()
    background_tasks.add_task(
        log_action,
        action="DELETE_GROUP",
        resource_type="GROUP",
        resource_id=group_id,
        user_id=current_user.id,
        church_id=org_id,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
    )
    return {"status": "success", "message": "Group deleted"}

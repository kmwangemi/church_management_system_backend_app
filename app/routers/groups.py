from typing import List, Optional

from app.models.church import Branch
from app.models.group import Department, Group, GroupMember
from app.models.member import Member
from app.schemas.group import GroupCreate, GroupResponse
from app.services.audit import queue_audit
from app.core.deps import CurrentUser, DbSession, church_id_from, require, resolve_actor
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from sqlalchemy import delete, func, select

router = APIRouter()


@router.get("/", response_model=List[GroupResponse])
async def get_groups(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
    branch_id: Optional[str] = Query(None),
    department_id: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Groups of ONE church (any active member can browse them). Total in X-Total-Count."""
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Group.church_id == actor.church_id]
    for column, value in (
        (Group.branch_id, branch_id),
        (Group.department_id, department_id),
        (Group.is_active, is_active),
    ):
        if value is not None:
            conds.append(column == value)
    response.headers["X-Total-Count"] = str(
        (await session.execute(select(func.count(Group.id)).where(*conds))).scalar()
        or 0
    )
    rows = await session.execute(
        select(Group)
        .where(*conds)
        .order_by(Group.name, Group.id)
        .limit(limit)
        .offset(offset)
    )
    return rows.scalars().all()


@router.post("/", response_model=GroupResponse)
async def create_group(
    request: Request,
    background_tasks: BackgroundTasks,
    group_in: GroupCreate,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, group_in.church_id)
    )
    church_id = actor.church_id
    await require(session, actor, "group.create", group_in.branch_id)
    if (
        group_in.branch_id
        and not (
            await session.execute(
                select(Branch.id).where(
                    Branch.id == group_in.branch_id,
                    Branch.church_id == church_id,
                    Branch.deleted_at.is_(None),
                )
            )
        ).first()
    ):
        raise HTTPException(status_code=404, detail="Branch not found")
    if (
        group_in.department_id
        and not (
            await session.execute(
                select(Department.id).where(
                    Department.id == group_in.department_id,
                    Department.church_id == church_id,
                )
            )
        ).first()
    ):
        raise HTTPException(status_code=404, detail="Department not found")
    if (
        group_in.leader_member_id
        and not (
            await session.execute(
                select(Member.id).where(
                    Member.id == group_in.leader_member_id,
                    Member.church_id == church_id,
                    Member.deleted_at.is_(None),
                )
            )
        ).first()
    ):
        raise HTTPException(status_code=404, detail="Leader not found in this church")
    group = Group(**{**group_in.model_dump(), "church_id": church_id})
    session.add(group)
    await session.commit()
    await session.refresh(group)
    queue_audit(
        background_tasks,
        request,
        "CREATE_GROUP",
        "GROUP",
        group.id,
        actor_user_id=current_user.id,
        church_id=church_id,
    )
    return group


@router.delete("/{group_id}")
async def delete_group(
    group_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    group = (
        await session.execute(select(Group).where(Group.id == group_id))
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    actor = await resolve_actor(session, current_user, group.church_id)
    await require(session, actor, "group.delete", group.branch_id)
    church_id = group.church_id
    # Explicit child delete (an ORM delete would try to NULL group_member.group_id and fail).
    await session.execute(delete(GroupMember).where(GroupMember.group_id == group.id))
    await session.execute(delete(Group).where(Group.id == group.id))
    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "DELETE_GROUP",
        "GROUP",
        group_id,
        actor_user_id=current_user.id,
        church_id=church_id,
    )
    return {"status": "success", "message": "Group deleted"}

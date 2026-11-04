from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional

from app.core.deps import CurrentUser, DbSession, church_id_from, require, resolve_actor
from app.models.church import Branch
from app.models.group import Department, Group, GroupMember
from app.models.member import Member
from app.models.user import User
from app.schemas.group import GroupCreate, GroupResponse
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


async def _format_group(session: DbSession, group: Group) -> Dict[str, Any]:
    leader_info = None
    if group.leader_member_id:
        lm = (
            await session.execute(
                select(Member, User)
                .outerjoin(User, User.id == Member.user_id)
                .where(Member.id == group.leader_member_id)
            )
        ).first()
        if lm:
            m, u = lm
            first_name = u.first_name if u else ""
            last_name = u.last_name if u else ""
            leader_info = {
                "_id": str(m.id),
                "id": str(m.id),
                "firstName": first_name,
                "lastName": last_name,
                "email": u.email if u else "",
                "membershipNumber": m.membership_number or "",
            }

    member_count = (
        await session.execute(
            select(func.count(GroupMember.id)).where(GroupMember.group_id == group.id)
        )
    ).scalar() or 0

    return {
        "_id": str(group.id),
        "id": str(group.id),
        "groupName": group.name,
        "name": group.name,
        "description": group.description or "",
        "isActive": group.is_active,
        "is_active": group.is_active,
        "branchId": group.branch_id,
        "departmentId": group.department_id,
        "leaderId": leader_info,
        "leader": leader_info,
        "capacity": 20,
        "memberCount": member_count,
        "members": member_count,
        "location": "Church Facility",
        "category": "Small Group",
        "meetingDay": ["Wednesday"],
        "meetingTime": ["06:00 PM"],
        "createdAt": _iso(group.created_at),
        "updatedAt": _iso(group.updated_at),
    }


class GroupPayload(BaseModel):
    groupName: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    leaderId: Optional[str] = None
    leader_member_id: Optional[str] = None
    branchId: Optional[str] = None
    branch_id: Optional[str] = None
    departmentId: Optional[str] = None
    department_id: Optional[str] = None
    category: Optional[str] = None
    capacity: Optional[Any] = None
    location: Optional[str] = None
    establishedDate: Optional[str] = None
    meetingDay: Optional[List[str]] = None
    meetingTime: Optional[List[str]] = None
    isActive: Optional[bool] = True


@router.get("")
@router.get("/")
async def get_groups(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
    branch_id: Optional[str] = Query(None),
    department_id: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    search: str = Query("", max_length=100),
    offset: Optional[int] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Group.church_id == actor.church_id]
    if branch_id:
        conds.append(Group.branch_id == branch_id)
    if department_id:
        conds.append(Group.department_id == department_id)
    if is_active is not None:
        conds.append(Group.is_active == is_active)
    if search:
        s = f"%{search.strip()}%"
        conds.append(or_(Group.name.ilike(s), Group.description.ilike(s)))

    total = (
        await session.execute(select(func.count(Group.id)).where(*conds))
    ).scalar() or 0

    if offset is None:
        offset = (page - 1) * limit

    rows = (
        (
            await session.execute(
                select(Group)
                .where(*conds)
                .order_by(Group.name, Group.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    formatted_groups = [await _format_group(session, g) for g in rows]
    pages = math.ceil(total / limit) if limit else 1
    response.headers["X-Total-Count"] = str(total)

    # Return structure matching frontend GroupListResponse: { groups: [...], pagination: { ... } }
    return {
        "groups": formatted_groups,
        "data": formatted_groups,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": pages,
            "hasNext": page < pages,
            "hasPrev": page > 1,
        },
    }


@router.post("")
@router.post("/")
async def create_group(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: GroupPayload,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    name = (payload.groupName or payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")

    leader_id = payload.leaderId or payload.leader_member_id
    if leader_id:
        leader_exists = (
            await session.execute(
                select(Member.id).where(
                    Member.id == leader_id, Member.church_id == actor.church_id
                )
            )
        ).scalar_one_or_none()
        if not leader_exists:
            leader_id = None

    branch_id = payload.branchId or payload.branch_id
    if branch_id:
        branch_exists = (
            await session.execute(
                select(Branch.id).where(
                    Branch.id == branch_id, Branch.church_id == actor.church_id
                )
            )
        ).scalar_one_or_none()
        if not branch_exists:
            branch_id = None

    dept_id = payload.departmentId or payload.department_id

    group = Group(
        name=name,
        church_id=actor.church_id,
        branch_id=branch_id,
        department_id=dept_id,
        leader_member_id=leader_id,
        description=payload.description or "",
        is_active=payload.isActive if payload.isActive is not None else True,
    )
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
        church_id=actor.church_id,
    )

    formatted = await _format_group(session, group)
    return {
        "status": "success",
        "message": "Group created successfully",
        "group": formatted,
        "data": formatted,
    }


@router.get("/{group_id}")
async def get_group(
    group_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(
                Group.id == group_id, Group.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    formatted = await _format_group(session, group)
    return {
        "group": formatted,
        "data": {"group": formatted},
    }


@router.put("/{group_id}")
async def update_group(
    group_id: str,
    payload: GroupPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(
                Group.id == group_id, Group.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    name = payload.groupName or payload.name
    if name is not None:
        group.name = name.strip()
    if payload.description is not None:
        group.description = payload.description
    if payload.leaderId is not None or payload.leader_member_id is not None:
        group.leader_member_id = payload.leaderId or payload.leader_member_id or None
    if payload.isActive is not None:
        group.is_active = payload.isActive

    group.updated_at = _now()
    await session.commit()
    await session.refresh(group)

    queue_audit(
        background_tasks,
        request,
        "UPDATE_GROUP",
        "GROUP",
        group.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    formatted = await _format_group(session, group)
    return {
        "status": "success",
        "message": "Group updated successfully",
        "group": formatted,
        "data": formatted,
    }


@router.delete("/{group_id}")
async def delete_group(
    group_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    force: bool = Query(False),
):
    group = (
        await session.execute(select(Group).where(Group.id == group_id))
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    actor = await resolve_actor(session, current_user, group.church_id)
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
        church_id=actor.church_id,
    )

    return {"status": "success", "message": "Group deleted successfully"}

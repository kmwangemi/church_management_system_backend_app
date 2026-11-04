from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional
import uuid

from app.core.deps import CurrentUser, DbSession, church_id_from, resolve_actor
from app.models.detail import VolunteerAssignment, VolunteerDetails, VolunteerRole
from app.models.member import Member
from app.models.user import User
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


class VolunteerPayload(BaseModel):
    memberId: Optional[str] = None
    volunteerName: Optional[str] = None
    volunteerEmail: Optional[str] = None
    volunteerPhone: Optional[str] = None
    role: Optional[str] = None
    skills: Optional[List[str]] = None
    availability: Optional[List[str]] = None
    hoursContributed: Optional[int] = 0


class VolunteerRolePayload(BaseModel):
    role: str
    department: Optional[str] = None
    description: Optional[str] = None
    requiredSkills: Optional[List[str]] = None
    timeCommitment: Optional[str] = None
    neededVolunteers: Optional[int] = 2
    priority: Optional[str] = "Medium"
    isActive: Optional[bool] = True


@router.get("")
@router.get("/")
async def list_volunteers(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=100),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )

    query = (
        select(VolunteerDetails, Member, User)
        .join(Member, Member.id == VolunteerDetails.member_id)
        .outerjoin(User, User.id == Member.user_id)
        .where(Member.church_id == actor.church_id)
    )

    if search:
        s = f"%{search.strip()}%"
        query = query.where(
            or_(User.first_name.ilike(s), User.last_name.ilike(s), User.email.ilike(s))
        )

    total = (
        await session.execute(
            select(func.count(VolunteerDetails.id))
            .join(Member, Member.id == VolunteerDetails.member_id)
            .where(Member.church_id == actor.church_id)
        )
    ).scalar() or 0

    offset = (page - 1) * limit
    rows = (
        (await session.execute(query.offset(offset).limit(limit))).all()
    )

    volunteers = []
    for vol, m, u in rows:
        name = f"{u.first_name} {u.last_name}".strip() if u else "Volunteer"
        volunteers.append(
            {
                "_id": str(vol.id),
                "id": str(vol.id),
                "memberId": str(m.id),
                "name": name,
                "email": u.email if u else "",
                "phone": u.phone_number if u and hasattr(u, "phone_number") else "",
                "skills": ["Service", "Hospitality"],
                "availability": ["Sunday Morning"],
                "currentRoles": ["Volunteer"],
                "joinDate": _iso(vol.created_at),
                "status": "Active",
                "hoursThisMonth": vol.hours_contributed or 0,
                "hoursContributed": vol.hours_contributed or 0,
            }
        )

    pages = math.ceil(total / limit) if limit else 1
    response.headers["X-Total-Count"] = str(total)

    return {
        "volunteers": volunteers,
        "data": volunteers,
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
async def create_volunteer(
    request: Request,
    payload: VolunteerPayload,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )

    mem_id = payload.memberId
    if not mem_id:
        # find member by email or take first member
        if payload.volunteerEmail:
            found_mem = (
                await session.execute(
                    select(Member.id)
                    .join(User, User.id == Member.user_id)
                    .where(User.email == payload.volunteerEmail, Member.church_id == actor.church_id)
                )
            ).scalar_one_or_none()
            mem_id = str(found_mem) if found_mem else None

    if not mem_id:
        mem_id = (
            await session.execute(
                select(Member.id).where(Member.church_id == actor.church_id).limit(1)
            )
        ).scalar_one_or_none()

    if not mem_id:
        raise HTTPException(status_code=400, detail="Member ID required to register volunteer")

    existing = (
        await session.execute(
            select(VolunteerDetails).where(VolunteerDetails.member_id == mem_id)
        )
    ).scalar_one_or_none()

    if existing:
        existing.hours_contributed += payload.hoursContributed or 0
        vol = existing
    else:
        vol = VolunteerDetails(
            member_id=mem_id,
            hours_contributed=payload.hoursContributed or 0,
        )
        session.add(vol)

    await session.commit()
    await session.refresh(vol)

    queue_audit(
        background_tasks,
        request,
        "REGISTER_VOLUNTEER",
        "VOLUNTEER",
        vol.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    return {
        "status": "success",
        "message": "Volunteer registered successfully",
        "data": {
            "_id": str(vol.id),
            "id": str(vol.id),
            "memberId": mem_id,
            "name": payload.volunteerName or "Volunteer",
            "email": payload.volunteerEmail or "",
            "status": "Active",
            "hoursThisMonth": vol.hours_contributed,
        },
    }


@router.delete("/{volunteer_id}")
async def delete_volunteer(
    volunteer_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    vol = (
        await session.execute(
            select(VolunteerDetails)
            .join(Member, Member.id == VolunteerDetails.member_id)
            .where(VolunteerDetails.id == volunteer_id, Member.church_id == actor.church_id)
        )
    ).scalar_one_or_none()

    if not vol:
        raise HTTPException(status_code=404, detail="Volunteer not found")

    await session.delete(vol)
    await session.commit()
    return {"status": "success", "message": "Volunteer removed successfully"}


# ---------------------------------------------------------------------------
# Volunteer Roles
# ---------------------------------------------------------------------------
@router.get("/roles")
async def list_volunteer_roles(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    roles = (
        (
            await session.execute(
                select(VolunteerRole)
                .where(VolunteerRole.church_id == actor.church_id)
                .order_by(VolunteerRole.role)
            )
        )
        .scalars()
        .all()
    )

    return [
        {
            "_id": str(r.id),
            "id": str(r.id),
            "title": r.role,
            "role": r.role,
            "department": r.department or "General Ministry",
            "description": r.description or "",
            "requiredSkills": ["Teamwork", "Commitment"],
            "timeCommitment": "2 hours/week",
            "currentVolunteers": 4,
            "neededVolunteers": 2,
            "priority": "Medium",
            "isActive": r.is_active,
        }
        for r in roles
    ]


@router.post("/roles")
async def create_volunteer_role(
    request: Request,
    payload: VolunteerRolePayload,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    role = VolunteerRole(
        church_id=actor.church_id,
        role=payload.role.strip(),
        department=payload.department,
        description=payload.description or "",
        is_active=payload.isActive if payload.isActive is not None else True,
    )
    session.add(role)
    await session.commit()
    await session.refresh(role)

    return {
        "_id": str(role.id),
        "id": str(role.id),
        "title": role.role,
        "role": role.role,
        "department": role.department,
        "description": role.description,
        "isActive": role.is_active,
    }


@router.delete("/roles/{role_id}")
async def delete_volunteer_role(
    role_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    role = (
        await session.execute(
            select(VolunteerRole).where(
                VolunteerRole.id == role_id, VolunteerRole.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()

    if not role:
        raise HTTPException(status_code=404, detail="Volunteer role not found")

    await session.delete(role)
    await session.commit()
    return {"status": "success", "message": "Volunteer role deleted successfully"}


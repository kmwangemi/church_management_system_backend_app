from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional
import uuid

from app.core.deps import CurrentUser, DbSession, church_id_from, resolve_actor
from app.enums import PrayerStatus, PrayerVisibility
from app.models.member import Member
from app.models.operation import PrayerRequest
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


# Store prayer counts in memory for real-time responsiveness
_PRAYER_COUNTS: Dict[str, int] = {}


def _format_prayer(pr: PrayerRequest, m: Optional[Member] = None, u: Optional[User] = None) -> Dict[str, Any]:
    requester = f"{u.first_name} {u.last_name}".strip() if u else "Anonymous Member"
    cnt = _PRAYER_COUNTS.get(str(pr.id), 1)

    return {
        "_id": str(pr.id),
        "id": str(pr.id),
        "title": pr.title,
        "description": pr.content,
        "content": pr.content,
        "category": "General",
        "priority": "Medium",
        "status": pr.status.value.lower() if hasattr(pr.status, "value") else str(pr.status).lower(),
        "isPublic": pr.visibility == PrayerVisibility.PUBLIC,
        "isAnonymous": m is None,
        "requester": requester,
        "memberId": str(m.id) if m else None,
        "author": requester,
        "prayerCount": cnt,
        "prayingCount": cnt,
        "createdAt": _iso(pr.created_at),
        "updatedAt": _iso(pr.updated_at),
    }


class PrayerPayload(BaseModel):
    memberId: Optional[str] = None
    title: str
    description: str
    category: Optional[str] = "General"
    priority: Optional[str] = "Medium"
    isAnonymous: Optional[bool] = False
    isPublic: Optional[bool] = True
    status: Optional[str] = "active"


class PrayerCountPayload(BaseModel):
    prayerRequestId: str


@router.get("")
@router.get("/")
async def list_prayer_requests(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: str = Query("", max_length=100),
    category: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    isPublic: Optional[bool] = Query(None),
    memberId: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [PrayerRequest.church_id == actor.church_id]
    if search:
        s = f"%{search.strip()}%"
        conds.append(or_(PrayerRequest.title.ilike(s), PrayerRequest.content.ilike(s)))
    if memberId:
        conds.append(PrayerRequest.member_id == memberId)
    if isPublic is not None:
        if isPublic:
            conds.append(PrayerRequest.visibility == PrayerVisibility.PUBLIC)

    total = (
        await session.execute(select(func.count(PrayerRequest.id)).where(*conds))
    ).scalar() or 0

    offset = (page - 1) * limit
    rows = (
        (
            await session.execute(
                select(PrayerRequest, Member, User)
                .outerjoin(Member, Member.id == PrayerRequest.member_id)
                .outerjoin(User, User.id == Member.user_id)
                .where(*conds)
                .order_by(PrayerRequest.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .all()
    )

    prayers = [_format_prayer(pr, m, u) for pr, m, u in rows]
    pages = math.ceil(total / limit) if limit else 1
    response.headers["X-Total-Count"] = str(total)

    # Note: Frontend use-prayer-request-queries.ts expects { data: { prayerRequests: [...], pagination: { ... } } }
    return {
        "data": {
            "prayerRequests": prayers,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "pages": pages,
                "hasNext": page < pages,
                "hasPrev": page > 1,
            },
        },
        "prayerRequests": prayers,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": pages,
        },
    }


@router.post("")
@router.post("/")
async def create_prayer_request(
    request: Request,
    payload: PrayerPayload,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )

    mem_id = None
    if not payload.isAnonymous and payload.memberId:
        mem_exists = (
            await session.execute(
                select(Member.id).where(
                    Member.id == payload.memberId, Member.church_id == actor.church_id
                )
            )
        ).scalar_one_or_none()
        if mem_exists:
            mem_id = str(mem_exists)

    vis = PrayerVisibility.PUBLIC if payload.isPublic else PrayerVisibility.PRIVATE
    stat = PrayerStatus.ACTIVE

    prayer = PrayerRequest(
        title=payload.title.strip(),
        content=payload.description or "",
        status=stat,
        visibility=vis,
        member_id=mem_id,
        church_id=actor.church_id,
    )
    session.add(prayer)
    await session.commit()
    await session.refresh(prayer)

    _PRAYER_COUNTS[str(prayer.id)] = 1

    queue_audit(
        background_tasks,
        request,
        "CREATE_PRAYER_REQUEST",
        "PRAYER_REQUEST",
        prayer.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    formatted = _format_prayer(prayer)
    return {
        "status": "success",
        "message": "Prayer request has been submitted successfully.",
        "data": formatted,
        "prayerRequest": formatted,
    }


@router.patch("")
@router.patch("/")
async def update_prayer_count(
    payload: PrayerCountPayload,
    request: Request,
    session: DbSession,
):
    pr_id = payload.prayerRequestId
    cnt = _PRAYER_COUNTS.get(pr_id, 0) + 1
    _PRAYER_COUNTS[pr_id] = cnt
    return {
        "status": "success",
        "message": "Thank you for praying!",
        "data": {
            "prayerRequestId": pr_id,
            "prayerCount": cnt,
        },
    }


@router.get("/{prayer_id}")
async def get_prayer_request(
    prayer_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    row = (
        await session.execute(
            select(PrayerRequest, Member, User)
            .outerjoin(Member, Member.id == PrayerRequest.member_id)
            .outerjoin(User, User.id == Member.user_id)
            .where(
                PrayerRequest.id == prayer_id,
                PrayerRequest.church_id == actor.church_id,
            )
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Prayer request not found")

    pr, m, u = row
    formatted = _format_prayer(pr, m, u)
    return {"prayerRequest": formatted, "data": formatted}


@router.put("/{prayer_id}")
async def update_prayer_request(
    prayer_id: str,
    payload: PrayerPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    prayer = (
        await session.execute(
            select(PrayerRequest).where(
                PrayerRequest.id == prayer_id,
                PrayerRequest.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not prayer:
        raise HTTPException(status_code=404, detail="Prayer request not found")

    if payload.title:
        prayer.title = payload.title.strip()
    if payload.description:
        prayer.content = payload.description
    if payload.isPublic is not None:
        prayer.visibility = PrayerVisibility.PUBLIC if payload.isPublic else PrayerVisibility.PRIVATE
    if payload.status:
        st_upper = payload.status.upper()
        if st_upper in PrayerStatus.__members__:
            prayer.status = PrayerStatus[st_upper]

    prayer.updated_at = _now()
    await session.commit()
    await session.refresh(prayer)

    formatted = _format_prayer(prayer)
    return {
        "status": "success",
        "message": "Prayer request updated successfully",
        "data": formatted,
    }


@router.delete("/{prayer_id}")
async def delete_prayer_request(
    prayer_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    prayer = (
        await session.execute(
            select(PrayerRequest).where(
                PrayerRequest.id == prayer_id,
                PrayerRequest.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not prayer:
        raise HTTPException(status_code=404, detail="Prayer request not found")

    await session.delete(prayer)
    await session.commit()
    return {"status": "success", "message": "Prayer request deleted successfully"}


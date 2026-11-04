from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional
import uuid

from app.core.deps import CurrentUser, DbSession, church_id_from, resolve_actor
from app.models.member import Member
from app.models.operation import Announcement
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


def _format_announcement(a: Announcement, author_name: str = "") -> Dict[str, Any]:
    return {
        "_id": str(a.id),
        "id": str(a.id),
        "title": a.title,
        "content": a.content,
        "category": "General",
        "priority": "Medium",
        "publishDate": _iso(a.publish_date or a.created_at),
        "targetAudience": "All Members",
        "author": author_name or "Church Admin",
        "authorRole": "Admin",
        "status": "Active" if a.is_active else "Archived",
        "isPinned": False,
        "pinned": False,
        "likes": 0,
        "comments": 0,
        "createdAt": _iso(a.created_at),
        "updatedAt": _iso(a.updated_at),
    }


class AnnouncementPayload(BaseModel):
    title: str
    content: str
    category: Optional[str] = "General"
    priority: Optional[str] = "Medium"
    publishDate: Optional[str] = None
    targetAudience: Optional[str] = "All Members"
    expiryDate: Optional[str] = None
    status: Optional[str] = "Active"
    isPinned: Optional[bool] = False
    branchId: Optional[str] = None
    branch_id: Optional[str] = None


class MessagePayload(BaseModel):
    type: Optional[str] = "SMS"
    title: str
    content: str
    recipients: Optional[List[str]] = None
    scheduleType: Optional[str] = "now"
    scheduleDate: Optional[str] = None
    scheduleTime: Optional[str] = None
    template: Optional[str] = None


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------
@router.get("/announcements")
async def list_announcements(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
    search: str = Query("", max_length=100),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Announcement.church_id == actor.church_id]
    if search:
        s = f"%{search.strip()}%"
        conds.append(or_(Announcement.title.ilike(s), Announcement.content.ilike(s)))

    total = (
        await session.execute(select(func.count(Announcement.id)).where(*conds))
    ).scalar() or 0

    offset = (page - 1) * limit
    rows = (
        (
            await session.execute(
                select(Announcement, Member, User)
                .outerjoin(Member, Member.id == Announcement.author_member_id)
                .outerjoin(User, User.id == Member.user_id)
                .where(*conds)
                .order_by(Announcement.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .all()
    )

    announcements = [
        _format_announcement(a, f"{u.first_name} {u.last_name}".strip() if u else "")
        for a, m, u in rows
    ]
    pages = math.ceil(total / limit) if limit else 1
    response.headers["X-Total-Count"] = str(total)

    return {
        "announcements": announcements,
        "data": announcements,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": pages,
            "hasNext": page < pages,
            "hasPrev": page > 1,
        },
    }


@router.post("/announcements")
async def create_announcement(
    request: Request,
    payload: AnnouncementPayload,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    pub_date = _now()
    if payload.publishDate:
        try:
            pub_date = datetime.strptime(payload.publishDate, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            pub_date = _now()

    author_id = actor.member.id if actor.member else None

    announcement = Announcement(
        title=title,
        content=payload.content,
        church_id=actor.church_id,
        branch_id=payload.branchId or payload.branch_id,
        author_member_id=author_id,
        publish_date=pub_date,
        is_active=payload.status != "Archived",
    )
    session.add(announcement)
    await session.commit()
    await session.refresh(announcement)

    queue_audit(
        background_tasks,
        request,
        "CREATE_ANNOUNCEMENT",
        "ANNOUNCEMENT",
        announcement.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    formatted = _format_announcement(announcement)
    return {
        "status": "success",
        "message": "Announcement created successfully",
        "announcement": formatted,
        "data": formatted,
    }


@router.get("/announcements/{announcement_id}")
async def get_announcement(
    announcement_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    a = (
        await session.execute(
            select(Announcement).where(
                Announcement.id == announcement_id,
                Announcement.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Announcement not found")

    formatted = _format_announcement(a)
    return {"announcement": formatted, "data": {"announcement": formatted}}


@router.put("/announcements/{announcement_id}")
async def update_announcement(
    announcement_id: str,
    payload: AnnouncementPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    a = (
        await session.execute(
            select(Announcement).where(
                Announcement.id == announcement_id,
                Announcement.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Announcement not found")

    if payload.title:
        a.title = payload.title.strip()
    if payload.content:
        a.content = payload.content
    if payload.status:
        a.is_active = payload.status != "Archived"

    a.updated_at = _now()
    await session.commit()
    await session.refresh(a)

    formatted = _format_announcement(a)
    return {
        "status": "success",
        "message": "Announcement updated successfully",
        "announcement": formatted,
        "data": formatted,
    }


@router.delete("/announcements/{announcement_id}")
async def delete_announcement(
    announcement_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    a = (
        await session.execute(
            select(Announcement).where(
                Announcement.id == announcement_id,
                Announcement.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Announcement not found")

    await session.delete(a)
    await session.commit()
    return {"status": "success", "message": "Announcement deleted successfully"}


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
# In-memory store for broadcast messages within session
_MESSAGES_STORE: List[Dict[str, Any]] = []


@router.get("/messages")
async def list_messages(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: str = Query("", max_length=100),
    type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    scheduleType: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church_messages = [
        m for m in _MESSAGES_STORE if m.get("churchId") == actor.church_id
    ]

    if search:
        s = search.lower()
        church_messages = [
            m for m in church_messages
            if s in m.get("title", "").lower() or s in m.get("content", "").lower()
        ]

    total = len(church_messages)
    offset = (page - 1) * limit
    paginated = church_messages[offset : offset + limit]
    pages = math.ceil(total / limit) if limit else 1

    return {
        "data": paginated,
        "messages": paginated,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": pages,
            "hasNext": page < pages,
            "hasPrev": page > 1,
        },
    }


@router.post("/messages")
async def create_message(
    request: Request,
    payload: MessagePayload,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    msg_id = str(uuid.uuid4())
    msg_obj = {
        "_id": msg_id,
        "id": msg_id,
        "churchId": actor.church_id,
        "type": payload.type or "SMS",
        "title": payload.title,
        "content": payload.content,
        "recipients": payload.recipients or ["All Members"],
        "recipientCount": len(payload.recipients or [1]) * 10,
        "scheduleType": payload.scheduleType or "now",
        "status": "sent" if payload.scheduleType != "draft" else "draft",
        "sentAt": _iso(_now()),
        "createdAt": _iso(_now()),
    }
    _MESSAGES_STORE.insert(0, msg_obj)

    queue_audit(
        background_tasks,
        request,
        "CREATE_MESSAGE",
        "MESSAGE",
        msg_id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    return {
        "status": "success",
        "message": "Message sent successfully",
        "data": msg_obj,
    }


@router.delete("/messages/{message_id}")
async def delete_message(
    message_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    global _MESSAGES_STORE
    _MESSAGES_STORE = [m for m in _MESSAGES_STORE if m.get("_id") != message_id and m.get("id") != message_id]
    return {"status": "success", "message": "Message deleted successfully"}


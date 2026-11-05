import csv
from datetime import datetime, timedelta, timezone
import io
import math
from typing import Any, Dict, List, Optional, Tuple
import uuid

from app.core.deps import CurrentUser, DbSession, church_id_from, resolve_actor
from app.enums import AttendanceStatus, MemberType
from app.models.church import Branch, Church
from app.models.detail import VolunteerDetails
from app.models.event import Activity, Attendance, Event
from app.models.finance import Expense, Fund, Offering, Pledge
from app.models.group import Department, DepartmentMember, Group, GroupMember
from app.models.member import Member, MembershipStatus
from app.models.operation import Announcement
from app.models.user import User
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm.attributes import flag_modified

router = APIRouter()
root_router = APIRouter()


async def _get_church(session: DbSession, church_id: str) -> Church:
    church = (
        await session.execute(select(Church).where(Church.id == church_id))
    ).scalar_one_or_none()
    if not church:
        raise HTTPException(status_code=404, detail="Church not found")
    return church


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
        _format_announcement(
            a,
            (
                f"{m.first_name or ''} {m.last_name or ''}".strip()
                if m
                else (u.display_name if u and u.display_name else "")
            ),
        )
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
# Messages & Recipient Groups
# ---------------------------------------------------------------------------
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
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    church_messages: List[Dict[str, Any]] = list(meta.get("messages") or [])

    if search:
        s = search.lower()
        church_messages = [
            m
            for m in church_messages
            if s in (m.get("title") or "").lower() or s in (m.get("content") or "").lower()
        ]
    if type and type.lower() != "all":
        church_messages = [
            m for m in church_messages if (m.get("type") or "").lower() == type.lower()
        ]
    if status and status.lower() != "all":
        church_messages = [
            m for m in church_messages if (m.get("status") or "").lower() == status.lower()
        ]
    if scheduleType and scheduleType.lower() != "all":
        church_messages = [
            m
            for m in church_messages
            if (m.get("scheduleType") or "").lower() == scheduleType.lower()
        ]

    total = len(church_messages)
    offset = (page - 1) * limit
    paginated = church_messages[offset : offset + limit]
    pages = max(1, math.ceil(total / limit)) if limit else 1

    return {
        "data": {
            "messages": paginated,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "pages": pages,
                "hasNext": page < pages,
                "hasPrev": page > 1,
            },
        },
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
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    church_messages: List[Dict[str, Any]] = list(meta.get("messages") or [])

    total_members = (
        await session.execute(
            select(func.count(Member.id)).where(
                Member.church_id == actor.church_id,
                Member.deleted_at.is_(None),
            )
        )
    ).scalar() or 1

    msg_id = str(uuid.uuid4())
    msg_obj = {
        "_id": msg_id,
        "id": msg_id,
        "churchId": actor.church_id,
        "type": payload.type or "SMS",
        "title": payload.title,
        "content": payload.content,
        "recipients": payload.recipients or ["all_members"],
        "recipientCount": total_members,
        "scheduleType": payload.scheduleType or "now",
        "status": "draft" if payload.scheduleType == "draft" else "sent",
        "sentAt": _iso(_now()),
        "createdAt": _iso(_now()),
    }
    church_messages.insert(0, msg_obj)
    meta["messages"] = church_messages
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

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
        "success": True,
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
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    church_messages: List[Dict[str, Any]] = list(meta.get("messages") or [])
    meta["messages"] = [
        m for m in church_messages if m.get("_id") != message_id and m.get("id") != message_id
    ]
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"status": "success", "success": True, "message": "Message deleted successfully"}


async def _build_recipient_groups(session: DbSession, church_id: str) -> List[Dict[str, Any]]:
    total_members = (
        await session.execute(
            select(func.count(Member.id)).where(
                Member.church_id == church_id, Member.deleted_at.is_(None)
            )
        )
    ).scalar() or 0

    active_members = (
        await session.execute(
            select(func.count(Member.id)).where(
                Member.church_id == church_id,
                Member.deleted_at.is_(None),
                Member.status == MembershipStatus.ACTIVE,
            )
        )
    ).scalar() or 0

    volunteer_count = (
        await session.execute(
            select(func.count(VolunteerDetails.id))
            .join(Member, Member.id == VolunteerDetails.member_id)
            .where(Member.church_id == church_id, Member.deleted_at.is_(None))
        )
    ).scalar() or 0

    groups_out: List[Dict[str, Any]] = [
        {
            "id": "all_members",
            "name": "All Members",
            "count": total_members,
            "type": "system",
            "description": "All registered church members",
            "targetModel": "User",
            "criteria": {},
        },
        {
            "id": "active_members",
            "name": "Active Members",
            "count": active_members,
            "type": "system",
            "description": "Active church members",
            "targetModel": "User",
            "criteria": {"status": "ACTIVE"},
        },
        {
            "id": "volunteers",
            "name": "Volunteers",
            "count": volunteer_count,
            "type": "system",
            "description": "All church volunteers",
            "targetModel": "User",
            "criteria": {"isVolunteer": True},
        },
    ]

    depts = (
        await session.execute(
            select(Department).where(Department.church_id == church_id)
        )
    ).scalars().all()
    for d in depts:
        cnt = (
            await session.execute(
                select(func.count(DepartmentMember.id)).where(
                    DepartmentMember.department_id == d.id
                )
            )
        ).scalar() or 0
        groups_out.append(
            {
                "id": f"department_{d.id}",
                "name": f"Dept: {d.name}",
                "count": cnt,
                "type": "department",
                "description": d.description or f"Members of {d.name}",
                "targetModel": "Department",
                "targetId": str(d.id),
                "criteria": {"departmentId": str(d.id)},
            }
        )

    small_groups = (
        await session.execute(select(Group).where(Group.church_id == church_id))
    ).scalars().all()
    for sg in small_groups:
        cnt = (
            await session.execute(
                select(func.count(GroupMember.id)).where(GroupMember.group_id == sg.id)
            )
        ).scalar() or 0
        groups_out.append(
            {
                "id": f"group_{sg.id}",
                "name": f"Group: {sg.name}",
                "count": cnt,
                "type": "group",
                "description": f"Members of {sg.name}",
                "targetModel": "Group",
                "targetId": str(sg.id),
                "criteria": {"groupId": str(sg.id)},
            }
        )

    return groups_out


@router.get("/recipient-groups")
@root_router.get("/recipient-groups")
async def get_recipient_groups(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    withCounts: bool = Query(True),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    groups_out = await _build_recipient_groups(session, actor.church_id)
    return {
        "success": True,
        "data": {
            "groups": groups_out,
            "total": len(groups_out),
        },
    }


@router.post("/recipient-groups/calculate-count")
@root_router.post("/recipient-groups/calculate-count")
async def calculate_recipient_group_count(
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    recipients = payload.get("recipients") or []
    groups_out = await _build_recipient_groups(session, actor.church_id)
    by_id = {g["id"]: g["count"] for g in groups_out}
    by_target = {g.get("targetId"): g["count"] for g in groups_out if g.get("targetId")}
    recipient_counts = []
    total_count = 0
    for rid in recipients:
        c = by_id.get(rid, by_target.get(rid, 0))
        recipient_counts.append({"recipientId": rid, "count": c})
        total_count += c
    return {
        "success": True,
        "data": {
            "totalCount": total_count,
            "recipientCounts": recipient_counts,
        },
    }


@router.post("/messages/members")
async def get_message_members(
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    rows = (
        await session.execute(
            select(Member).where(
                Member.church_id == actor.church_id,
                Member.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    members_list = [
        {
            "_id": str(m.id),
            "id": str(m.id),
            "firstName": m.first_name or "",
            "lastName": m.last_name or "",
            "email": m.email or "",
            "phoneNumber": m.phone_number or "",
        }
        for m in rows
    ]
    return {
        "success": True,
        "data": {
            "members": members_list,
            "totalCount": len(members_list),
            "membersBySource": {"all_members": [str(m.id) for m in rows]},
            "messageType": payload.get("messageType") or "sms",
        },
    }


# ---------------------------------------------------------------------------
# Content Library (/church/content)
# ---------------------------------------------------------------------------
@router.get("/content/stats")
async def get_content_stats(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    items: List[Dict[str, Any]] = list(meta.get("content") or [])

    total = len(items)
    published = sum(1 for c in items if c.get("status") == "published")
    draft = sum(1 for c in items if c.get("status") == "draft")
    archived = sum(1 for c in items if c.get("status") == "archived")
    total_views = sum(int(c.get("viewCount", 0) or 0) for c in items)
    total_downloads = sum(int(c.get("downloadCount", 0) or 0) for c in items)

    type_counts: Dict[str, int] = {}
    cat_counts: Dict[str, int] = {}
    for c in items:
        t = c.get("type") or "document"
        cat = c.get("category") or "general"
        type_counts[t] = type_counts.get(t, 0) + 1
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    stats_obj = {
        "totalContent": total,
        "publishedContent": published,
        "draftContent": draft,
        "archivedContent": archived,
        "privateContent": sum(1 for c in items if not c.get("isPublic", True)),
        "totalViews": total_views,
        "totalDownloads": total_downloads,
        "contentByType": [{"type": k, "count": v, "percentage": round((v / max(1, total)) * 100)} for k, v in type_counts.items()],
        "contentByCategory": [{"category": k, "count": v, "percentage": round((v / max(1, total)) * 100)} for k, v in cat_counts.items()],
        "recentContent": items[:5],
    }
    return {"success": True, "data": stats_obj, **stats_obj}


@router.get("/content/popular")
async def get_popular_content(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    limit: int = Query(10, ge=1),
    timeframe: str = Query("month"),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    items: List[Dict[str, Any]] = list(meta.get("content") or [])
    sorted_items = sorted(
        items,
        key=lambda x: int(x.get("viewCount", 0) or 0) + int(x.get("downloadCount", 0) or 0),
        reverse=True,
    )[:limit]
    return {
        "success": True,
        "data": {
            "content": sorted_items,
            "pagination": {"page": 1, "limit": limit, "total": len(sorted_items), "pages": 1, "hasNext": False, "hasPrev": False},
        },
    }


@router.get("/content")
async def list_content(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=200),
    search: str = Query(""),
    type: str = Query(""),
    category: str = Query(""),
    status: str = Query(""),
    tags: str = Query(""),
    sortBy: str = Query("createdAt"),
    sortOrder: str = Query("desc"),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    items: List[Dict[str, Any]] = list(meta.get("content") or [])

    filtered = []
    for c in items:
        if search:
            q = search.lower()
            if q not in f"{c.get('title', '')} {c.get('description', '')} {c.get('author', '')}".lower():
                continue
        if type and type.lower() != "all" and str(c.get("type", "")).lower() != type.lower():
            continue
        if category and category.lower() != "all" and str(c.get("category", "")).lower() != category.lower():
            continue
        if status and status.lower() != "all" and str(c.get("status", "")).lower() != status.lower():
            continue
        filtered.append(c)

    total = len(filtered)
    pages = max(1, math.ceil(total / limit)) if limit else 1
    start = (page - 1) * limit
    paginated = filtered[start : start + limit]

    return {
        "success": True,
        "data": {
            "content": paginated,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "pages": pages,
                "hasNext": page < pages,
                "hasPrev": page > 1,
            },
            "filters": {
                "search": search,
                "type": type,
                "category": category,
                "status": status,
                "tags": tags,
                "sortBy": sortBy,
                "sortOrder": sortOrder,
            },
        },
    }


@router.post("/content")
async def create_content(
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    items: List[Dict[str, Any]] = list(meta.get("content") or [])

    cid = str(uuid.uuid4())
    now_iso = _iso(_now())
    tags_val = payload.get("tags")
    if isinstance(tags_val, str):
        tags_list = [t.strip() for t in tags_val.split(",") if t.strip()]
    elif isinstance(tags_val, list):
        tags_list = tags_val
    else:
        tags_list = []

    new_item = {
        "_id": cid,
        "id": cid,
        "churchId": actor.church_id,
        "branchId": payload.get("branchId") or "",
        "title": (payload.get("title") or "Untitled Content").strip(),
        "description": payload.get("description") or "",
        "type": payload.get("type") or "sermon",
        "category": payload.get("category") or "spiritual",
        "tags": tags_list,
        "status": payload.get("status") or "published",
        "author": payload.get("author") or current_user.display_name or "Church Admin",
        "isPublic": bool(payload.get("isPublic", True)),
        "fileUrl": payload.get("fileUrl") or "",
        "fileName": payload.get("fileName") or "",
        "fileSize": payload.get("fileSize") or 0,
        "fileMimeType": payload.get("fileMimeType") or "",
        "viewCount": 0,
        "downloadCount": 0,
        "publishedAt": now_iso,
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }
    items.insert(0, new_item)
    meta["content"] = items
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {"success": True, "data": new_item, "message": "Content created successfully"}


@router.get("/content/{content_id}")
async def get_content_by_id(
    content_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    items: List[Dict[str, Any]] = list(meta.get("content") or [])
    found = next((c for c in items if c.get("_id") == content_id or c.get("id") == content_id), None)
    if not found:
        raise HTTPException(status_code=404, detail="Content not found")
    return {"success": True, "data": found, "message": "Content retrieved successfully"}


@router.put("/content")
@router.put("/content/{content_id}")
async def update_content(
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    content_id: Optional[str] = None,
    id: Optional[str] = Query(None),
):
    target_id = content_id or id or payload.get("id") or payload.get("_id")
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    items: List[Dict[str, Any]] = list(meta.get("content") or [])

    target = next((c for c in items if c.get("_id") == target_id or c.get("id") == target_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Content not found")

    for k in ("title", "description", "type", "category", "status", "author", "isPublic", "fileUrl", "fileName", "fileSize", "fileMimeType"):
        if k in payload and payload[k] is not None:
            target[k] = payload[k]
    if "tags" in payload and payload["tags"] is not None:
        tags_val = payload["tags"]
        target["tags"] = [t.strip() for t in tags_val.split(",") if t.strip()] if isinstance(tags_val, str) else tags_val
    target["updatedAt"] = _iso(_now())

    meta["content"] = items
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {"success": True, "data": target, "message": "Content updated successfully"}


@router.delete("/content/bulk")
async def bulk_delete_content(
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    ids = set(payload.get("ids") or [])
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    items: List[Dict[str, Any]] = list(meta.get("content") or [])
    remaining = [c for c in items if c.get("_id") not in ids and c.get("id") not in ids]
    deleted_count = len(items) - len(remaining)
    meta["content"] = remaining
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True, "message": "Deleted successfully", "deleted": deleted_count}


@router.patch("/content/bulk/status")
async def bulk_update_content_status(
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    ids = set(payload.get("ids") or [])
    new_status = payload.get("status") or "published"
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    items: List[Dict[str, Any]] = list(meta.get("content") or [])
    updated = 0
    for c in items:
        if c.get("_id") in ids or c.get("id") in ids:
            c["status"] = new_status
            c["updatedAt"] = _iso(_now())
            updated += 1
    meta["content"] = items
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True, "message": "Status updated", "updated": updated}


@router.delete("/content/{content_id}")
async def delete_content(
    content_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    items: List[Dict[str, Any]] = list(meta.get("content") or [])
    meta["content"] = [c for c in items if c.get("_id") != content_id and c.get("id") != content_id]
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True, "message": "Content deleted successfully"}


@router.patch("/content/{content_id}/view")
async def increment_content_view(
    content_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    items: List[Dict[str, Any]] = list(meta.get("content") or [])
    for c in items:
        if c.get("_id") == content_id or c.get("id") == content_id:
            c["viewCount"] = int(c.get("viewCount", 0) or 0) + 1
    meta["content"] = items
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True}


@router.patch("/content/{content_id}/download")
async def increment_content_download(
    content_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    items: List[Dict[str, Any]] = list(meta.get("content") or [])
    for c in items:
        if c.get("_id") == content_id or c.get("id") == content_id:
            c["downloadCount"] = int(c.get("downloadCount", 0) or 0) + 1
    meta["content"] = items
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True}


# ---------------------------------------------------------------------------
# Reports (/church/reports)
# ---------------------------------------------------------------------------
DEFAULT_REPORT_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "tpl_attendance_monthly",
        "name": "Monthly Attendance Summary",
        "type": "attendance",
        "description": "Standard monthly attendance report with check-in counts and event participation breakdown",
        "frequency": "Monthly",
        "dateRange": "last30days",
        "format": "csv",
        "isActive": True,
    },
    {
        "id": "tpl_financial_summary",
        "name": "Financial Dashboard & Giving Summary",
        "type": "financial",
        "description": "Comprehensive financial overview including offerings, tithes, pledges, and expenses",
        "frequency": "Monthly",
        "dateRange": "last30days",
        "format": "excel",
        "isActive": True,
    },
    {
        "id": "tpl_membership_growth",
        "name": "Member Growth & Demographics Report",
        "type": "membership",
        "description": "Church membership directory, active/inactive status, and branch distribution",
        "frequency": "Quarterly",
        "dateRange": "last3months",
        "format": "pdf",
        "isActive": True,
    },
    {
        "id": "tpl_events_participation",
        "name": "Event & Activity Participation Report",
        "type": "events",
        "description": "Analysis of church events, schedules, and attendance rates",
        "frequency": "Monthly",
        "dateRange": "last30days",
        "format": "csv",
        "isActive": True,
    },
]


def _ensure_report_templates(meta: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    templates = meta.get("report_templates")
    if isinstance(templates, list) and len(templates) > 0:
        return list(templates), False
    now_iso = _iso(_now())
    seeded = [
        {
            **tpl,
            "_id": tpl["id"],
            "lastGenerated": None,
            "createdAt": now_iso,
            "updatedAt": now_iso,
        }
        for tpl in DEFAULT_REPORT_TEMPLATES
    ]
    meta["report_templates"] = seeded
    return seeded, True


def _resolve_date_range_bounds(
    date_range: str,
    custom_start: Optional[str] = None,
    custom_end: Optional[str] = None,
) -> Tuple[datetime, datetime]:
    now = _now()
    dr = (date_range or "last30days").lower()
    if dr == "last7days":
        return now - timedelta(days=7), now
    if dr in ("last30days", "monthly"):
        return now - timedelta(days=30), now
    if dr in ("last3months", "quarterly"):
        return now - timedelta(days=90), now
    if dr == "last6months":
        return now - timedelta(days=180), now
    if dr in ("lastyear", "annual", "yearly"):
        return now - timedelta(days=365), now
    if dr == "custom" and custom_start:
        try:
            start_dt = datetime.fromisoformat(custom_start.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        except Exception:
            start_dt = now - timedelta(days=30)
        if custom_end:
            try:
                end_dt = datetime.fromisoformat(custom_end.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
            except Exception:
                end_dt = now
        else:
            end_dt = now
        return start_dt, end_dt
    return now - timedelta(days=30), now


async def _compute_department_breakdown(
    session: DbSession, church_id: str, dept_ids: List[str]
) -> List[Dict[str, Any]]:
    breakdown: List[Dict[str, Any]] = []
    depts = (
        await session.execute(
            select(Department).where(Department.church_id == church_id)
        )
    ).scalars().all()
    dept_map = {str(d.id): d for d in depts}
    dept_map.update({f"department_{d.id}": d for d in depts})

    if not dept_ids or "all_members" in dept_ids or "all" in [d.lower() for d in dept_ids]:
        target_depts = depts
    else:
        target_depts = [dept_map[did] for did in dept_ids if did in dept_map]

    for d in target_depts:
        cnt = (
            await session.execute(
                select(func.count(DepartmentMember.id)).where(
                    DepartmentMember.department_id == d.id
                )
            )
        ).scalar() or 0
        breakdown.append(
            {
                "departmentId": str(d.id),
                "departmentName": d.name,
                "count": cnt,
            }
        )

    if not breakdown:
        total_members = (
            await session.execute(
                select(func.count(Member.id)).where(
                    Member.church_id == church_id, Member.deleted_at.is_(None)
                )
            )
        ).scalar() or 0
        breakdown.append(
            {
                "departmentId": "all_members",
                "departmentName": "All Church Members",
                "count": total_members,
            }
        )
    return breakdown


async def _generate_live_report_data(
    session: DbSession,
    church_id: str,
    report_type: str,
    date_range: str,
    custom_start: Optional[str] = None,
    custom_end: Optional[str] = None,
    departments: Optional[List[str]] = None,
) -> Dict[str, Any]:
    start_dt, end_dt = _resolve_date_range_bounds(
        date_range, custom_start, custom_end
    )
    rtype = (report_type or "attendance").lower()
    dept_breakdown = await _compute_department_breakdown(
        session, church_id, departments or []
    )

    columns: List[str] = []
    rows: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {}

    if rtype == "attendance":
        columns = ["Event Name", "Member Name", "Status", "Checked In At"]
        att_rows = (
            await session.execute(
                select(Attendance, Event, Member)
                .join(Event, Event.id == Attendance.event_id)
                .join(Member, Member.id == Attendance.member_id)
                .where(Event.church_id == church_id)
                .order_by(Attendance.created_at.desc())
                .limit(250)
            )
        ).all()
        present_cnt = 0
        absent_cnt = 0
        events_seen = set()
        for att, ev, mem in att_rows:
            events_seen.add(str(ev.id))
            st_val = att.status.value if att.status else "PRESENT"
            if st_val == "PRESENT":
                present_cnt += 1
            else:
                absent_cnt += 1
            rows.append(
                {
                    "Event Name": ev.name,
                    "Member Name": mem.full_name,
                    "Status": st_val,
                    "Checked In At": _iso(att.checked_in_at or att.created_at) or "",
                }
            )
        if not rows:
            # Fallback to listing events if no individual check-ins recorded yet
            ev_rows = (
                await session.execute(
                    select(Event)
                    .where(Event.church_id == church_id)
                    .order_by(Event.start_time.desc())
                    .limit(100)
                )
            ).scalars().all()
            for ev in ev_rows:
                events_seen.add(str(ev.id))
                rows.append(
                    {
                        "Event Name": ev.name,
                        "Member Name": "All Attendees",
                        "Status": ev.status.value if ev.status else "ACTIVE",
                        "Checked In At": _iso(ev.start_time) or "",
                    }
                )
        summary = {
            "totalRecords": len(rows),
            "eventsCovered": len(events_seen),
            "presentCount": present_cnt,
            "totalPresent": present_cnt,
            "absentCount": absent_cnt,
        }

    elif rtype in ("financial", "finance", "giving"):
        columns = [
            "Date",
            "Entry Type",
            "Fund / Category",
            "Member / Description",
            "Payment Method",
            "Amount",
        ]
        off_rows = (
            await session.execute(
                select(Offering, Fund, Member)
                .outerjoin(Fund, Fund.id == Offering.fund_id)
                .outerjoin(Member, Member.id == Offering.member_id)
                .where(
                    Offering.church_id == church_id,
                    Offering.deleted_at.is_(None),
                )
                .order_by(Offering.received_at.desc())
                .limit(200)
            )
        ).all()
        total_offerings = 0.0
        for off, fund, mem in off_rows:
            amt = float(off.amount or 0)
            total_offerings += amt
            rows.append(
                {
                    "Date": _iso(off.received_at) or "",
                    "Entry Type": off.type.value if off.type else "OFFERING",
                    "Fund / Category": fund.name if fund else "General Fund",
                    "Member / Description": mem.full_name if mem else "Anonymous",
                    "Payment Method": (
                        off.payment_method.value if off.payment_method else "CASH"
                    ),
                    "Amount": f"{off.currency or 'KES'} {amt:,.2f}",
                }
            )

        exp_rows = (
            await session.execute(
                select(Expense)
                .where(
                    Expense.church_id == church_id,
                    Expense.deleted_at.is_(None),
                )
                .order_by(Expense.incurred_at.desc())
                .limit(100)
            )
        ).scalars().all()
        total_expenses = 0.0
        for exp in exp_rows:
            amt = float(exp.amount or 0)
            total_expenses += amt
            rows.append(
                {
                    "Date": _iso(exp.incurred_at) or "",
                    "Entry Type": "EXPENSE",
                    "Fund / Category": exp.category or "General",
                    "Member / Description": exp.description or "Church Expense",
                    "Payment Method": "N/A",
                    "Amount": f"-{exp.currency or 'KES'} {amt:,.2f}",
                }
            )

        pledges_total = (
            await session.execute(
                select(func.coalesce(func.sum(Pledge.amount), 0)).where(
                    Pledge.church_id == church_id,
                    Pledge.deleted_at.is_(None),
                )
            )
        ).scalar() or 0
        summary = {
            "totalRecords": len(rows),
            "totalIncome": round(total_offerings, 2),
            "totalOfferings": round(total_offerings, 2),
            "totalExpenses": round(total_expenses, 2),
            "netBalance": round(total_offerings - total_expenses, 2),
            "totalPledges": round(float(pledges_total), 2),
        }

    elif rtype in ("membership", "members"):
        columns = [
            "Membership #",
            "Full Name",
            "Email",
            "Phone",
            "Branch",
            "Member Type",
            "Status",
            "Joined Date",
        ]
        mem_rows = (
            await session.execute(
                select(Member, Branch)
                .outerjoin(Branch, Branch.id == Member.branch_id)
                .where(
                    Member.church_id == church_id,
                    Member.deleted_at.is_(None),
                )
                .order_by(Member.created_at.desc())
                .limit(300)
            )
        ).all()
        active_cnt = 0
        visitor_cnt = 0
        child_cnt = 0
        for m, b in mem_rows:
            if m.status == MembershipStatus.ACTIVE:
                active_cnt += 1
            if m.member_type == MemberType.VISITOR:
                visitor_cnt += 1
            elif m.member_type == MemberType.CHILD:
                child_cnt += 1
            rows.append(
                {
                    "Membership #": m.membership_number or "",
                    "Full Name": m.full_name,
                    "Email": m.email or "",
                    "Phone": m.phone_number or "",
                    "Branch": b.name if b else "Main Branch",
                    "Member Type": m.member_type.value if m.member_type else "MEMBER",
                    "Status": m.status.value if m.status else "ACTIVE",
                    "Joined Date": _iso(m.joined_at or m.created_at) or "",
                }
            )
        summary = {
            "totalRecords": len(rows),
            "activeMembers": active_cnt,
            "inactiveMembers": max(0, len(rows) - active_cnt),
            "visitors": visitor_cnt,
            "children": child_cnt,
        }

    else:
        # events / activities / general
        columns = [
            "Event / Activity",
            "Branch / Location",
            "Start Time",
            "End Time",
            "Status",
            "Attendees",
        ]
        ev_rows = (
            await session.execute(
                select(Event)
                .where(Event.church_id == church_id)
                .order_by(Event.start_time.desc())
                .limit(200)
            )
        ).scalars().all()
        for ev in ev_rows:
            att_cnt = (
                await session.execute(
                    select(func.count(Attendance.id)).where(
                        Attendance.event_id == ev.id
                    )
                )
            ).scalar() or 0
            rows.append(
                {
                    "Event / Activity": ev.name,
                    "Branch / Location": ev.location or "Main Sanctuary",
                    "Start Time": _iso(ev.start_time) or "",
                    "End Time": _iso(ev.end_time) or "",
                    "Status": ev.status.value if ev.status else "ACTIVE",
                    "Attendees": att_cnt,
                }
            )
        # Also include branch metadata activities
        branches = (
            await session.execute(
                select(Branch).where(
                    Branch.church_id == church_id, Branch.deleted_at.is_(None)
                )
            )
        ).scalars().all()
        for b in branches:
            b_acts = (b.metadata_json or {}).get("activities") or []
            for act in b_acts:
                rows.append(
                    {
                        "Event / Activity": act.get("activity") or act.get("name") or "Activity",
                        "Branch / Location": act.get("location") or b.name,
                        "Start Time": act.get("date") or act.get("startTime") or "",
                        "End Time": act.get("endTime") or "",
                        "Status": (act.get("status") or "PLANNED").upper(),
                        "Attendees": act.get("participants") or 0,
                    }
                )
        summary = {
            "totalRecords": len(rows),
            "eventsCount": len(ev_rows),
        }

    # Build CSV string to calculate true size
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([r.get(col, "") for col in columns])
    csv_bytes = buf.getvalue().encode("utf-8")
    byte_size = max(len(csv_bytes), 512)
    if byte_size >= 1024 * 1024:
        formatted_size = f"{byte_size / (1024 * 1024):.1f} MB"
    else:
        formatted_size = f"{max(1.0, round(byte_size / 1024, 1))} KB"

    return {
        "columns": columns,
        "rows": rows[:150],
        "summary": summary,
        "fileSize": byte_size,
        "formattedFileSize": formatted_size,
        "reportData": {
            "totalRecords": len(rows),
            "departmentCounts": dept_breakdown,
            "dateRangeUsed": {
                "startDate": _iso(start_dt),
                "endDate": _iso(end_dt),
            },
            "generationTime": 1,
        },
    }


def _build_report_download_bytes(report: Dict[str, Any]) -> Tuple[bytes, str, str]:
    fmt = (report.get("format") or "csv").lower()
    columns: List[str] = report.get("columns") or [
        "Report Name",
        "Type",
        "Date Range",
        "Status",
        "Generated At",
    ]
    rows: List[Dict[str, Any]] = report.get("rows") or []
    summary: Dict[str, Any] = report.get("summary") or {}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([f"Report: {report.get('name', 'Church Report')}"])
    writer.writerow(
        [
            f"Type: {report.get('type', 'general').upper()}",
            f"Generated At: {report.get('generatedAt', '')}",
            f"Generated By: {(report.get('createdBy') or {}).get('name', 'Church Admin')}",
        ]
    )
    if summary:
        writer.writerow(
            [f"{k}: {v}" for k, v in summary.items()]
        )
    writer.writerow([])
    writer.writerow(columns)
    if rows:
        for row in rows:
            writer.writerow([row.get(col, "") for col in columns])
    else:
        writer.writerow(["No records found for the selected criteria"])

    content_bytes = buf.getvalue().encode("utf-8")
    safe_slug = (
        "".join(
            c if c.isalnum() or c in ("-", "_") else "_"
            for c in (report.get("name") or "report").lower()
        ).strip("_")
        or "report"
    )
    ext = "csv" if fmt in ("csv", "excel") else "csv"
    filename = f"{safe_slug}_{str(report.get('id', ''))[:8]}.{ext}"
    return content_bytes, "text/csv; charset=utf-8", filename


@router.get("/reports/stats")
async def get_report_stats(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    reports: List[Dict[str, Any]] = list(meta.get("reports") or [])
    templates, seeded = _ensure_report_templates(meta)
    if seeded:
        church.metadata_json = meta
        flag_modified(church, "metadata_json")
        await session.commit()

    total = len(reports)
    completed = sum(
        1 for r in reports if (r.get("status") or "").lower() == "completed"
    )
    generating = sum(
        1 for r in reports if (r.get("status") or "").lower() in ("generating", "processing")
    )
    failed = sum(1 for r in reports if (r.get("status") or "").lower() == "failed")
    total_downloads = sum(
        int(r.get("downloadCount", r.get("downloads", 0)) or 0) for r in reports
    )
    active_templates = sum(1 for t in templates if t.get("isActive", True))

    by_type: Dict[str, int] = {}
    downloads_by_type: Dict[str, int] = {}
    by_status: Dict[str, int] = {}

    now = _now()
    this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = this_month_start - timedelta(seconds=1)
    last_month_start = last_month_end.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    two_months_ago_end = last_month_start - timedelta(seconds=1)
    two_months_ago_start = two_months_ago_end.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    this_month_count = 0
    last_month_count = 0
    two_months_ago_count = 0

    for r in reports:
        t = (r.get("type") or "attendance").lower()
        st = (r.get("status") or "completed").lower()
        dl = int(r.get("downloadCount", r.get("downloads", 0)) or 0)
        by_type[t] = by_type.get(t, 0) + 1
        downloads_by_type[t] = downloads_by_type.get(t, 0) + dl
        by_status[st] = by_status.get(st, 0) + 1

        raw_dt = r.get("generatedAt") or r.get("createdAt")
        if raw_dt:
            try:
                dt = datetime.fromisoformat(str(raw_dt).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= this_month_start:
                    this_month_count += 1
                elif last_month_start <= dt <= last_month_end:
                    last_month_count += 1
                elif two_months_ago_start <= dt <= two_months_ago_end:
                    two_months_ago_count += 1
            except Exception:
                pass

    popular_raw = sorted(
        [
            {
                "id": r.get("id") or r.get("_id"),
                "name": r.get("name") or "Untitled Report",
                "type": r.get("type") or "attendance",
                "downloads": int(r.get("downloadCount", r.get("downloads", 0)) or 0),
                "generatedAt": r.get("generatedAt") or r.get("createdAt"),
            }
            for r in reports
        ],
        key=lambda x: x["downloads"],
        reverse=True,
    )[:5]
    max_dl = max((item["downloads"] for item in popular_raw), default=0) or 1
    popular_reports = [
        {
            **item,
            "percentage": round((item["downloads"] / max_dl) * 100) if item["downloads"] > 0 else 0,
        }
        for item in popular_raw
    ]

    # Live church database snapshot for analytics dashboard
    total_members = (
        await session.execute(
            select(func.count(Member.id)).where(
                Member.church_id == actor.church_id, Member.deleted_at.is_(None)
            )
        )
    ).scalar() or 0
    active_members = (
        await session.execute(
            select(func.count(Member.id)).where(
                Member.church_id == actor.church_id,
                Member.deleted_at.is_(None),
                Member.status == MembershipStatus.ACTIVE,
            )
        )
    ).scalar() or 0
    total_events = (
        await session.execute(
            select(func.count(Event.id)).where(Event.church_id == actor.church_id)
        )
    ).scalar() or 0
    total_departments = (
        await session.execute(
            select(func.count(Department.id)).where(
                Department.church_id == actor.church_id,
                Department.is_active.is_(True),
            )
        )
    ).scalar() or 0
    total_offerings = (
        await session.execute(
            select(func.coalesce(func.sum(Offering.amount), 0)).where(
                Offering.church_id == actor.church_id,
                Offering.deleted_at.is_(None),
            )
        )
    ).scalar() or 0
    total_expenses = (
        await session.execute(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.church_id == actor.church_id,
                Expense.deleted_at.is_(None),
            )
        )
    ).scalar() or 0

    return {
        "success": True,
        "data": {
            "totalReports": total,
            "completedReports": completed,
            "generatingReports": generating,
            "failedReports": failed,
            "totalDownloads": total_downloads,
            "activeTemplates": active_templates,
            "totalTemplates": len(templates),
            "generationTrends": {
                "thisMonth": this_month_count,
                "lastMonth": last_month_count,
                "twoMonthsAgo": two_months_ago_count,
            },
            "popularReports": popular_reports,
            "reportsByType": [
                {
                    "type": k,
                    "count": v,
                    "downloads": downloads_by_type.get(k, 0),
                }
                for k, v in by_type.items()
            ],
            "reportsByStatus": [
                {"status": k, "count": v} for k, v in by_status.items()
            ],
            "churchSummary": {
                "totalMembers": total_members,
                "activeMembers": active_members,
                "totalEvents": total_events,
                "totalDepartments": total_departments,
                "totalOfferings": float(total_offerings),
                "totalIncome": float(total_offerings),
                "totalExpenses": float(total_expenses),
            },
            "recentReports": reports[:5],
        },
    }


@router.get("/reports/templates")
async def list_report_templates(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    templates, seeded = _ensure_report_templates(meta)
    if seeded:
        church.metadata_json = meta
        flag_modified(church, "metadata_json")
        await session.commit()
    return {"success": True, "data": {"templates": templates}, "templates": templates}


@router.post("/reports/templates")
async def create_report_template(
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    templates, _ = _ensure_report_templates(meta)
    tid = f"tpl_{uuid.uuid4().hex[:10]}"
    now_iso = _iso(_now())
    new_tpl = {
        "id": tid,
        "_id": tid,
        "name": (payload.get("name") or "Custom Report Template").strip(),
        "type": (payload.get("type") or "attendance").lower(),
        "description": payload.get("description") or "",
        "frequency": payload.get("frequency") or "Monthly",
        "dateRange": payload.get("dateRange") or "last30days",
        "format": (payload.get("format") or "csv").lower(),
        "isActive": bool(payload.get("isActive", True)),
        "lastGenerated": None,
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }
    templates.insert(0, new_tpl)
    meta["report_templates"] = templates
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {
        "success": True,
        "data": new_tpl,
        "message": "Report template created successfully",
    }


@router.put("/reports/templates/{template_id}")
@router.patch("/reports/templates/{template_id}")
async def update_report_template(
    template_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    templates, _ = _ensure_report_templates(meta)
    target = next(
        (
            t
            for t in templates
            if t.get("id") == template_id or t.get("_id") == template_id
        ),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail="Report template not found")
    for k in (
        "name",
        "type",
        "description",
        "frequency",
        "dateRange",
        "format",
        "isActive",
    ):
        if k in payload and payload[k] is not None:
            target[k] = payload[k]
    target["updatedAt"] = _iso(_now())
    meta["report_templates"] = templates
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {
        "success": True,
        "data": target,
        "message": "Report template updated successfully",
    }


@router.delete("/reports/templates/{template_id}")
async def delete_report_template(
    template_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    templates, _ = _ensure_report_templates(meta)
    meta["report_templates"] = [
        t
        for t in templates
        if t.get("id") != template_id and t.get("_id") != template_id
    ]
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True, "message": "Report template deleted successfully"}


@router.post("/reports/templates/{template_id}/generate")
async def generate_report_from_template(
    template_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    templates, _ = _ensure_report_templates(meta)
    target = next(
        (
            t
            for t in templates
            if t.get("id") == template_id or t.get("_id") == template_id
        ),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail="Report template not found")

    now_iso = _iso(_now())
    date_label = datetime.now(timezone.utc).strftime("%b %Y")
    report_payload = {
        "name": f"{target.get('name')} - {date_label}",
        "type": target.get("type") or "attendance",
        "description": target.get("description") or "",
        "dateRange": target.get("dateRange") or "last30days",
        "format": target.get("format") or "csv",
        "includeCharts": True,
        "includeComparisons": True,
        "departments": ["all_members"],
    }
    res = await create_report(report_payload, request, current_user, session)
    # Refresh church metadata to update template.lastGenerated
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    templates, _ = _ensure_report_templates(meta)
    for t in templates:
        if t.get("id") == template_id or t.get("_id") == template_id:
            t["lastGenerated"] = now_iso
            t["updatedAt"] = now_iso
    meta["report_templates"] = templates
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return res


@router.get("/reports/export")
async def export_all_reports(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    reports: List[Dict[str, Any]] = list(meta.get("reports") or [])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Report ID",
            "Name",
            "Type",
            "Date Range",
            "Format",
            "Status",
            "Total Records",
            "Downloads",
            "Generated By",
            "Generated At",
        ]
    )
    for r in reports:
        writer.writerow(
            [
                r.get("id") or r.get("_id") or "",
                r.get("name") or "",
                r.get("type") or "",
                r.get("dateRange") or "",
                (r.get("format") or "csv").upper(),
                r.get("status") or "completed",
                (r.get("reportData") or {}).get(
                    "totalRecords", len(r.get("rows") or [])
                ),
                r.get("downloadCount", r.get("downloads", 0)),
                (r.get("createdBy") or {}).get("name", "Church Admin"),
                r.get("generatedAt") or r.get("createdAt") or "",
            ]
        )

    csv_bytes = buf.getvalue().encode("utf-8")
    date_str = _now().strftime("%Y-%m-%d")
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="church_reports_export_{date_str}.csv"'
        },
    )


@router.get("/reports")
async def list_reports(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: str = Query(""),
    type: str = Query(""),
    status: str = Query(""),
    dateRange: str = Query(""),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    reports: List[Dict[str, Any]] = list(meta.get("reports") or [])

    filtered = []
    for r in reports:
        if (
            search
            and search.lower()
            not in f"{r.get('name', '')} {r.get('description', '')} {(r.get('createdBy') or {}).get('name', '')}".lower()
        ):
            continue
        if (
            type
            and type.lower() != "all"
            and str(r.get("type", "")).lower() != type.lower()
        ):
            continue
        if (
            status
            and status.lower() != "all"
            and str(r.get("status", "")).lower() != status.lower()
        ):
            continue
        if (
            dateRange
            and dateRange.lower() != "all"
            and str(r.get("dateRange", "")).lower() != dateRange.lower()
        ):
            continue
        filtered.append(r)

    total = len(filtered)
    pages = max(1, math.ceil(total / limit)) if limit else 1
    start = (page - 1) * limit
    paginated = filtered[start : start + limit]

    return {
        "success": True,
        "data": {
            "reports": paginated,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "pages": pages,
                "hasNext": page < pages,
                "hasPrev": page > 1,
            },
        },
    }


@router.post("/reports")
async def create_report(
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    reports: List[Dict[str, Any]] = list(meta.get("reports") or [])

    rid = str(uuid.uuid4())
    now_iso = _iso(_now())
    fmt = (payload.get("format") or "csv").lower()
    rtype = (payload.get("type") or "attendance").lower()
    date_range = payload.get("dateRange") or "last30days"
    custom_start = payload.get("customStartDate")
    custom_end = payload.get("customEndDate")
    depts = payload.get("departments") or ["all_members"]

    live = await _generate_live_report_data(
        session,
        actor.church_id,
        rtype,
        date_range,
        custom_start,
        custom_end,
        depts,
    )

    author_name = (
        actor.member.full_name
        if actor.member
        else (current_user.display_name or "Church Admin")
    )
    new_report = {
        "_id": rid,
        "id": rid,
        "churchId": actor.church_id,
        "name": (payload.get("name") or "Church Report").strip(),
        "type": rtype,
        "description": payload.get("description") or "",
        "dateRange": date_range,
        "customStartDate": custom_start,
        "customEndDate": custom_end,
        "format": fmt,
        "includeCharts": bool(payload.get("includeCharts", True)),
        "includeComparisons": bool(payload.get("includeComparisons", False)),
        "departments": depts,
        "status": "completed",
        "fileName": f"report_{rid[:8]}.csv",
        "fileSize": live["fileSize"],
        "formattedFileSize": live["formattedFileSize"],
        "generatedAt": now_iso,
        "downloadCount": 0,
        "summary": live["summary"],
        "columns": live["columns"],
        "rows": live["rows"],
        "reportData": live["reportData"],
        "createdBy": {
            "_id": str(current_user.id),
            "name": author_name,
            "email": current_user.email or "",
        },
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }
    reports.insert(0, new_report)
    meta["reports"] = reports
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "success": True,
        "data": new_report,
        "message": "Report generated successfully",
    }


@router.post("/reports/preview")
async def preview_report(
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    rtype = (payload.get("type") or "attendance").lower()
    date_range = payload.get("dateRange") or "last30days"
    depts = payload.get("departments") or ["all_members"]
    live = await _generate_live_report_data(
        session,
        actor.church_id,
        rtype,
        date_range,
        payload.get("customStartDate"),
        payload.get("customEndDate"),
        depts,
    )
    return {
        "success": True,
        "data": {
            "estimatedRecords": live["reportData"]["totalRecords"],
            "departmentBreakdown": live["reportData"]["departmentCounts"],
            "dateRangeUsed": live["reportData"]["dateRangeUsed"],
            "estimatedSize": live["formattedFileSize"],
            "estimatedGenerationTime": "1 second",
            "summary": live["summary"],
            "columns": live["columns"],
            "sampleRows": live["rows"][:10],
        },
    }


@router.post("/reports/calculate-count")
async def calculate_report_department_count(
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept_ids = payload.get("departments") or []
    counts = await _compute_department_breakdown(
        session, actor.church_id, dept_ids
    )
    total = sum(c.get("count", 0) for c in counts)
    return {
        "success": True,
        "data": {"totalCount": total, "departmentCounts": counts},
    }


@router.delete("/reports/bulk")
async def bulk_delete_reports(
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    ids = set(payload.get("reportIds") or payload.get("ids") or [])
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    reports: List[Dict[str, Any]] = list(meta.get("reports") or [])
    remaining = [
        r for r in reports if r.get("_id") not in ids and r.get("id") not in ids
    ]
    deleted = len(reports) - len(remaining)
    meta["reports"] = remaining
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {
        "success": True,
        "data": {"deletedCount": deleted, "failedCount": 0},
        "message": "Reports deleted successfully",
    }


@router.get("/reports/{report_id}")
async def get_report_by_id(
    report_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    reports: List[Dict[str, Any]] = list(meta.get("reports") or [])
    found = next(
        (
            r
            for r in reports
            if r.get("_id") == report_id or r.get("id") == report_id
        ),
        None,
    )
    if not found:
        raise HTTPException(status_code=404, detail="Report not found")
    # If older report had no rows/summary populated, populate live data on the fly
    if "rows" not in found or "columns" not in found:
        live = await _generate_live_report_data(
            session,
            actor.church_id,
            found.get("type") or "attendance",
            found.get("dateRange") or "last30days",
            found.get("customStartDate"),
            found.get("customEndDate"),
            found.get("departments") or ["all_members"],
        )
        found["columns"] = live["columns"]
        found["rows"] = live["rows"]
        found["summary"] = live["summary"]
        found["reportData"] = live["reportData"]
    return {"success": True, "data": found}


@router.get("/reports/{report_id}/download")
async def download_report_by_id(
    report_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    reports: List[Dict[str, Any]] = list(meta.get("reports") or [])
    found = next(
        (
            r
            for r in reports
            if r.get("_id") == report_id or r.get("id") == report_id
        ),
        None,
    )
    if not found:
        raise HTTPException(status_code=404, detail="Report not found")

    if "rows" not in found or "columns" not in found:
        live = await _generate_live_report_data(
            session,
            actor.church_id,
            found.get("type") or "attendance",
            found.get("dateRange") or "last30days",
            found.get("customStartDate"),
            found.get("customEndDate"),
            found.get("departments") or ["all_members"],
        )
        found["columns"] = live["columns"]
        found["rows"] = live["rows"]
        found["summary"] = live["summary"]
        found["reportData"] = live["reportData"]

    found["downloadCount"] = int(found.get("downloadCount", 0) or 0) + 1
    meta["reports"] = reports
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    content_bytes, media_type, filename = _build_report_download_bytes(found)
    return Response(
        content=content_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/reports/{report_id}/cancel")
async def cancel_report_by_id(
    report_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    reports: List[Dict[str, Any]] = list(meta.get("reports") or [])
    for r in reports:
        if r.get("_id") == report_id or r.get("id") == report_id:
            r["status"] = "cancelled"
            r["updatedAt"] = _iso(_now())
    meta["reports"] = reports
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True, "message": "Report cancelled successfully"}


@router.post("/reports/{report_id}/duplicate")
async def duplicate_report_by_id(
    report_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    reports: List[Dict[str, Any]] = list(meta.get("reports") or [])
    found = next(
        (
            r
            for r in reports
            if r.get("_id") == report_id or r.get("id") == report_id
        ),
        None,
    )
    if not found:
        raise HTTPException(status_code=404, detail="Report not found")
    new_id = str(uuid.uuid4())
    now_iso = _iso(_now())
    dup = {
        **found,
        "_id": new_id,
        "id": new_id,
        "name": f"{found.get('name', 'Report')} (Copy)",
        "downloadCount": 0,
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }
    reports.insert(0, dup)
    meta["reports"] = reports
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {
        "success": True,
        "data": dup,
        "message": "Report duplicated successfully",
    }


@router.post("/reports/{report_id}/retry")
async def retry_report_by_id(
    report_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    reports: List[Dict[str, Any]] = list(meta.get("reports") or [])
    found = None
    for r in reports:
        if r.get("_id") == report_id or r.get("id") == report_id:
            live = await _generate_live_report_data(
                session,
                actor.church_id,
                r.get("type") or "attendance",
                r.get("dateRange") or "last30days",
                r.get("customStartDate"),
                r.get("customEndDate"),
                r.get("departments") or ["all_members"],
            )
            r["status"] = "completed"
            r["fileSize"] = live["fileSize"]
            r["formattedFileSize"] = live["formattedFileSize"]
            r["summary"] = live["summary"]
            r["columns"] = live["columns"]
            r["rows"] = live["rows"]
            r["reportData"] = live["reportData"]
            r["generatedAt"] = _iso(_now())
            r["updatedAt"] = _iso(_now())
            found = r
            break
    if not found:
        raise HTTPException(status_code=404, detail="Report not found")
    meta["reports"] = reports
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {
        "success": True,
        "data": found,
        "message": "Report regenerated successfully",
    }


@router.delete("/reports/{report_id}")
async def delete_report_by_id(
    report_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    reports: List[Dict[str, Any]] = list(meta.get("reports") or [])
    meta["reports"] = [
        r for r in reports if r.get("_id") != report_id and r.get("id") != report_id
    ]
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True, "message": "Report deleted successfully"}


# ---------------------------------------------------------------------------
# Public Demo Request (/demo)
# ---------------------------------------------------------------------------
@root_router.post("/demo")
async def submit_demo_request(payload: Dict[str, Any]):
    return {
        "success": True,
        "status": "success",
        "message": "Demo request submitted successfully",
        "data": {"id": str(uuid.uuid4()), **payload},
    }


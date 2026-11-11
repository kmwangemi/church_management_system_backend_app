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


_ANNOUNCEMENT_CATEGORY_LABELS = {
    "general": "General",
    "youth": "Youth",
    "children": "Children",
    "men": "Men",
    "women": "Women",
    "prayer": "Prayer",
    "events": "Events",
    "ministry": "Ministry",
}

_ANNOUNCEMENT_PRIORITY_LABELS = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
}

_ANNOUNCEMENT_AUDIENCE_LABELS = {
    "all": "All Members",
    "all members": "All Members",
    "youth": "Youth Group",
    "children": "Children Ministry",
    "men": "Men's Fellowship",
    "women": "Women's Ministry",
    "leaders": "Church Leaders",
    "volunteers": "Volunteers",
}

_ANNOUNCEMENT_STATUS_LABELS = {
    "published": "Published",
    "active": "Published",
    "draft": "Draft",
    "scheduled": "Scheduled",
    "expired": "Expired",
    "archived": "Archived",
}


def _normalize_announcement_meta(
    payload: Optional["AnnouncementPayload"] = None,
    existing: Optional[Dict[str, Any]] = None,
    is_active: bool = True,
) -> Dict[str, Any]:
    base = dict(existing or {})
    if payload is not None:
        if payload.category is not None:
            raw_cat = payload.category.strip()
            base["category"] = _ANNOUNCEMENT_CATEGORY_LABELS.get(
                raw_cat.lower(), raw_cat.title() if raw_cat else "General"
            )
        if payload.priority is not None:
            raw_pri = payload.priority.strip()
            base["priority"] = _ANNOUNCEMENT_PRIORITY_LABELS.get(
                raw_pri.lower(), raw_pri.title() if raw_pri else "Medium"
            )
        if payload.targetAudience is not None:
            raw_aud = payload.targetAudience.strip()
            base["targetAudience"] = _ANNOUNCEMENT_AUDIENCE_LABELS.get(
                raw_aud.lower(), raw_aud if raw_aud else "All Members"
            )
        if payload.expiryDate is not None:
            base["expiryDate"] = payload.expiryDate
        if payload.status is not None:
            raw_status = payload.status.strip()
            base["status"] = _ANNOUNCEMENT_STATUS_LABELS.get(
                raw_status.lower(), raw_status.title() if raw_status else "Published"
            )
        if payload.isPinned is not None:
            base["isPinned"] = bool(payload.isPinned)

    status_val = base.get("status")
    if not status_val:
        status_val = "Published" if is_active else "Archived"
    else:
        status_val = _ANNOUNCEMENT_STATUS_LABELS.get(status_val.lower(), status_val)

    expiry_str = base.get("expiryDate")
    if not expiry_str:
        expiry_str = (_now() + timedelta(days=30)).strftime("%Y-%m-%d")

    return {
        "category": base.get("category") or "General",
        "priority": base.get("priority") or "Medium",
        "targetAudience": base.get("targetAudience") or "All Members",
        "expiryDate": expiry_str,
        "status": status_val,
        "isPinned": bool(base.get("isPinned", False)),
        "views": int(base.get("views", 0) or 0),
    }


def _format_announcement(
    a: Announcement,
    author_name: str = "",
    meta_entry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    norm = _normalize_announcement_meta(None, meta_entry, is_active=bool(a.is_active))
    pub_dt = a.publish_date or a.created_at
    return {
        "_id": str(a.id),
        "id": str(a.id),
        "title": a.title,
        "content": a.content,
        "category": norm["category"],
        "priority": norm["priority"],
        "publishDate": _iso(pub_dt),
        "expiryDate": norm["expiryDate"],
        "targetAudience": norm["targetAudience"],
        "author": author_name or (meta_entry or {}).get("author") or "Church Admin",
        "authorRole": "Admin",
        "status": norm["status"],
        "isPinned": norm["isPinned"],
        "pinned": norm["isPinned"],
        "views": norm["views"],
        "likes": 0,
        "comments": 0,
        "createdAt": _iso(a.created_at),
        "updatedAt": _iso(a.updated_at),
    }


class AnnouncementPayload(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = "General"
    priority: Optional[str] = "Medium"
    publishDate: Optional[str] = None
    targetAudience: Optional[str] = "All Members"
    expiryDate: Optional[str] = None
    status: Optional[str] = "Published"
    isPinned: Optional[bool] = False
    branchId: Optional[str] = None
    branch_id: Optional[str] = None


class MessagePayload(BaseModel):
    type: Optional[str] = "SMS"
    title: Optional[str] = None
    content: Optional[str] = None
    recipients: Optional[List[str]] = None
    scheduleType: Optional[str] = "now"
    scheduleDate: Optional[str] = None
    scheduleTime: Optional[str] = None
    template: Optional[str] = None
    status: Optional[str] = None


class MessageTemplatePayload(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = "SMS"
    category: Optional[str] = "General"
    content: Optional[str] = None


def _default_message_templates() -> List[Dict[str, Any]]:
    now_iso = _iso(_now())
    return [
        {
            "id": "tpl-sunday-reminder",
            "_id": "tpl-sunday-reminder",
            "name": "Sunday Service Reminder",
            "type": "SMS",
            "category": "Service Reminders",
            "content": "Join us this Sunday for worship service at 9:00 AM and 11:00 AM. Youth service at 6:00 PM. See you there!",
            "usageCount": 4,
            "lastUsed": now_iso,
            "createdAt": now_iso,
        },
        {
            "id": "tpl-welcome-member",
            "_id": "tpl-welcome-member",
            "name": "Welcome New Member",
            "type": "Email",
            "category": "Member Onboarding",
            "content": "Welcome to our church family! We're excited to have you join our community of faith.",
            "usageCount": 2,
            "lastUsed": now_iso,
            "createdAt": now_iso,
        },
        {
            "id": "tpl-event-confirmation",
            "_id": "tpl-event-confirmation",
            "name": "Event Registration Confirmation",
            "type": "Email",
            "category": "Events",
            "content": "Thank you for registering for our upcoming event. Here are the details you need to know...",
            "usageCount": 3,
            "lastUsed": now_iso,
            "createdAt": now_iso,
        },
        {
            "id": "tpl-prayer-followup",
            "_id": "tpl-prayer-followup",
            "name": "Prayer Request Follow-up",
            "type": "SMS",
            "category": "Pastoral Care",
            "content": "We wanted to follow up on your recent prayer request and let you know we're still praying for you.",
            "usageCount": 1,
            "lastUsed": now_iso,
            "createdAt": now_iso,
        },
        {
            "id": "tpl-volunteer-schedule",
            "_id": "tpl-volunteer-schedule",
            "name": "Volunteer Schedule Notification",
            "type": "Email",
            "category": "Volunteers",
            "content": "Here is your volunteer schedule for the upcoming month. Thank you for your faithful service!",
            "usageCount": 2,
            "lastUsed": now_iso,
            "createdAt": now_iso,
        },
    ]


def _ensure_message_templates(meta: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    if meta.get("message_templates") is None:
        seeded = _default_message_templates()
        meta["message_templates"] = seeded
        return seeded, True
    return list(meta.get("message_templates") or []), False


def _normalize_message_record(m: Dict[str, Any], fallback_recipients: int = 0) -> Dict[str, Any]:
    msg_id = str(m.get("id") or m.get("_id") or uuid.uuid4())
    raw_type = str(m.get("type") or "SMS").strip()
    norm_type = "Email" if raw_type.lower() == "email" else "SMS"

    raw_status = str(m.get("status") or "").strip().lower()
    sched_type = str(m.get("scheduleType") or "now").strip().lower()
    if raw_status in ("draft",) or sched_type == "draft":
        norm_status = "Draft"
    elif raw_status in ("scheduled",) or sched_type == "scheduled":
        norm_status = "Scheduled"
    elif raw_status in ("failed",):
        norm_status = "Failed"
    else:
        norm_status = "Sent"

    raw_recipients = m.get("recipients")
    if isinstance(raw_recipients, list):
        recipient_groups = [str(x) for x in raw_recipients]
        recipient_count = int(m.get("recipientCount") or fallback_recipients or len(recipient_groups))
    else:
        recipient_groups = list(m.get("recipientGroups") or ["all_members"])
        try:
            recipient_count = int(raw_recipients if raw_recipients is not None else (m.get("recipientCount") or fallback_recipients))
        except Exception:
            recipient_count = int(m.get("recipientCount") or fallback_recipients or 0)

    sent_date = m.get("sentDate") or m.get("sentAt") or m.get("scheduleDate") or m.get("createdAt") or _iso(_now())
    delivery_rate = m.get("deliveryRate")
    if delivery_rate is None:
        delivery_rate = 100.0 if norm_status == "Sent" else 0.0

    return {
        **m,
        "_id": msg_id,
        "id": msg_id,
        "type": norm_type,
        "title": m.get("title") or "Untitled Message",
        "content": m.get("content") or "",
        "recipients": recipient_count,
        "recipientCount": recipient_count,
        "recipientGroups": recipient_groups,
        "recipientGroupNames": m.get("recipientGroupNames") or [],
        "scheduleType": m.get("scheduleType") or "now",
        "scheduleDate": m.get("scheduleDate"),
        "scheduleTime": m.get("scheduleTime"),
        "status": norm_status,
        "sentBy": m.get("sentBy") or "Church Admin",
        "sentDate": sent_date,
        "sentAt": m.get("sentAt") or sent_date,
        "deliveryRate": round(float(delivery_rate), 1),
        "createdAt": m.get("createdAt") or sent_date,
    }


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
    limit: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=100),
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church = await _get_church(session, actor.church_id)
    ann_meta_map: Dict[str, Any] = dict((church.metadata_json or {}).get("announcement_meta") or {})

    conds = [Announcement.church_id == actor.church_id]
    if search:
        s = f"%{search.strip()}%"
        conds.append(or_(Announcement.title.ilike(s), Announcement.content.ilike(s)))

    rows = (
        (
            await session.execute(
                select(Announcement, Member, User)
                .outerjoin(Member, Member.id == Announcement.author_member_id)
                .outerjoin(User, User.id == Member.user_id)
                .where(*conds)
                .order_by(Announcement.created_at.desc())
            )
        )
        .all()
    )

    all_announcements = [
        _format_announcement(
            a,
            (
                f"{m.first_name or ''} {m.last_name or ''}".strip()
                if m
                else (u.display_name if u and u.display_name else "")
            ),
            ann_meta_map.get(str(a.id)),
        )
        for a, m, u in rows
    ]

    if category and category.lower() != "all":
        all_announcements = [
            item for item in all_announcements if (item.get("category") or "").lower() == category.lower()
        ]
    if status and status.lower() != "all":
        all_announcements = [
            item for item in all_announcements if (item.get("status") or "").lower() == status.lower()
        ]
    if priority and priority.lower() != "all":
        all_announcements = [
            item for item in all_announcements if (item.get("priority") or "").lower() == priority.lower()
        ]

    # Sort pinned announcements first, then by createdAt descending
    all_announcements.sort(
        key=lambda item: (1 if item.get("isPinned") else 0, item.get("createdAt") or ""),
        reverse=True,
    )

    total = len(all_announcements)
    offset = (page - 1) * limit
    announcements = all_announcements[offset : offset + limit]
    pages = max(1, math.ceil(total / limit)) if limit else 1
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
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    pub_date = _now()
    if payload.publishDate:
        try:
            pub_date = datetime.strptime(payload.publishDate[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            pub_date = _now()

    author_id = actor.member.id if actor.member else None
    author_name = (
        f"{actor.member.first_name or ''} {actor.member.last_name or ''}".strip()
        if actor.member
        else (current_user.display_name or "Church Admin")
    )

    norm_meta = _normalize_announcement_meta(payload, None, is_active=True)
    norm_meta["author"] = author_name

    announcement = Announcement(
        title=title,
        content=payload.content or "",
        church_id=actor.church_id,
        branch_id=payload.branchId or payload.branch_id,
        author_member_id=author_id,
        publish_date=pub_date,
        is_active=norm_meta["status"] in ("Published", "Active"),
    )
    session.add(announcement)
    await session.flush()

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    ann_meta_map = dict(meta.get("announcement_meta") or {})
    ann_meta_map[str(announcement.id)] = norm_meta
    meta["announcement_meta"] = ann_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

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

    formatted = _format_announcement(announcement, author_name, norm_meta)
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

    church = await _get_church(session, actor.church_id)
    ann_meta_map = dict((church.metadata_json or {}).get("announcement_meta") or {})
    formatted = _format_announcement(a, "", ann_meta_map.get(str(a.id)))
    return {"announcement": formatted, "data": {"announcement": formatted}}


@router.post("/announcements/{announcement_id}/view")
async def increment_announcement_view(
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

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    ann_meta_map = dict(meta.get("announcement_meta") or {})
    entry = _normalize_announcement_meta(None, ann_meta_map.get(str(a.id)), is_active=bool(a.is_active))
    entry["views"] = int(entry.get("views", 0) or 0) + 1
    ann_meta_map[str(a.id)] = entry
    meta["announcement_meta"] = ann_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    formatted = _format_announcement(a, "", entry)
    return {
        "status": "success",
        "announcement": formatted,
        "data": formatted,
    }


@router.put("/announcements/{announcement_id}")
@router.patch("/announcements/{announcement_id}")
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

    if payload.title is not None and payload.title.strip():
        a.title = payload.title.strip()
    if payload.content is not None:
        a.content = payload.content
    if payload.publishDate:
        try:
            a.publish_date = datetime.strptime(payload.publishDate[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            pass

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    ann_meta_map = dict(meta.get("announcement_meta") or {})
    updated_meta = _normalize_announcement_meta(
        payload,
        ann_meta_map.get(str(a.id)),
        is_active=bool(a.is_active),
    )
    a.is_active = updated_meta["status"] in ("Published", "Active")
    ann_meta_map[str(a.id)] = updated_meta
    meta["announcement_meta"] = ann_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    a.updated_at = _now()
    await session.commit()
    await session.refresh(a)

    formatted = _format_announcement(a, "", updated_meta)
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

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    ann_meta_map = dict(meta.get("announcement_meta") or {})
    if str(a.id) in ann_meta_map:
        ann_meta_map.pop(str(a.id), None)
        meta["announcement_meta"] = ann_meta_map
        church.metadata_json = meta
        flag_modified(church, "metadata_json")

    await session.delete(a)
    await session.commit()
    return {"status": "success", "message": "Announcement deleted successfully"}


# ---------------------------------------------------------------------------
# Message Templates & Communication Stats
# ---------------------------------------------------------------------------
@router.get("/messages/templates")
async def list_message_templates(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    search: str = Query("", max_length=100),
    type: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    templates, changed = _ensure_message_templates(meta)
    if changed:
        church.metadata_json = meta
        flag_modified(church, "metadata_json")
        await session.commit()

    results = list(templates)
    if search:
        s = search.lower().strip()
        results = [
            t
            for t in results
            if s in (t.get("name") or "").lower()
            or s in (t.get("content") or "").lower()
            or s in (t.get("category") or "").lower()
        ]
    if type and type.lower() != "all":
        results = [
            t for t in results if (t.get("type") or "").lower() == type.lower()
        ]
    if category and category.lower() != "all":
        results = [
            t for t in results if (t.get("category") or "").lower() == category.lower()
        ]

    return {
        "status": "success",
        "templates": results,
        "data": {
            "templates": results,
            "total": len(results),
        },
    }


@router.post("/messages/templates")
async def create_message_template(
    payload: MessageTemplatePayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Template name is required")
    content = (payload.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Template content is required")

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    templates, _ = _ensure_message_templates(meta)

    raw_type = (payload.type or "SMS").strip()
    norm_type = "Email" if raw_type.lower() == "email" else "SMS"
    tpl_id = f"tpl-{uuid.uuid4().hex[:8]}"
    now_iso = _iso(_now())
    new_tpl = {
        "id": tpl_id,
        "_id": tpl_id,
        "name": name,
        "type": norm_type,
        "category": (payload.category or "General").strip() or "General",
        "content": content,
        "usageCount": 0,
        "lastUsed": now_iso,
        "createdAt": now_iso,
    }
    templates.insert(0, new_tpl)
    meta["message_templates"] = templates
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "status": "success",
        "message": "Message template created successfully",
        "template": new_tpl,
        "data": new_tpl,
    }


@router.put("/messages/templates/{template_id}")
@router.patch("/messages/templates/{template_id}")
async def update_message_template(
    template_id: str,
    payload: MessageTemplatePayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    templates, _ = _ensure_message_templates(meta)

    target = None
    for t in templates:
        if t.get("id") == template_id or t.get("_id") == template_id:
            target = t
            break

    if not target:
        raise HTTPException(status_code=404, detail="Message template not found")

    if payload.name is not None and payload.name.strip():
        target["name"] = payload.name.strip()
    if payload.type is not None and payload.type.strip():
        target["type"] = "Email" if payload.type.strip().lower() == "email" else "SMS"
    if payload.category is not None and payload.category.strip():
        target["category"] = payload.category.strip()
    if payload.content is not None and payload.content.strip():
        target["content"] = payload.content.strip()
    target["updatedAt"] = _iso(_now())

    meta["message_templates"] = templates
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "status": "success",
        "message": "Message template updated successfully",
        "template": target,
        "data": target,
    }


@router.delete("/messages/templates/{template_id}")
async def delete_message_template(
    template_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    templates, _ = _ensure_message_templates(meta)

    before_len = len(templates)
    templates = [
        t for t in templates if t.get("id") != template_id and t.get("_id") != template_id
    ]
    if len(templates) == before_len:
        raise HTTPException(status_code=404, detail="Message template not found")

    meta["message_templates"] = templates
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {"status": "success", "message": "Message template deleted successfully"}


@router.get("/communications/stats")
@router.get("/messages/stats")
async def get_communication_stats(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    templates, changed = _ensure_message_templates(meta)
    if changed:
        church.metadata_json = meta
        flag_modified(church, "metadata_json")
        await session.commit()

    total_members = (
        await session.execute(
            select(func.count(Member.id)).where(
                Member.church_id == actor.church_id,
                Member.deleted_at.is_(None),
            )
        )
    ).scalar() or 0

    raw_messages: List[Dict[str, Any]] = list(meta.get("messages") or [])
    messages = [_normalize_message_record(m, total_members) for m in raw_messages]

    ann_rows = (
        await session.execute(
            select(Announcement).where(Announcement.church_id == actor.church_id)
        )
    ).scalars().all()
    ann_meta_map: Dict[str, Any] = dict(meta.get("announcement_meta") or {})
    announcements = [
        _format_announcement(a, "", ann_meta_map.get(str(a.id))) for a in ann_rows
    ]

    now_dt = _now()
    today_str = now_dt.strftime("%Y-%m-%d")
    week_ago = now_dt - timedelta(days=7)
    two_weeks_ago = now_dt - timedelta(days=14)

    sent_today = 0
    total_recipients = 0
    sms_rates: List[float] = []
    email_rates: List[float] = []
    sms_count = 0
    email_count = 0
    messages_this_week = 0
    messages_last_week = 0

    for m in messages:
        total_recipients += int(m.get("recipients") or 0)
        m_type = (m.get("type") or "SMS").upper()
        if m_type == "SMS":
            sms_count += 1
            if m.get("status") == "Sent":
                sms_rates.append(float(m.get("deliveryRate") or 100.0))
        else:
            email_count += 1
            if m.get("status") == "Sent":
                email_rates.append(float(m.get("deliveryRate") or 100.0))

        sent_date_str = str(m.get("sentDate") or m.get("createdAt") or "")
        if m.get("status") == "Sent" and sent_date_str[:10] == today_str:
            sent_today += 1

        try:
            dt_val = datetime.fromisoformat(sent_date_str.replace("Z", "+00:00"))
            if dt_val.tzinfo is None:
                dt_val = dt_val.replace(tzinfo=timezone.utc)
            if dt_val >= week_ago:
                messages_this_week += 1
            elif dt_val >= two_weeks_ago:
                messages_last_week += 1
        except Exception:
            pass

    active_announcements = sum(
        1 for a in announcements if (a.get("status") or "").lower() == "published"
    )
    total_views = sum(int(a.get("views") or 0) for a in announcements)

    sms_delivery_rate = round(sum(sms_rates) / len(sms_rates), 1) if sms_rates else 98.5
    email_delivery_rate = (
        round(sum(email_rates) / len(email_rates), 1) if email_rates else 94.2
    )

    max_weekly = max(messages_this_week, messages_last_week, 1)
    this_week_percent = min(100, round((messages_this_week / max_weekly) * 100)) if messages_this_week else 0
    last_week_percent = min(100, round((messages_last_week / max_weekly) * 100)) if messages_last_week else 0

    stats_payload = {
        "totalMessages": len(messages),
        "sentToday": sent_today,
        "activeAnnouncements": active_announcements,
        "totalAnnouncements": len(announcements),
        "totalRecipients": total_recipients,
        "smsDeliveryRate": sms_delivery_rate,
        "emailDeliveryRate": email_delivery_rate,
        "smsCount": sms_count,
        "emailCount": email_count,
        "messagesThisWeek": messages_this_week,
        "messagesLastWeek": messages_last_week,
        "thisWeekPercent": this_week_percent,
        "lastWeekPercent": last_week_percent,
        "totalTemplates": len(templates),
        "totalViews": total_views,
    }
    return {
        "status": "success",
        "stats": stats_payload,
        "data": stats_payload,
    }


# ---------------------------------------------------------------------------
# Messages & Recipient Groups
# ---------------------------------------------------------------------------
@router.get("/messages")
async def list_messages(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
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
    total_members = (
        await session.execute(
            select(func.count(Member.id)).where(
                Member.church_id == actor.church_id,
                Member.deleted_at.is_(None),
            )
        )
    ).scalar() or 0

    raw_messages: List[Dict[str, Any]] = list(meta.get("messages") or [])
    church_messages = [_normalize_message_record(m, total_members) for m in raw_messages]

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
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Message title is required")

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    church_messages: List[Dict[str, Any]] = list(meta.get("messages") or [])

    groups_out = await _build_recipient_groups(session, actor.church_id)
    by_id = {g["id"]: g for g in groups_out}
    by_target = {g.get("targetId"): g for g in groups_out if g.get("targetId")}

    selected_groups = payload.recipients or ["all_members"]
    recipient_count = 0
    group_names: List[str] = []
    for rid in selected_groups:
        g = by_id.get(rid) or by_target.get(rid)
        if g:
            recipient_count += int(g.get("count") or 0)
            group_names.append(str(g.get("name") or rid))
        else:
            group_names.append(str(rid))

    if recipient_count <= 0:
        total_members = (
            await session.execute(
                select(func.count(Member.id)).where(
                    Member.church_id == actor.church_id,
                    Member.deleted_at.is_(None),
                )
            )
        ).scalar() or 1
        recipient_count = total_members

    sched_type = (payload.scheduleType or "now").lower()
    if sched_type == "draft":
        msg_status = "Draft"
    elif sched_type == "scheduled":
        msg_status = "Scheduled"
    else:
        msg_status = "Sent"

    raw_type = (payload.type or "SMS").strip()
    norm_type = "Email" if raw_type.lower() == "email" else "SMS"

    sent_by = (
        f"{actor.member.first_name or ''} {actor.member.last_name or ''}".strip()
        if actor.member
        else (current_user.display_name or "Church Admin")
    )

    now_iso = _iso(_now())
    sent_date = now_iso
    if sched_type == "scheduled" and payload.scheduleDate:
        if payload.scheduleTime:
            sent_date = f"{payload.scheduleDate[:10]}T{payload.scheduleTime}:00Z"
        else:
            sent_date = payload.scheduleDate

    # If a template was used, increment its usageCount
    if payload.template and payload.template != "none":
        templates, _ = _ensure_message_templates(meta)
        for t in templates:
            if (
                t.get("id") == payload.template
                or t.get("_id") == payload.template
                or (t.get("name") or "").lower() == payload.template.lower()
            ):
                t["usageCount"] = int(t.get("usageCount") or 0) + 1
                t["lastUsed"] = now_iso
                break
        meta["message_templates"] = templates

    msg_id = str(uuid.uuid4())
    msg_obj = _normalize_message_record(
        {
            "_id": msg_id,
            "id": msg_id,
            "churchId": actor.church_id,
            "type": norm_type,
            "title": title,
            "content": payload.content or "",
            "recipients": recipient_count,
            "recipientCount": recipient_count,
            "recipientGroups": selected_groups,
            "recipientGroupNames": group_names,
            "scheduleType": sched_type,
            "scheduleDate": payload.scheduleDate,
            "scheduleTime": payload.scheduleTime,
            "template": payload.template,
            "status": msg_status,
            "sentBy": sent_by,
            "sentDate": sent_date,
            "sentAt": now_iso,
            "deliveryRate": 100.0 if msg_status == "Sent" else 0.0,
            "createdAt": now_iso,
        },
        recipient_count,
    )
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
        "message": "Message saved as draft" if msg_status == "Draft" else ("Message scheduled successfully" if msg_status == "Scheduled" else "Message sent successfully"),
        "data": msg_obj,
    }


@router.get("/messages/{message_id}")
async def get_message(
    message_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    for m in list(meta.get("messages") or []):
        if m.get("_id") == message_id or m.get("id") == message_id:
            norm = _normalize_message_record(m)
            return {"status": "success", "message": norm, "data": norm}
    raise HTTPException(status_code=404, detail="Message not found")


@router.post("/messages/{message_id}/duplicate")
async def duplicate_message(
    message_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    church_messages: List[Dict[str, Any]] = list(meta.get("messages") or [])

    source = None
    for m in church_messages:
        if m.get("_id") == message_id or m.get("id") == message_id:
            source = m
            break
    if not source:
        raise HTTPException(status_code=404, detail="Message not found")

    norm_source = _normalize_message_record(source)
    new_id = str(uuid.uuid4())
    now_iso = _iso(_now())
    duplicated = _normalize_message_record(
        {
            **norm_source,
            "_id": new_id,
            "id": new_id,
            "title": f"{norm_source['title']} (Copy)",
            "status": "Draft",
            "scheduleType": "draft",
            "deliveryRate": 0.0,
            "sentDate": now_iso,
            "sentAt": now_iso,
            "createdAt": now_iso,
        },
        norm_source["recipients"],
    )
    church_messages.insert(0, duplicated)
    meta["messages"] = church_messages
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "status": "success",
        "success": True,
        "message": "Message duplicated as draft",
        "data": duplicated,
    }


@router.put("/messages/{message_id}")
@router.patch("/messages/{message_id}")
async def update_message(
    message_id: str,
    payload: MessagePayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    church_messages: List[Dict[str, Any]] = list(meta.get("messages") or [])

    idx = -1
    for i, m in enumerate(church_messages):
        if m.get("_id") == message_id or m.get("id") == message_id:
            idx = i
            break
    if idx < 0:
        raise HTTPException(status_code=404, detail="Message not found")

    current = dict(church_messages[idx])
    if payload.title is not None and payload.title.strip():
        current["title"] = payload.title.strip()
    if payload.content is not None:
        current["content"] = payload.content
    if payload.type is not None:
        current["type"] = "Email" if payload.type.strip().lower() == "email" else "SMS"
    if payload.scheduleType is not None:
        current["scheduleType"] = payload.scheduleType
    if payload.status is not None:
        current["status"] = payload.status
    elif payload.scheduleType is not None:
        st = payload.scheduleType.lower()
        current["status"] = "Draft" if st == "draft" else ("Scheduled" if st == "scheduled" else "Sent")
    if current.get("status") == "Sent" and not current.get("deliveryRate"):
        current["deliveryRate"] = 100.0
        current["sentDate"] = _iso(_now())

    updated = _normalize_message_record(current)
    church_messages[idx] = updated
    meta["messages"] = church_messages
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "status": "success",
        "success": True,
        "message": "Message updated successfully",
        "data": updated,
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


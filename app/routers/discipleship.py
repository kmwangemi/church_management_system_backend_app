from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional
import uuid

from app.core.deps import CurrentUser, DbSession, church_id_from, resolve_actor
from app.models.member import Member
from app.models.user import User
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


# In-memory stores for Disciples and Milestones
_DISCIPLES_STORE: List[Dict[str, Any]] = []
_MILESTONES_STORE: List[Dict[str, Any]] = [
    {
        "_id": "m-1",
        "id": "m-1",
        "name": "Foundations of Faith",
        "description": "Complete the 4-week introductory course on basic Christian beliefs and church life.",
        "category": "Foundations",
        "points": 20,
        "requirements": "Attend all 4 sessions and complete the study guide.",
        "isActive": True,
        "order": 1,
        "level": "BEGINNER",
        "completionCount": 14,
        "difficulty": "Easy",
        "formattedPoints": "20 pts",
        "createdAt": _iso(_now()),
        "updatedAt": _iso(_now()),
    },
    {
        "_id": "m-2",
        "id": "m-2",
        "name": "Water Baptism",
        "description": "Public declaration of faith through immersion baptism in water.",
        "category": "Spiritual Growth",
        "points": 30,
        "requirements": "Baptism class completion and pastoral interview.",
        "isActive": True,
        "order": 2,
        "level": "BEGINNER",
        "completionCount": 9,
        "difficulty": "Medium",
        "formattedPoints": "30 pts",
        "createdAt": _iso(_now()),
        "updatedAt": _iso(_now()),
    },
    {
        "_id": "m-3",
        "id": "m-3",
        "name": "Ministry Leadership Training",
        "description": "Equipping future leaders for small group facilitation and church ministries.",
        "category": "Leadership",
        "points": 50,
        "requirements": "6-week leadership workshop and mentor evaluation.",
        "isActive": True,
        "order": 3,
        "level": "LEADER",
        "completionCount": 5,
        "difficulty": "Advanced",
        "formattedPoints": "50 pts",
        "createdAt": _iso(_now()),
        "updatedAt": _iso(_now()),
    },
]


class DisciplePayload(BaseModel):
    memberId: str
    mentorId: str
    startDate: Optional[str] = None
    currentLevel: Optional[str] = "NEW_CONVERT"
    goals: Optional[str] = ""
    notes: Optional[str] = ""
    status: Optional[str] = "active"


class MilestonePayload(BaseModel):
    name: str
    description: str
    category: Optional[str] = "General"
    points: Any = 10
    requirements: Optional[str] = None
    level: Optional[str] = "BEGINNER"
    isActive: Optional[bool] = True


async def _resolve_member_info(session: DbSession, member_id: str) -> Dict[str, Any]:
    row = (
        await session.execute(
            select(Member, User)
            .outerjoin(User, User.id == Member.user_id)
            .where(Member.id == member_id)
        )
    ).first()
    if row:
        m, u = row
        return {
            "_id": str(m.id),
            "id": str(m.id),
            "firstName": u.first_name if u else "Member",
            "lastName": u.last_name if u else "",
            "email": u.email if u else "",
            "phone": m.phone_number if hasattr(m, "phone_number") else "",
        }
    return {
        "_id": member_id,
        "id": member_id,
        "firstName": "Church",
        "lastName": "Member",
        "email": "",
        "phone": "",
    }


# ---------------------------------------------------------------------------
# Disciples
# ---------------------------------------------------------------------------
@router.get("/disciples")
async def list_disciples(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: str = Query("", max_length=100),
    level: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    items = [d for d in _DISCIPLES_STORE if d.get("churchId") == actor.church_id or not d.get("churchId")]

    if search:
        s = search.lower()
        items = [
            d for d in items
            if s in d.get("memberId", {}).get("firstName", "").lower()
            or s in d.get("memberId", {}).get("lastName", "").lower()
            or s in d.get("goals", "").lower()
        ]
    if status:
        items = [d for d in items if d.get("status", "").lower() == status.lower()]
    if level:
        items = [d for d in items if d.get("currentLevel", "").lower() == level.lower()]

    total = len(items)
    offset = (page - 1) * limit
    paginated = items[offset : offset + limit]
    pages = math.ceil(total / limit) if limit else 1

    return {
        "success": True,
        "data": {
            "disciples": paginated,
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


@router.post("/disciples")
async def create_disciple(
    request: Request,
    payload: DisciplePayload,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    d_id = str(uuid.uuid4())
    mem_info = await _resolve_member_info(session, payload.memberId)
    mentor_info = await _resolve_member_info(session, payload.mentorId)

    start_date = payload.startDate or _iso(_now())

    disciple = {
        "_id": d_id,
        "id": d_id,
        "churchId": actor.church_id,
        "branchId": str(actor.member.branch_id) if actor.member and actor.member.branch_id else "main",
        "memberId": mem_info,
        "mentorId": mentor_info,
        "startDate": start_date,
        "currentLevel": payload.currentLevel or "NEW_CONVERT",
        "status": payload.status or "active",
        "goals": payload.goals or "",
        "notes": payload.notes or "",
        "progress": 20,
        "milestonesCompleted": [],
        "durationInDays": 30,
        "completionPercentage": 20,
        "createdAt": _iso(_now()),
        "updatedAt": _iso(_now()),
    }
    _DISCIPLES_STORE.insert(0, disciple)

    queue_audit(
        background_tasks,
        request,
        "CREATE_DISCIPLE",
        "DISCIPLE",
        d_id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    return {"success": True, "data": {"disciple": disciple}}


@router.get("/disciples/{disciple_id}")
async def get_disciple(
    disciple_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    for d in _DISCIPLES_STORE:
        if d.get("_id") == disciple_id or d.get("id") == disciple_id:
            return {
                "success": True,
                "data": {
                    "disciple": d,
                    "progressRecords": [],
                },
            }
    raise HTTPException(status_code=404, detail="Disciple record not found")


@router.put("/disciples/{disciple_id}")
async def update_disciple(
    disciple_id: str,
    payload: DisciplePayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    for d in _DISCIPLES_STORE:
        if d.get("_id") == disciple_id or d.get("id") == disciple_id:
            if payload.goals:
                d["goals"] = payload.goals
            if payload.notes:
                d["notes"] = payload.notes
            if payload.currentLevel:
                d["currentLevel"] = payload.currentLevel
            if payload.status:
                d["status"] = payload.status
            d["updatedAt"] = _iso(_now())
            return {"success": True, "data": {"disciple": d}}
    raise HTTPException(status_code=404, detail="Disciple record not found")


@router.delete("/disciples/{disciple_id}")
async def delete_disciple(
    disciple_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    global _DISCIPLES_STORE
    _DISCIPLES_STORE = [d for d in _DISCIPLES_STORE if d.get("_id") != disciple_id and d.get("id") != disciple_id]
    return {"success": True, "message": "Disciple record deleted successfully"}


@router.post("/disciples/{disciple_id}/milestones")
async def add_disciple_milestone(
    disciple_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    milestone_id = payload.get("milestoneId")
    for d in _DISCIPLES_STORE:
        if d.get("_id") == disciple_id or d.get("id") == disciple_id:
            found_m = next((m for m in _MILESTONES_STORE if m.get("_id") == milestone_id), None)
            if found_m:
                d.setdefault("milestonesCompleted", []).append(
                    {
                        "_id": found_m["_id"],
                        "name": found_m["name"],
                        "points": found_m["points"],
                        "category": found_m["category"],
                    }
                )
                d["progress"] = min(100, d.get("progress", 0) + 25)
            return {"success": True, "message": "Milestone recorded successfully"}
    return {"success": True, "message": "Milestone recorded"}


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------
@router.get("/milestones")
async def list_milestones(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: str = Query("", max_length=100),
    category: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    items = list(_MILESTONES_STORE)
    if search:
        s = search.lower()
        items = [m for m in items if s in m.get("name", "").lower() or s in m.get("description", "").lower()]
    if category:
        items = [m for m in items if m.get("category", "").lower() == category.lower()]

    total = len(items)
    offset = (page - 1) * limit
    paginated = items[offset : offset + limit]
    pages = math.ceil(total / limit) if limit else 1

    return {
        "success": True,
        "data": {
            "milestones": paginated,
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


@router.post("/milestones")
async def create_milestone(
    request: Request,
    payload: MilestonePayload,
    current_user: CurrentUser,
    session: DbSession,
):
    m_id = str(uuid.uuid4())
    try:
        pts = int(payload.points)
    except Exception:
        pts = 10

    milestone = {
        "_id": m_id,
        "id": m_id,
        "name": payload.name,
        "description": payload.description,
        "category": payload.category or "General",
        "points": pts,
        "formattedPoints": f"{pts} pts",
        "requirements": payload.requirements or "",
        "isActive": payload.isActive if payload.isActive is not None else True,
        "order": len(_MILESTONES_STORE) + 1,
        "level": payload.level or "BEGINNER",
        "completionCount": 0,
        "difficulty": "Medium",
        "createdAt": _iso(_now()),
        "updatedAt": _iso(_now()),
    }
    _MILESTONES_STORE.insert(0, milestone)
    return {"success": True, "data": {"milestone": milestone}}


@router.get("/milestones/{milestone_id}")
async def get_milestone(
    milestone_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    for m in _MILESTONES_STORE:
        if m.get("_id") == milestone_id or m.get("id") == milestone_id:
            return {
                "success": True,
                "data": {
                    "milestone": m,
                    "stats": {"approved": 10, "pending": 2, "rejected": 0, "total": 12},
                    "recentCompletions": [],
                },
            }
    raise HTTPException(status_code=404, detail="Milestone not found")


@router.put("/milestones/{milestone_id}")
async def update_milestone(
    milestone_id: str,
    payload: MilestonePayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    for m in _MILESTONES_STORE:
        if m.get("_id") == milestone_id or m.get("id") == milestone_id:
            if payload.name:
                m["name"] = payload.name
            if payload.description:
                m["description"] = payload.description
            if payload.category:
                m["category"] = payload.category
            if payload.points is not None:
                try:
                    pts = int(payload.points)
                    m["points"] = pts
                    m["formattedPoints"] = f"{pts} pts"
                except Exception:
                    pass
            if payload.isActive is not None:
                m["isActive"] = payload.isActive
            m["updatedAt"] = _iso(_now())
            return {"success": True, "data": {"milestone": m}}
    raise HTTPException(status_code=404, detail="Milestone not found")


@router.delete("/milestones/{milestone_id}")
async def delete_milestone(
    milestone_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    global _MILESTONES_STORE
    _MILESTONES_STORE = [m for m in _MILESTONES_STORE if m.get("_id") != milestone_id and m.get("id") != milestone_id]
    return {"success": True, "message": "Milestone deleted successfully"}


@router.post("/milestones/{milestone_id}/toggle-status")
async def toggle_milestone_status(
    milestone_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    for m in _MILESTONES_STORE:
        if m.get("_id") == milestone_id or m.get("id") == milestone_id:
            m["isActive"] = not m.get("isActive", True)
            m["updatedAt"] = _iso(_now())
            return {"success": True, "data": {"milestone": m}}
    raise HTTPException(status_code=404, detail="Milestone not found")


from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional, Tuple
import uuid

from app.core.deps import CurrentUser, DbSession, church_id_from, resolve_actor
from app.models.church import Church
from app.models.detail import PastorDetails
from app.models.member import Member
from app.models.user import User
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm.attributes import flag_modified

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


async def _get_church(session: DbSession, church_id: str) -> Church:
    church = (
        await session.execute(select(Church).where(Church.id == church_id))
    ).scalar_one_or_none()
    if not church:
        raise HTTPException(status_code=404, detail="Church not found")
    return church


def _normalize_level(raw: Optional[str]) -> str:
    if not raw:
        return "New Convert"
    val = raw.strip().lower().replace("_", "-")
    mapping = {
        "new-convert": "New Convert",
        "new convert": "New Convert",
        "beginner": "New Convert",
        "growing": "Growing",
        "intermediate": "Growing",
        "mature": "Mature",
        "advanced": "Mature",
        "leader": "Leader",
    }
    return mapping.get(val, raw.strip())


def _normalize_disciple_status(raw: Optional[str]) -> str:
    if not raw:
        return "Active"
    val = raw.strip().lower()
    mapping = {
        "active": "Active",
        "graduated": "Graduated",
        "completed": "Graduated",
        "inactive": "Inactive",
        "paused": "Inactive",
        "discontinued": "Inactive",
    }
    return mapping.get(val, "Active")


def _normalize_class_level(raw: Optional[str]) -> str:
    if not raw:
        return "Beginner"
    val = raw.strip().lower()
    mapping = {
        "beginner": "Beginner",
        "intermediate": "Intermediate",
        "advanced": "Advanced",
    }
    return mapping.get(val, raw.strip().capitalize())


def _normalize_class_status(raw: Optional[str]) -> str:
    if not raw:
        return "Active"
    val = raw.strip().lower()
    mapping = {
        "active": "Active",
        "scheduled": "Scheduled",
        "completed": "Completed",
        "inactive": "Inactive",
    }
    return mapping.get(val, "Active")


class DisciplePayload(BaseModel):
    memberId: Optional[str] = None
    mentorId: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    startDate: Optional[str] = None
    currentLevel: Optional[str] = "New Convert"
    goals: Optional[str] = ""
    notes: Optional[str] = ""
    status: Optional[str] = "Active"
    progress: Optional[int] = None
    completedClasses: Optional[int] = None
    totalClasses: Optional[int] = None
    followUpCount: Optional[int] = None
    lastContact: Optional[str] = None
    enrolledClassIds: Optional[List[str]] = None


class DiscipleUpdatePayload(BaseModel):
    memberId: Optional[str] = None
    mentorId: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    startDate: Optional[str] = None
    currentLevel: Optional[str] = None
    goals: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    progress: Optional[int] = None
    completedClasses: Optional[int] = None
    totalClasses: Optional[int] = None
    followUpCount: Optional[int] = None
    lastContact: Optional[str] = None
    enrolledClassIds: Optional[List[str]] = None
    milestones: Optional[List[str]] = None


class FollowUpPayload(BaseModel):
    date: Optional[str] = None
    notes: Optional[str] = None
    progress: Optional[int] = None
    completedClasses: Optional[int] = None
    currentLevel: Optional[str] = None
    status: Optional[str] = None


class DiscipleshipClassPayload(BaseModel):
    name: str
    instructor: Optional[str] = ""
    instructorId: Optional[str] = None
    level: Optional[str] = "Beginner"
    duration: Optional[str] = "8 weeks"
    maxCapacity: Optional[int] = 15
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    schedule: Optional[str] = None
    description: Optional[str] = ""
    status: Optional[str] = "Active"
    progress: Optional[int] = 0
    studentIds: Optional[List[str]] = None


class MentorPayload(BaseModel):
    memberId: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    experience: Optional[str] = "3 years"
    specialization: Optional[str] = "New Converts"
    maxCapacity: Optional[int] = 5
    status: Optional[str] = "Active"
    notes: Optional[str] = ""


class MilestonePayload(BaseModel):
    name: str
    description: str
    category: Optional[str] = "General"
    points: Any = 10
    requirements: Optional[str] = None
    level: Optional[str] = "BEGINNER"
    isActive: Optional[bool] = True


async def _resolve_member_info(
    session: DbSession, member_id: Optional[str], church_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    if not member_id:
        return None
    stmt = (
        select(Member, User)
        .outerjoin(User, User.id == Member.user_id)
        .where(or_(Member.id == member_id, Member.user_id == member_id))
    )
    if church_id:
        stmt = stmt.where(Member.church_id == church_id)
    row = (await session.execute(stmt.limit(1))).first()
    if row:
        m, u = row
        first_name = (m.first_name if m and m.first_name else "") or ""
        last_name = (m.last_name if m and m.last_name else "") or ""
        if not first_name and not last_name and u and u.display_name:
            parts = u.display_name.strip().split(" ", 1)
            first_name = parts[0] if parts else "Member"
            last_name = parts[1] if len(parts) > 1 else ""
        full_name = f"{first_name} {last_name}".strip() or "Member"
        return {
            "_id": str(m.id),
            "id": str(m.id),
            "userId": str(m.user_id) if m.user_id else None,
            "firstName": first_name or "Member",
            "lastName": last_name,
            "fullName": full_name,
            "email": (m.email if m and m.email else (u.email if u else "")) or "",
            "phone": (
                m.phone_number
                if m and m.phone_number
                else (u.phone_number if u else "")
            )
            or "",
            "avatar": m.photo_url or "/placeholder.svg",
        }
    return None


async def _load_discipleship_context(
    session: DbSession, church_id: str
) -> Tuple[
    Church,
    Dict[str, Any],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
]:
    church = await _get_church(session, church_id)
    meta = dict(church.metadata_json or {})
    dirty = False

    # 1. Mentors (auto-seed from PastorDetails if not yet initialized)
    raw_mentors: Optional[List[Dict[str, Any]]] = meta.get("discipleship_mentors")
    if raw_mentors is None:
        raw_mentors = []
        pastor_rows = (
            await session.execute(
                select(PastorDetails, Member, User)
                .join(Member, Member.id == PastorDetails.member_id)
                .outerjoin(User, User.id == Member.user_id)
                .where(
                    Member.church_id == church_id,
                    Member.deleted_at.is_(None),
                )
            )
        ).all()
        for pd, m, u in pastor_rows:
            mid = f"mnt_{str(m.id)[:8]}"
            raw_mentors.append(
                {
                    "id": mid,
                    "_id": mid,
                    "memberId": str(m.id),
                    "userId": str(m.user_id) if m.user_id else None,
                    "name": m.full_name or (u.display_name if u else "Pastor"),
                    "email": m.email or (u.email if u else "") or "N/A",
                    "phone": m.phone_number or (u.phone_number if u else "") or "N/A",
                    "avatar": m.photo_url or "/placeholder.svg",
                    "experience": "5+ years",
                    "specialization": "New Converts & Spiritual Growth",
                    "maxCapacity": 6,
                    "status": "Active",
                    "notes": pd.biography or "",
                    "createdAt": _iso(_now()),
                }
            )
        meta["discipleship_mentors"] = raw_mentors
        dirty = True

    raw_disciples: List[Dict[str, Any]] = list(meta.get("discipleship_disciples") or [])
    raw_classes: List[Dict[str, Any]] = list(meta.get("discipleship_classes") or [])
    raw_milestones: Optional[List[Dict[str, Any]]] = meta.get("discipleship_milestones")
    if raw_milestones is None:
        raw_milestones = [
            {
                "_id": "m-1",
                "id": "m-1",
                "name": "Foundations of Faith",
                "description": "Complete the introductory course on basic Christian beliefs and church life.",
                "category": "Foundations",
                "points": 20,
                "requirements": "Attend all sessions and complete the study guide.",
                "isActive": True,
                "order": 1,
                "level": "BEGINNER",
                "completionCount": 0,
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
                "completionCount": 0,
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
                "requirements": "Leadership workshop and mentor evaluation.",
                "isActive": True,
                "order": 3,
                "level": "LEADER",
                "completionCount": 0,
                "difficulty": "Advanced",
                "formattedPoints": "50 pts",
                "createdAt": _iso(_now()),
                "updatedAt": _iso(_now()),
            },
        ]
        meta["discipleship_milestones"] = raw_milestones
        dirty = True

    if dirty:
        church.metadata_json = meta
        flag_modified(church, "metadata_json")
        await session.commit()

    # Build lookup maps for mentors
    mentor_by_any_id: Dict[str, Dict[str, Any]] = {}
    for m in raw_mentors:
        for key in (m.get("id"), m.get("_id"), m.get("memberId"), m.get("userId")):
            if key:
                mentor_by_any_id[str(key)] = m

    # Format disciples & track mentor mentee counts
    mentees_by_mentor_id: Dict[str, List[Dict[str, Any]]] = {}
    formatted_disciples: List[Dict[str, Any]] = []

    for d in raw_disciples:
        did = str(d.get("id") or d.get("_id"))
        mem_obj = d.get("memberId") if isinstance(d.get("memberId"), dict) else {}
        mnt_obj = d.get("mentorId") if isinstance(d.get("mentorId"), dict) else {}

        disciple_name = (
            d.get("name")
            or f"{mem_obj.get('firstName', '')} {mem_obj.get('lastName', '')}".strip()
            or "Disciple"
        )
        disciple_email = d.get("email") or mem_obj.get("email") or ""
        disciple_phone = d.get("phone") or mem_obj.get("phone") or ""
        disciple_avatar = d.get("avatar") or mem_obj.get("avatar") or "/placeholder.svg"

        raw_mentor_ref = (
            d.get("mentorRefId")
            or mnt_obj.get("id")
            or mnt_obj.get("_id")
            or (d.get("mentorId") if isinstance(d.get("mentorId"), str) else None)
        )
        matched_mentor = (
            mentor_by_any_id.get(str(raw_mentor_ref)) if raw_mentor_ref else None
        )
        if matched_mentor:
            mentor_name = matched_mentor.get("name") or "Unassigned"
            canonical_mentor_id = str(matched_mentor.get("id") or matched_mentor.get("_id"))
        else:
            mentor_name = (
                d.get("mentor")
                or f"{mnt_obj.get('firstName', '')} {mnt_obj.get('lastName', '')}".strip()
                or "Unassigned"
            )
            canonical_mentor_id = str(raw_mentor_ref) if raw_mentor_ref else ""

        level_label = _normalize_level(d.get("currentLevel"))
        status_label = _normalize_disciple_status(d.get("status"))
        enrolled_class_ids = list(d.get("enrolledClassIds") or [])
        total_classes = max(
            int(d.get("totalClasses") or 0),
            len(enrolled_class_ids) if enrolled_class_ids else 4,
        )
        completed_classes = min(
            total_classes, max(0, int(d.get("completedClasses") or 0))
        )
        progress_val = d.get("progress")
        if progress_val is None:
            progress_val = (
                round((completed_classes / total_classes) * 100)
                if total_classes > 0
                else 0
            )
        progress_val = max(0, min(100, int(progress_val)))

        if status_label == "Graduated" and progress_val < 100:
            progress_val = 100

        follow_up_count = max(0, int(d.get("followUpCount") or 0))
        last_contact = (
            d.get("lastContact")
            or (d.get("updatedAt") or d.get("startDate") or "")[:10]
            or "Not contacted"
        )
        start_date_str = (d.get("startDate") or _iso(_now()) or "")[:10]

        milestones_completed = list(d.get("milestonesCompleted") or [])
        milestone_names = list(
            d.get("milestones")
            or [
                m.get("name") if isinstance(m, dict) else str(m)
                for m in milestones_completed
            ]
        )

        formatted_d = {
            **d,
            "_id": did,
            "id": did,
            "churchId": church_id,
            "name": disciple_name,
            "email": disciple_email,
            "phone": disciple_phone,
            "avatar": disciple_avatar,
            "mentor": mentor_name,
            "mentorRefId": canonical_mentor_id,
            "memberId": mem_obj
            or {
                "_id": d.get("memberIdStr") or did,
                "id": d.get("memberIdStr") or did,
                "firstName": disciple_name.split(" ")[0],
                "lastName": " ".join(disciple_name.split(" ")[1:]),
                "email": disciple_email,
                "phone": disciple_phone,
            },
            "mentorId": mnt_obj
            or {
                "_id": canonical_mentor_id,
                "id": canonical_mentor_id,
                "firstName": mentor_name.split(" ")[0] if mentor_name else "",
                "lastName": " ".join(mentor_name.split(" ")[1:]) if mentor_name else "",
                "email": matched_mentor.get("email", "") if matched_mentor else "",
            },
            "startDate": start_date_str,
            "currentLevel": level_label,
            "status": status_label,
            "progress": progress_val,
            "completionPercentage": progress_val,
            "completedClasses": completed_classes,
            "totalClasses": total_classes,
            "enrolledClassIds": enrolled_class_ids,
            "followUpCount": follow_up_count,
            "lastContact": last_contact,
            "milestones": milestone_names,
            "milestonesCompleted": milestones_completed,
            "goals": d.get("goals") or "",
            "notes": d.get("notes") or "",
        }
        formatted_disciples.append(formatted_d)

        if canonical_mentor_id and status_label == "Active":
            mentees_by_mentor_id.setdefault(canonical_mentor_id, []).append(
                {
                    "id": did,
                    "name": disciple_name,
                    "level": level_label,
                }
            )

    disciple_by_id = {d["id"]: d for d in formatted_disciples}

    # Format classes
    formatted_classes: List[Dict[str, Any]] = []
    for c in raw_classes:
        cid = str(c.get("id") or c.get("_id"))
        student_ids = [
            sid
            for sid in (c.get("studentIds") or [])
            if sid in disciple_by_id
        ]
        # Also include disciples that reference this class in enrolledClassIds
        for d in formatted_disciples:
            if cid in (d.get("enrolledClassIds") or []) and d["id"] not in student_ids:
                student_ids.append(d["id"])

        students = [
            {
                "id": sid,
                "name": disciple_by_id[sid]["name"],
                "email": disciple_by_id[sid]["email"],
                "level": disciple_by_id[sid]["currentLevel"],
            }
            for sid in student_ids
            if sid in disciple_by_id
        ]
        max_cap = max(1, int(c.get("maxCapacity") or 15))
        formatted_classes.append(
            {
                **c,
                "id": cid,
                "_id": cid,
                "name": c.get("name") or "Discipleship Class",
                "instructor": c.get("instructor") or "Church Instructor",
                "instructorId": c.get("instructorId"),
                "level": _normalize_class_level(c.get("level")),
                "duration": c.get("duration") or "8 weeks",
                "currentStudents": len(students),
                "maxCapacity": max_cap,
                "studentIds": student_ids,
                "students": students,
                "startDate": (c.get("startDate") or _iso(_now()) or "")[:10],
                "endDate": (c.get("endDate") or "")[:10],
                "schedule": c.get("schedule") or "Sundays, 9:00 AM",
                "description": c.get("description") or "",
                "status": _normalize_class_status(c.get("status")),
                "progress": max(0, min(100, int(c.get("progress") or 0))),
            }
        )

    # Format mentors
    formatted_mentors: List[Dict[str, Any]] = []
    for m in raw_mentors:
        mid = str(m.get("id") or m.get("_id"))
        assigned_mentees = mentees_by_mentor_id.get(mid, [])
        max_cap = max(1, int(m.get("maxCapacity") or 5))
        formatted_mentors.append(
            {
                **m,
                "id": mid,
                "_id": mid,
                "name": m.get("name") or "Mentor",
                "email": m.get("email") or "N/A",
                "phone": m.get("phone") or "N/A",
                "avatar": m.get("avatar") or "/placeholder.svg",
                "experience": m.get("experience") or "3 years",
                "specialization": m.get("specialization") or "New Converts",
                "currentMentees": len(assigned_mentees),
                "mentees": assigned_mentees,
                "menteeIds": [mt["id"] for mt in assigned_mentees],
                "maxCapacity": max_cap,
                "status": m.get("status") or "Active",
                "notes": m.get("notes") or "",
            }
        )

    return (
        church,
        meta,
        formatted_disciples,
        formatted_classes,
        formatted_mentors,
        raw_milestones,
    )


# ---------------------------------------------------------------------------
# Stats (/church/discipleship/stats & /church/disciples/stats)
# ---------------------------------------------------------------------------
@router.get("/discipleship/stats")
@router.get("/disciples/stats")
async def get_discipleship_stats(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    _, _, disciples, classes, mentors, milestones = await _load_discipleship_context(
        session, actor.church_id
    )

    total_disciples = len(disciples)
    new_converts = sum(
        1 for d in disciples if d.get("currentLevel") == "New Convert"
    )
    active_disciples = sum(
        1 for d in disciples if d.get("status") == "Active"
    )
    graduated_disciples = sum(
        1 for d in disciples if d.get("status") == "Graduated"
    )
    inactive_disciples = sum(
        1 for d in disciples if d.get("status") == "Inactive"
    )

    active_classes = sum(
        1 for c in classes if c.get("status") == "Active"
    )
    active_mentors = sum(
        1 for m in mentors if (m.get("status") or "Active").lower() == "active"
    )

    avg_progress = (
        round(
            sum(int(d.get("progress") or 0) for d in disciples) / total_disciples
        )
        if total_disciples > 0
        else 0
    )
    avg_follow_ups = (
        round(
            sum(int(d.get("followUpCount") or 0) for d in disciples)
            / total_disciples,
            1,
        )
        if total_disciples > 0
        else 0
    )

    total_mentees = sum(int(m.get("currentMentees") or 0) for m in mentors)
    total_mentor_capacity = sum(int(m.get("maxCapacity") or 0) for m in mentors)
    mentor_utilization = (
        round((total_mentees / total_mentor_capacity) * 100)
        if total_mentor_capacity > 0
        else 0
    )

    return {
        "success": True,
        "data": {
            "newConverts": new_converts,
            "totalDisciples": total_disciples,
            "activeDisciples": active_disciples,
            "graduated": graduated_disciples,
            "graduatedDisciples": graduated_disciples,
            "inactiveDisciples": inactive_disciples,
            "activeClasses": active_classes,
            "totalClasses": len(classes),
            "activeMentors": active_mentors,
            "totalMentors": len(mentors),
            "averageProgress": avg_progress,
            "averageFollowUps": avg_follow_ups,
            "mentorUtilization": mentor_utilization,
            "totalMilestones": len(milestones),
        },
    }


# ---------------------------------------------------------------------------
# Disciples CRUD (/church/disciples)
# ---------------------------------------------------------------------------
@router.get("/disciples")
async def list_disciples(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=100),
    level: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    mentorId: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    _, _, disciples, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )

    items = disciples
    if search:
        s = search.strip().lower()
        items = [
            d
            for d in items
            if s
            in f"{d.get('name', '')} {d.get('email', '')} {d.get('mentor', '')} {d.get('currentLevel', '')} {d.get('goals', '')}".lower()
        ]
    if status and status.lower() not in ("all", ""):
        norm_st = _normalize_disciple_status(status)
        items = [
            d
            for d in items
            if d.get("status", "").lower() == norm_st.lower()
            or d.get("status", "").lower() == status.strip().lower()
        ]
    if level and level.lower() not in ("all", ""):
        norm_lvl = _normalize_level(level)
        items = [
            d
            for d in items
            if d.get("currentLevel", "").lower() == norm_lvl.lower()
            or d.get("currentLevel", "").lower() == level.strip().lower()
        ]
    if mentorId:
        items = [
            d for d in items if d.get("mentorRefId") == mentorId
        ]

    total = len(items)
    offset = (page - 1) * limit
    paginated = items[offset : offset + limit]
    pages = max(1, math.ceil(total / limit)) if limit else 1

    return {
        "success": True,
        "disciples": paginated,
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
    church, meta, _, _, mentors, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_disciples: List[Dict[str, Any]] = list(
        meta.get("discipleship_disciples") or []
    )
    raw_mentors: List[Dict[str, Any]] = list(
        meta.get("discipleship_mentors") or []
    )

    d_id = f"dsc_{uuid.uuid4().hex[:10]}"
    mem_info = await _resolve_member_info(
        session, payload.memberId, actor.church_id
    )
    if not mem_info:
        full_name = (payload.name or "New Disciple").strip()
        parts = full_name.split(" ", 1)
        mem_info = {
            "_id": payload.memberId or d_id,
            "id": payload.memberId or d_id,
            "firstName": parts[0],
            "lastName": parts[1] if len(parts) > 1 else "",
            "fullName": full_name,
            "email": (payload.email or "").strip(),
            "phone": (payload.phone or "").strip(),
            "avatar": "/placeholder.svg",
        }

    # Resolve mentor: check existing mentors first, otherwise resolve member and register as mentor
    mentor_ref_id = ""
    mentor_info: Dict[str, Any] = {}
    if payload.mentorId:
        existing_mnt = next(
            (
                m
                for m in raw_mentors
                if m.get("id") == payload.mentorId
                or m.get("_id") == payload.mentorId
                or m.get("memberId") == payload.mentorId
                or m.get("userId") == payload.mentorId
            ),
            None,
        )
        if existing_mnt:
            mentor_ref_id = str(existing_mnt.get("id") or existing_mnt.get("_id"))
            m_parts = (existing_mnt.get("name") or "Mentor").split(" ", 1)
            mentor_info = {
                "_id": mentor_ref_id,
                "id": mentor_ref_id,
                "firstName": m_parts[0],
                "lastName": m_parts[1] if len(m_parts) > 1 else "",
                "fullName": existing_mnt.get("name") or "Mentor",
                "email": existing_mnt.get("email") or "",
                "phone": existing_mnt.get("phone") or "",
            }
        else:
            resolved_mnt = await _resolve_member_info(
                session, payload.mentorId, actor.church_id
            )
            if resolved_mnt:
                new_mid = f"mnt_{uuid.uuid4().hex[:8]}"
                new_mentor_entry = {
                    "id": new_mid,
                    "_id": new_mid,
                    "memberId": resolved_mnt["id"],
                    "userId": resolved_mnt.get("userId"),
                    "name": resolved_mnt["fullName"],
                    "email": resolved_mnt.get("email") or "N/A",
                    "phone": resolved_mnt.get("phone") or "N/A",
                    "avatar": resolved_mnt.get("avatar") or "/placeholder.svg",
                    "experience": "3+ years",
                    "specialization": "Discipleship & Mentoring",
                    "maxCapacity": 5,
                    "status": "Active",
                    "createdAt": _iso(_now()),
                }
                raw_mentors.append(new_mentor_entry)
                meta["discipleship_mentors"] = raw_mentors
                mentor_ref_id = new_mid
                mentor_info = {
                    "_id": new_mid,
                    "id": new_mid,
                    "firstName": resolved_mnt["firstName"],
                    "lastName": resolved_mnt["lastName"],
                    "fullName": resolved_mnt["fullName"],
                    "email": resolved_mnt.get("email") or "",
                    "phone": resolved_mnt.get("phone") or "",
                }

    now_str = _iso(_now())
    start_date = (payload.startDate or now_str or "")[:10]
    level_label = _normalize_level(payload.currentLevel)
    status_label = _normalize_disciple_status(payload.status)

    disciple_record = {
        "_id": d_id,
        "id": d_id,
        "churchId": actor.church_id,
        "branchId": (
            str(actor.member.branch_id)
            if actor.member and actor.member.branch_id
            else "main"
        ),
        "name": mem_info.get("fullName") or "Disciple",
        "email": mem_info.get("email") or "",
        "phone": mem_info.get("phone") or "",
        "avatar": mem_info.get("avatar") or "/placeholder.svg",
        "memberIdStr": mem_info.get("id"),
        "memberId": mem_info,
        "mentorRefId": mentor_ref_id,
        "mentor": mentor_info.get("fullName") or "Unassigned",
        "mentorId": mentor_info,
        "startDate": start_date,
        "currentLevel": level_label,
        "status": status_label,
        "goals": payload.goals or "",
        "notes": payload.notes or "",
        "progress": payload.progress if payload.progress is not None else 15,
        "completedClasses": payload.completedClasses or 0,
        "totalClasses": payload.totalClasses or 4,
        "enrolledClassIds": list(payload.enrolledClassIds or []),
        "followUpCount": payload.followUpCount if payload.followUpCount is not None else 1,
        "lastContact": payload.lastContact or start_date,
        "milestones": [],
        "milestonesCompleted": [],
        "durationInDays": 1,
        "completionPercentage": payload.progress if payload.progress is not None else 15,
        "createdAt": now_str,
        "updatedAt": now_str,
    }
    raw_disciples.insert(0, disciple_record)
    meta["discipleship_disciples"] = raw_disciples
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    queue_audit(
        background_tasks,
        request,
        "CREATE_DISCIPLE",
        "DISCIPLE",
        d_id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    _, _, formatted_disciples, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    created = next(
        (d for d in formatted_disciples if d["id"] == d_id), disciple_record
    )
    return {"success": True, "data": {"disciple": created}}


@router.get("/disciples/{disciple_id}")
async def get_disciple(
    disciple_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    _, _, disciples, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    for d in disciples:
        if d.get("_id") == disciple_id or d.get("id") == disciple_id:
            return {
                "success": True,
                "data": {
                    "disciple": d,
                    "progressRecords": d.get("followUpHistory") or [],
                },
            }
    raise HTTPException(status_code=404, detail="Disciple record not found")


@router.put("/disciples/{disciple_id}")
@router.patch("/disciples/{disciple_id}")
async def update_disciple(
    disciple_id: str,
    payload: DiscipleUpdatePayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_disciples: List[Dict[str, Any]] = list(
        meta.get("discipleship_disciples") or []
    )
    raw_mentors: List[Dict[str, Any]] = list(
        meta.get("discipleship_mentors") or []
    )

    target = next(
        (
            d
            for d in raw_disciples
            if d.get("_id") == disciple_id or d.get("id") == disciple_id
        ),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail="Disciple record not found")

    if payload.name is not None and payload.name.strip():
        target["name"] = payload.name.strip()
    if payload.email is not None:
        target["email"] = payload.email.strip()
    if payload.phone is not None:
        target["phone"] = payload.phone.strip()
    if payload.startDate is not None:
        target["startDate"] = payload.startDate[:10]
    if payload.goals is not None:
        target["goals"] = payload.goals
    if payload.notes is not None:
        target["notes"] = payload.notes
    if payload.currentLevel is not None:
        target["currentLevel"] = _normalize_level(payload.currentLevel)
    if payload.status is not None:
        target["status"] = _normalize_disciple_status(payload.status)
    if payload.progress is not None:
        target["progress"] = max(0, min(100, int(payload.progress)))
        target["completionPercentage"] = target["progress"]
    if payload.completedClasses is not None:
        target["completedClasses"] = max(0, int(payload.completedClasses))
    if payload.totalClasses is not None:
        target["totalClasses"] = max(1, int(payload.totalClasses))
    if payload.followUpCount is not None:
        target["followUpCount"] = max(0, int(payload.followUpCount))
    if payload.lastContact is not None:
        target["lastContact"] = payload.lastContact
    if payload.enrolledClassIds is not None:
        target["enrolledClassIds"] = list(payload.enrolledClassIds)
    if payload.milestones is not None:
        target["milestones"] = list(payload.milestones)

    if payload.mentorId is not None:
        if payload.mentorId == "" or payload.mentorId == "unassigned":
            target["mentorRefId"] = ""
            target["mentor"] = "Unassigned"
        else:
            existing_mnt = next(
                (
                    m
                    for m in raw_mentors
                    if m.get("id") == payload.mentorId
                    or m.get("_id") == payload.mentorId
                    or m.get("memberId") == payload.mentorId
                    or m.get("userId") == payload.mentorId
                ),
                None,
            )
            if existing_mnt:
                target["mentorRefId"] = str(
                    existing_mnt.get("id") or existing_mnt.get("_id")
                )
                target["mentor"] = existing_mnt.get("name") or "Mentor"
            else:
                resolved_mnt = await _resolve_member_info(
                    session, payload.mentorId, actor.church_id
                )
                if resolved_mnt:
                    new_mid = f"mnt_{uuid.uuid4().hex[:8]}"
                    raw_mentors.append(
                        {
                            "id": new_mid,
                            "_id": new_mid,
                            "memberId": resolved_mnt["id"],
                            "userId": resolved_mnt.get("userId"),
                            "name": resolved_mnt["fullName"],
                            "email": resolved_mnt.get("email") or "N/A",
                            "phone": resolved_mnt.get("phone") or "N/A",
                            "avatar": resolved_mnt.get("avatar") or "/placeholder.svg",
                            "experience": "3+ years",
                            "specialization": "Discipleship & Mentoring",
                            "maxCapacity": 5,
                            "status": "Active",
                            "createdAt": _iso(_now()),
                        }
                    )
                    meta["discipleship_mentors"] = raw_mentors
                    target["mentorRefId"] = new_mid
                    target["mentor"] = resolved_mnt["fullName"]

    target["updatedAt"] = _iso(_now())
    meta["discipleship_disciples"] = raw_disciples
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, formatted_disciples, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    updated = next(
        (d for d in formatted_disciples if d["id"] == disciple_id), target
    )
    return {"success": True, "data": {"disciple": updated}}


@router.post("/disciples/{disciple_id}/follow-up")
async def record_disciple_follow_up(
    disciple_id: str,
    payload: FollowUpPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_disciples: List[Dict[str, Any]] = list(
        meta.get("discipleship_disciples") or []
    )
    target = next(
        (
            d
            for d in raw_disciples
            if d.get("_id") == disciple_id or d.get("id") == disciple_id
        ),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail="Disciple record not found")

    today_str = (payload.date or _iso(_now()) or "")[:10]
    target["followUpCount"] = max(0, int(target.get("followUpCount") or 0)) + 1
    target["lastContact"] = today_str
    if payload.notes:
        existing_notes = target.get("notes") or ""
        target["notes"] = (
            f"{existing_notes}\n[{today_str}] {payload.notes}".strip()
            if existing_notes
            else f"[{today_str}] {payload.notes}"
        )
    if payload.progress is not None:
        target["progress"] = max(0, min(100, int(payload.progress)))
    if payload.completedClasses is not None:
        target["completedClasses"] = max(0, int(payload.completedClasses))
    if payload.currentLevel is not None:
        target["currentLevel"] = _normalize_level(payload.currentLevel)
    if payload.status is not None:
        target["status"] = _normalize_disciple_status(payload.status)

    target["updatedAt"] = _iso(_now())
    meta["discipleship_disciples"] = raw_disciples
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, formatted_disciples, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    updated = next(
        (d for d in formatted_disciples if d["id"] == disciple_id), target
    )
    return {
        "success": True,
        "message": "Follow-up recorded successfully",
        "data": {"disciple": updated},
    }


@router.delete("/disciples/{disciple_id}")
async def delete_disciple(
    disciple_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_disciples: List[Dict[str, Any]] = list(
        meta.get("discipleship_disciples") or []
    )
    meta["discipleship_disciples"] = [
        d
        for d in raw_disciples
        if d.get("_id") != disciple_id and d.get("id") != disciple_id
    ]
    # Also remove from any class studentIds
    raw_classes: List[Dict[str, Any]] = list(
        meta.get("discipleship_classes") or []
    )
    for c in raw_classes:
        if "studentIds" in c and isinstance(c["studentIds"], list):
            c["studentIds"] = [
                sid for sid in c["studentIds"] if sid != disciple_id
            ]
    meta["discipleship_classes"] = raw_classes

    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True, "message": "Disciple record deleted successfully"}


@router.post("/disciples/{disciple_id}/milestones")
async def add_disciple_milestone(
    disciple_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, raw_milestones = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_disciples: List[Dict[str, Any]] = list(
        meta.get("discipleship_disciples") or []
    )
    milestone_id = payload.get("milestoneId")
    for d in raw_disciples:
        if d.get("_id") == disciple_id or d.get("id") == disciple_id:
            found_m = next(
                (
                    m
                    for m in raw_milestones
                    if m.get("_id") == milestone_id or m.get("id") == milestone_id
                ),
                None,
            )
            if found_m:
                d.setdefault("milestonesCompleted", []).append(
                    {
                        "_id": found_m["_id"],
                        "name": found_m["name"],
                        "points": found_m["points"],
                        "category": found_m["category"],
                    }
                )
                d["progress"] = min(100, int(d.get("progress") or 0) + 25)
                meta["discipleship_disciples"] = raw_disciples
                church.metadata_json = meta
                flag_modified(church, "metadata_json")
                await session.commit()
            return {"success": True, "message": "Milestone recorded successfully"}
    return {"success": True, "message": "Milestone recorded"}


# ---------------------------------------------------------------------------
# Discipleship Classes (/church/discipleship/classes)
# ---------------------------------------------------------------------------
@router.get("/discipleship/classes")
async def list_discipleship_classes(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    search: str = Query(""),
    status: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    _, _, _, classes, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    if search:
        q = search.strip().lower()
        classes = [
            c
            for c in classes
            if q
            in f"{c.get('name', '')} {c.get('instructor', '')} {c.get('level', '')}".lower()
        ]
    if status and status.lower() not in ("all", ""):
        classes = [
            c
            for c in classes
            if (c.get("status") or "").lower() == status.strip().lower()
        ]
    return {
        "success": True,
        "classes": classes,
        "data": classes,
    }


@router.post("/discipleship/classes")
async def create_discipleship_class(
    payload: DiscipleshipClassPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_classes: List[Dict[str, Any]] = list(
        meta.get("discipleship_classes") or []
    )

    cid = f"cls_{uuid.uuid4().hex[:10]}"
    now_iso = _iso(_now())
    new_cls = {
        "id": cid,
        "_id": cid,
        "name": payload.name.strip(),
        "instructor": (payload.instructor or "Church Instructor").strip(),
        "instructorId": payload.instructorId,
        "level": _normalize_class_level(payload.level),
        "duration": (payload.duration or "8 weeks").strip(),
        "maxCapacity": max(1, int(payload.maxCapacity or 15)),
        "studentIds": list(payload.studentIds or []),
        "startDate": (payload.startDate or now_iso or "")[:10],
        "endDate": (payload.endDate or "")[:10],
        "schedule": payload.schedule or "Sundays, 9:00 AM",
        "description": payload.description or "",
        "status": _normalize_class_status(payload.status),
        "progress": max(0, min(100, int(payload.progress or 0))),
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }
    raw_classes.insert(0, new_cls)
    meta["discipleship_classes"] = raw_classes
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, _, formatted_classes, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    created = next((c for c in formatted_classes if c["id"] == cid), new_cls)
    return {
        "success": True,
        "message": "Discipleship class created successfully",
        "data": created,
    }


@router.put("/discipleship/classes/{class_id}")
@router.patch("/discipleship/classes/{class_id}")
async def update_discipleship_class(
    class_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_classes: List[Dict[str, Any]] = list(
        meta.get("discipleship_classes") or []
    )
    target = next(
        (
            c
            for c in raw_classes
            if c.get("id") == class_id or c.get("_id") == class_id
        ),
        None,
    )
    if not target:
        raise HTTPException(
            status_code=404, detail="Discipleship class not found"
        )

    for k in (
        "name",
        "instructor",
        "instructorId",
        "duration",
        "startDate",
        "endDate",
        "schedule",
        "description",
    ):
        if k in payload and payload[k] is not None:
            target[k] = payload[k]
    if "level" in payload and payload["level"] is not None:
        target["level"] = _normalize_class_level(payload["level"])
    if "status" in payload and payload["status"] is not None:
        target["status"] = _normalize_class_status(payload["status"])
    if "maxCapacity" in payload and payload["maxCapacity"] is not None:
        target["maxCapacity"] = max(1, int(payload["maxCapacity"] or 1))
    if "progress" in payload and payload["progress"] is not None:
        target["progress"] = max(0, min(100, int(payload["progress"] or 0)))
    if "studentIds" in payload and payload["studentIds"] is not None:
        target["studentIds"] = list(payload["studentIds"])

    target["updatedAt"] = _iso(_now())
    meta["discipleship_classes"] = raw_classes
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, _, formatted_classes, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    updated = next(
        (c for c in formatted_classes if c["id"] == class_id), target
    )
    return {
        "success": True,
        "message": "Discipleship class updated successfully",
        "data": updated,
    }


@router.post("/discipleship/classes/{class_id}/enroll")
async def enroll_students_in_class(
    class_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_classes: List[Dict[str, Any]] = list(
        meta.get("discipleship_classes") or []
    )
    raw_disciples: List[Dict[str, Any]] = list(
        meta.get("discipleship_disciples") or []
    )
    target = next(
        (
            c
            for c in raw_classes
            if c.get("id") == class_id or c.get("_id") == class_id
        ),
        None,
    )
    if not target:
        raise HTTPException(
            status_code=404, detail="Discipleship class not found"
        )

    student_ids = [
        str(sid) for sid in (payload.get("discipleIds") or payload.get("studentIds") or []) if sid
    ]
    target["studentIds"] = student_ids
    target["updatedAt"] = _iso(_now())

    # Sync enrolledClassIds on disciples
    for d in raw_disciples:
        did = str(d.get("id") or d.get("_id"))
        enrolled = list(d.get("enrolledClassIds") or [])
        if did in student_ids and class_id not in enrolled:
            enrolled.append(class_id)
        elif did not in student_ids and class_id in enrolled:
            enrolled.remove(class_id)
        d["enrolledClassIds"] = enrolled

    meta["discipleship_classes"] = raw_classes
    meta["discipleship_disciples"] = raw_disciples
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, _, formatted_classes, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    updated = next(
        (c for c in formatted_classes if c["id"] == class_id), target
    )
    return {
        "success": True,
        "message": "Class enrollment updated successfully",
        "data": updated,
    }


@router.delete("/discipleship/classes/{class_id}")
async def delete_discipleship_class(
    class_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_classes: List[Dict[str, Any]] = list(
        meta.get("discipleship_classes") or []
    )
    meta["discipleship_classes"] = [
        c
        for c in raw_classes
        if c.get("id") != class_id and c.get("_id") != class_id
    ]
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {
        "success": True,
        "message": "Discipleship class deleted successfully",
    }


# ---------------------------------------------------------------------------
# Mentors CRUD & Assignment (/church/discipleship/mentors)
# ---------------------------------------------------------------------------
@router.get("/discipleship/mentors")
async def list_mentors(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    search: str = Query(""),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    _, _, _, _, mentors, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    if search:
        q = search.strip().lower()
        mentors = [
            m
            for m in mentors
            if q
            in f"{m.get('name', '')} {m.get('email', '')} {m.get('specialization', '')}".lower()
        ]
    return {
        "success": True,
        "mentors": mentors,
        "data": mentors,
    }


@router.post("/discipleship/mentors")
async def create_mentor(
    payload: MentorPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_mentors: List[Dict[str, Any]] = list(
        meta.get("discipleship_mentors") or []
    )

    resolved = await _resolve_member_info(
        session, payload.memberId, actor.church_id
    )
    mid = f"mnt_{uuid.uuid4().hex[:8]}"
    name_val = (
        payload.name
        or (resolved["fullName"] if resolved else "")
        or "Mentor"
    ).strip()
    email_val = (
        payload.email
        or (resolved["email"] if resolved else "")
        or "N/A"
    ).strip()
    phone_val = (
        payload.phone
        or (resolved["phone"] if resolved else "")
        or "N/A"
    ).strip()

    new_mentor = {
        "id": mid,
        "_id": mid,
        "memberId": resolved["id"] if resolved else payload.memberId,
        "userId": resolved.get("userId") if resolved else None,
        "name": name_val,
        "email": email_val,
        "phone": phone_val,
        "avatar": resolved.get("avatar") if resolved else "/placeholder.svg",
        "experience": (payload.experience or "3 years").strip(),
        "specialization": (payload.specialization or "New Converts").strip(),
        "maxCapacity": max(1, int(payload.maxCapacity or 5)),
        "status": payload.status or "Active",
        "notes": payload.notes or "",
        "createdAt": _iso(_now()),
    }
    raw_mentors.insert(0, new_mentor)
    meta["discipleship_mentors"] = raw_mentors
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, _, _, formatted_mentors, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    created = next(
        (m for m in formatted_mentors if m["id"] == mid), new_mentor
    )
    return {
        "success": True,
        "message": "Mentor added successfully",
        "data": created,
    }


@router.put("/discipleship/mentors/{mentor_id}")
@router.patch("/discipleship/mentors/{mentor_id}")
async def update_mentor(
    mentor_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_mentors: List[Dict[str, Any]] = list(
        meta.get("discipleship_mentors") or []
    )
    target = next(
        (
            m
            for m in raw_mentors
            if m.get("id") == mentor_id or m.get("_id") == mentor_id
        ),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail="Mentor not found")

    for k in (
        "name",
        "email",
        "phone",
        "experience",
        "specialization",
        "status",
        "notes",
    ):
        if k in payload and payload[k] is not None:
            target[k] = payload[k]
    if "maxCapacity" in payload and payload["maxCapacity"] is not None:
        target["maxCapacity"] = max(1, int(payload["maxCapacity"] or 1))

    target["updatedAt"] = _iso(_now())
    meta["discipleship_mentors"] = raw_mentors
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, _, _, formatted_mentors, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    updated = next(
        (m for m in formatted_mentors if m["id"] == mentor_id), target
    )
    return {
        "success": True,
        "message": "Mentor updated successfully",
        "data": updated,
    }


@router.post("/discipleship/mentors/{mentor_id}/assign")
async def assign_mentees_to_mentor(
    mentor_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_mentors: List[Dict[str, Any]] = list(
        meta.get("discipleship_mentors") or []
    )
    raw_disciples: List[Dict[str, Any]] = list(
        meta.get("discipleship_disciples") or []
    )
    target = next(
        (
            m
            for m in raw_mentors
            if m.get("id") == mentor_id or m.get("_id") == mentor_id
        ),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail="Mentor not found")

    disciple_ids = [
        str(did)
        for did in (payload.get("discipleIds") or payload.get("menteeIds") or [])
        if did
    ]
    mentor_name = target.get("name") or "Mentor"

    for d in raw_disciples:
        did = str(d.get("id") or d.get("_id"))
        if did in disciple_ids:
            d["mentorRefId"] = mentor_id
            d["mentor"] = mentor_name
        elif d.get("mentorRefId") == mentor_id:
            d["mentorRefId"] = ""
            d["mentor"] = "Unassigned"

    meta["discipleship_disciples"] = raw_disciples
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, _, _, formatted_mentors, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    updated = next(
        (m for m in formatted_mentors if m["id"] == mentor_id), target
    )
    return {
        "success": True,
        "message": "Mentees assigned to mentor successfully",
        "data": updated,
    }


@router.delete("/discipleship/mentors/{mentor_id}")
async def delete_mentor(
    mentor_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, _ = await _load_discipleship_context(
        session, actor.church_id
    )
    raw_mentors: List[Dict[str, Any]] = list(
        meta.get("discipleship_mentors") or []
    )
    raw_disciples: List[Dict[str, Any]] = list(
        meta.get("discipleship_disciples") or []
    )
    meta["discipleship_mentors"] = [
        m
        for m in raw_mentors
        if m.get("id") != mentor_id and m.get("_id") != mentor_id
    ]
    for d in raw_disciples:
        if d.get("mentorRefId") == mentor_id:
            d["mentorRefId"] = ""
            d["mentor"] = "Unassigned"
    meta["discipleship_disciples"] = raw_disciples
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True, "message": "Mentor removed successfully"}


# ---------------------------------------------------------------------------
# Milestones (/church/milestones)
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
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    _, _, _, _, _, raw_milestones = await _load_discipleship_context(
        session, actor.church_id
    )
    items = list(raw_milestones)
    if search:
        s = search.lower()
        items = [
            m
            for m in items
            if s in m.get("name", "").lower()
            or s in m.get("description", "").lower()
        ]
    if category:
        items = [
            m
            for m in items
            if m.get("category", "").lower() == category.lower()
        ]

    total = len(items)
    offset = (page - 1) * limit
    paginated = items[offset : offset + limit]
    pages = max(1, math.ceil(total / limit)) if limit else 1

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
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, raw_milestones = await _load_discipleship_context(
        session, actor.church_id
    )
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
        "order": len(raw_milestones) + 1,
        "level": payload.level or "BEGINNER",
        "completionCount": 0,
        "difficulty": "Medium",
        "createdAt": _iso(_now()),
        "updatedAt": _iso(_now()),
    }
    raw_milestones.insert(0, milestone)
    meta["discipleship_milestones"] = raw_milestones
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True, "data": {"milestone": milestone}}


@router.get("/milestones/{milestone_id}")
async def get_milestone(
    milestone_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    _, _, _, _, _, raw_milestones = await _load_discipleship_context(
        session, actor.church_id
    )
    for m in raw_milestones:
        if m.get("_id") == milestone_id or m.get("id") == milestone_id:
            return {
                "success": True,
                "data": {
                    "milestone": m,
                    "stats": {
                        "approved": m.get("completionCount", 0),
                        "pending": 0,
                        "rejected": 0,
                        "total": m.get("completionCount", 0),
                    },
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
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, raw_milestones = await _load_discipleship_context(
        session, actor.church_id
    )
    for m in raw_milestones:
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
            meta["discipleship_milestones"] = raw_milestones
            church.metadata_json = meta
            flag_modified(church, "metadata_json")
            await session.commit()
            return {"success": True, "data": {"milestone": m}}
    raise HTTPException(status_code=404, detail="Milestone not found")


@router.delete("/milestones/{milestone_id}")
async def delete_milestone(
    milestone_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, raw_milestones = await _load_discipleship_context(
        session, actor.church_id
    )
    meta["discipleship_milestones"] = [
        m
        for m in raw_milestones
        if m.get("_id") != milestone_id and m.get("id") != milestone_id
    ]
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True, "message": "Milestone deleted successfully"}


@router.post("/milestones/{milestone_id}/toggle-status")
async def toggle_milestone_status(
    milestone_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church, meta, _, _, _, raw_milestones = await _load_discipleship_context(
        session, actor.church_id
    )
    for m in raw_milestones:
        if m.get("_id") == milestone_id or m.get("id") == milestone_id:
            m["isActive"] = not m.get("isActive", True)
            m["updatedAt"] = _iso(_now())
            meta["discipleship_milestones"] = raw_milestones
            church.metadata_json = meta
            flag_modified(church, "metadata_json")
            await session.commit()
            return {"success": True, "data": {"milestone": m}}
    raise HTTPException(status_code=404, detail="Milestone not found")

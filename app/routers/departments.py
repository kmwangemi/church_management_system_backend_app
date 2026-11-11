from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Dict, List, Optional
import uuid

from app.core.deps import CurrentUser, DbSession, church_id_from, require, resolve_actor
from app.enums import MemberRole
from app.models.auth import Account
from app.models.church import Branch
from app.models.group import Department, DepartmentMember
from app.models.member import Member
from app.models.user import User
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_dept_desc(raw_description: Optional[str]) -> tuple[str, dict]:
    if not raw_description:
        return "", {}
    meta = {}
    clean_desc = raw_description
    match = re.search(r"<!--dept_meta:(.*?)-->\n?", raw_description, re.DOTALL)
    if match:
        try:
            meta = json.loads(match.group(1))
            clean_desc = (
                raw_description[: match.start()] + raw_description[match.end() :]
            ).strip()
        except Exception:
            pass
    return clean_desc, meta


def _build_dept_desc(clean_desc: Optional[str], meta: dict) -> str:
    desc = clean_desc or ""
    meta_json = json.dumps(meta)
    return f"<!--dept_meta:{meta_json}-->\n{desc}"


async def _resolve_member_brief(
    session: DbSession,
    raw_id: Optional[str],
    church_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not raw_id:
        return None
    conds = [or_(Member.id == raw_id, Member.user_id == raw_id)]
    if church_id:
        conds.append(Member.church_id == church_id)
    row = (
        await session.execute(
            select(Member, User)
            .outerjoin(User, User.id == Member.user_id)
            .where(*conds)
            .limit(1)
        )
    ).first()
    if not row:
        return None
    m, u = row
    first_name = (m.first_name if m and m.first_name else "") or ""
    last_name = (m.last_name if m and m.last_name else "") or ""
    if not first_name and not last_name and u and u.display_name:
        parts = u.display_name.strip().split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""
    full_name = f"{first_name} {last_name}".strip()
    return {
        "_id": str(m.id),
        "id": str(m.id),
        "firstName": first_name,
        "lastName": last_name,
        "name": full_name,
        "fullName": full_name,
        "email": (m.email if m and m.email else (u.email if u else "")) or "",
        "phoneNumber": (
            (m.phone_number if m and m.phone_number else (u.phone_number if u and hasattr(u, "phone_number") else ""))
            or ""
        ),
        "profilePictureUrl": (
            (m.photo_url if m and m.photo_url else (u.profile_picture_url if u and hasattr(u, "profile_picture_url") else ""))
            or ""
        ),
        "isLocked": False,
    }


async def _format_dept(session: DbSession, dept: Department) -> Dict[str, Any]:
    leader_data = None
    if dept.leader_member_id:
        lm = (
            await session.execute(
                select(Member, User)
                .outerjoin(User, User.id == Member.user_id)
                .where(
                    (Member.id == dept.leader_member_id)
                    | (Member.user_id == dept.leader_member_id)
                )
            )
        ).first()
        if lm:
            m, u = lm
            first_name = (m.first_name if m and m.first_name else "") or ""
            last_name = (m.last_name if m and m.last_name else "") or ""
            if not first_name and not last_name and u and u.display_name:
                parts = u.display_name.strip().split(" ", 1)
                first_name = parts[0] if parts else ""
                last_name = parts[1] if len(parts) > 1 else ""
            email = (m.email if m and m.email else (u.email if u else "")) or ""
            phone = (
                (m.phone_number if m and m.phone_number else (u.phone_number if u and hasattr(u, "phone_number") else ""))
                or ""
            )
            image = (
                (m.photo_url if m and m.photo_url else (u.profile_picture_url if u and hasattr(u, "profile_picture_url") else ""))
                or ""
            )
            leader_data = {
                "_id": str(m.id),
                "id": str(m.id),
                "userId": str(u.id) if u else str(m.id),
                "firstName": first_name,
                "lastName": last_name,
                "name": f"{first_name} {last_name}".strip(),
                "email": email,
                "phoneNumber": phone,
                "profilePictureUrl": image,
                "membershipNumber": m.membership_number or "",
            }
        else:
            usr = (
                await session.execute(
                    select(User).where(User.id == dept.leader_member_id)
                )
            ).scalar_one_or_none()
            if usr:
                parts = (usr.display_name or "").strip().split(" ", 1)
                first_name = parts[0] if parts else ""
                last_name = parts[1] if len(parts) > 1 else ""
                leader_data = {
                    "_id": str(usr.id),
                    "id": str(usr.id),
                    "userId": str(usr.id),
                    "firstName": first_name,
                    "lastName": last_name,
                    "name": f"{first_name} {last_name}".strip() or (usr.display_name or ""),
                    "email": usr.email or "",
                    "phoneNumber": usr.phone_number if hasattr(usr, "phone_number") and usr.phone_number else "",
                    "profilePictureUrl": usr.profile_picture_url if hasattr(usr, "profile_picture_url") and usr.profile_picture_url else "",
                    "membershipNumber": "",
                }

    member_count = (
        await session.execute(
            select(func.count(DepartmentMember.id)).where(
                DepartmentMember.department_id == dept.id
            )
        )
    ).scalar() or 0

    clean_desc, meta = _parse_dept_desc(dept.description)
    total_budget = meta.get("totalBudget", 0)
    try:
        total_budget = float(total_budget)
    except (ValueError, TypeError):
        total_budget = 0.0

    category = meta.get("category", "ministry")
    location = meta.get("location", "Main Campus")
    established_date = meta.get("establishedDate", _iso(dept.created_at))
    meeting_day = meta.get("meetingDay", [])
    meeting_time = meta.get("meetingTime", [])
    activities = meta.get("activities", []) if isinstance(meta.get("activities"), list) else []
    expenses = meta.get("expenses", []) if isinstance(meta.get("expenses"), list) else []
    total_spent = sum(float(e.get("amount") or 0) for e in expenses)

    now = _now()
    current_ym = now.strftime("%Y-%m")
    monthly_acts = sum(
        1
        for a in activities
        if str(a.get("date") or a.get("createdAt") or "")[:7] == current_ym
    )

    branch_data = None
    branch_id_val = getattr(dept, "branch_id", None) or meta.get("branchId")
    if branch_id_val:
        br = (
            await session.execute(
                select(Branch).where(Branch.id == str(branch_id_val))
            )
        ).scalar_one_or_none()
        if br:
            branch_data = {
                "_id": str(br.id),
                "id": str(br.id),
                "branchName": br.name,
            }

    # Growth calculation based on members joined in current vs prior month
    growth = int(meta.get("growth", 0) or 0)

    return {
        "_id": str(dept.id),
        "id": str(dept.id),
        "churchId": str(dept.church_id),
        "branchId": branch_data,
        "departmentName": dept.name,
        "name": dept.name,
        "description": clean_desc,
        "isActive": dept.is_active,
        "is_active": dept.is_active,
        "isDeleted": False,
        "leaderId": leader_data,
        "leader": leader_data,
        "members": member_count,
        "memberCount": member_count,
        "totalBudget": total_budget,
        "budget": total_budget,
        "spentBudget": total_spent,
        "growth": growth,
        "category": category,
        "location": location,
        "establishedDate": established_date,
        "meetingDay": meeting_day,
        "meetingTime": meeting_time,
        "_count": {
            "members": member_count,
            "activities": len(activities),
            "monthlyActivities": monthly_acts,
            "expenses": len(expenses),
        },
        "createdAt": _iso(dept.created_at),
        "updatedAt": _iso(dept.updated_at),
    }


class DepartmentPayload(BaseModel):
    departmentName: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    leaderId: Optional[str] = None
    branchId: Optional[str] = None
    category: Optional[str] = None
    establishedDate: Optional[str] = None
    location: Optional[str] = None
    totalBudget: Optional[Any] = None
    meetingDay: Optional[List[str]] = None
    meetingTime: Optional[List[str]] = None
    isActive: Optional[bool] = None


async def _compute_departments_analytics(
    session: DbSession, church_id: str
) -> Dict[str, Any]:
    all_depts = (
        await session.execute(
            select(Department)
            .where(Department.church_id == church_id)
            .order_by(Department.name)
        )
    ).scalars().all()

    formatted_all = [await _format_dept(session, d) for d in all_depts]

    total_departments = len(formatted_all)
    active_departments = sum(1 for d in formatted_all if d.get("isActive"))
    total_members = sum(int(d.get("memberCount", 0) or 0) for d in formatted_all)
    total_budget = sum(float(d.get("totalBudget", 0) or 0) for d in formatted_all)
    total_spent = sum(float(d.get("spentBudget", 0) or 0) for d in formatted_all)

    now = _now()
    current_ym = now.strftime("%Y-%m")

    month_buckets: List[Dict[str, Any]] = []
    for i in range(5, -1, -1):
        m_num = now.month - i
        y_num = now.year
        while m_num <= 0:
            m_num += 12
            y_num -= 1
        dt_m = datetime(y_num, m_num, 1, tzinfo=timezone.utc)
        month_buckets.append({
            "ym": dt_m.strftime("%Y-%m"),
            "label": dt_m.strftime("%b"),
        })

    act_buckets: Dict[str, Dict[str, Any]] = {
        b["ym"]: {
            "month": b["label"],
            "worship": 0,
            "children": 0,
            "youth": 0,
            "outreach": 0,
            "education": 0,
            "prayer": 0,
            "total": 0,
        }
        for b in month_buckets
    }

    monthly_activities_count = 0
    total_activities_count = 0

    for d in all_depts:
        _, meta = _parse_dept_desc(d.description)
        ckey = str(meta.get("category") or "").lower()
        dname = str(d.name or "").lower()
        acts = meta.get("activities") if isinstance(meta.get("activities"), list) else []
        total_activities_count += len(acts)
        for a in acts:
            a_date = str(a.get("date") or a.get("createdAt") or "")
            a_ym = a_date[:7] if len(a_date) >= 7 else current_ym
            if a_ym == current_ym:
                monthly_activities_count += 1
            if a_ym in act_buckets:
                bucket = act_buckets[a_ym]
                bucket["total"] += 1
                a_type = str(a.get("type") or "").lower()
                if "worship" in ckey or "worship" in dname or a_type == "rehearsal":
                    bucket["worship"] += 1
                elif "children" in ckey or "child" in dname:
                    bucket["children"] += 1
                elif "youth" in ckey or "youth" in dname:
                    bucket["youth"] += 1
                elif "outreach" in ckey or "mission" in ckey or "outreach" in a_type:
                    bucket["outreach"] += 1
                elif "education" in ckey or "discipleship" in ckey or "training" in a_type:
                    bucket["education"] += 1
                elif "prayer" in ckey or "pastoral" in ckey or "prayer" in a_type:
                    bucket["prayer"] += 1
                else:
                    bucket["worship"] += 1

    activity_data = [act_buckets[b["ym"]] for b in month_buckets]

    palette = [
        "#3b82f6",
        "#10b981",
        "#f59e0b",
        "#ef4444",
        "#8b5cf6",
        "#06b6d4",
        "#84cc16",
        "#ec4899",
    ]
    member_distribution: List[Dict[str, Any]] = []
    department_budgets: List[Dict[str, Any]] = []

    for idx, fd in enumerate(formatted_all):
        member_distribution.append({
            "name": fd["departmentName"],
            "value": int(fd.get("memberCount", 0) or 0),
            "color": palette[idx % len(palette)],
        })
        department_budgets.append({
            "name": fd["departmentName"],
            "budget": float(fd.get("totalBudget", 0) or 0),
            "spent": float(fd.get("spentBudget", 0) or 0),
        })

    return {
        "totalDepartments": total_departments,
        "activeDepartments": active_departments,
        "totalMembers": total_members,
        "totalBudget": total_budget,
        "totalSpent": total_spent,
        "monthlyActivities": monthly_activities_count,
        "totalActivities": total_activities_count,
        "activityData": activity_data,
        "memberDistribution": member_distribution,
        "departmentBudgets": department_budgets,
    }


@router.get("/analytics")
async def get_departments_analytics(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    analytics = await _compute_departments_analytics(session, actor.church_id)
    return {
        "success": True,
        "data": analytics,
        "analytics": analytics,
    }


@router.get("")
@router.get("/")
async def list_departments(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
    search: str = Query("", max_length=100),
    status: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Department.church_id == actor.church_id]
    if is_active is not None:
        conds.append(Department.is_active == is_active)
    elif status and status.strip().lower() in ("active", "inactive"):
        conds.append(Department.is_active == (status.strip().lower() == "active"))
    if search:
        s = f"%{search.strip()}%"
        conds.append(
            or_(Department.name.ilike(s), Department.description.ilike(s))
        )

    total = (
        await session.execute(select(func.count(Department.id)).where(*conds))
    ).scalar() or 0

    offset = (page - 1) * limit
    rows = (
        (
            await session.execute(
                select(Department)
                .where(*conds)
                .order_by(Department.name)
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    departments = [await _format_dept(session, d) for d in rows]
    pages = max(1, math.ceil(total / limit)) if limit else 1
    response.headers["X-Total-Count"] = str(total)
    analytics = await _compute_departments_analytics(session, actor.church_id)

    return {
        "departments": departments,
        "data": departments,
        "analytics": analytics,
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
async def create_department(
    request: Request,
    payload: DepartmentPayload,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    name = (payload.departmentName or payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Department name is required")

    leader_id = payload.leaderId
    leader_member_id = None
    if leader_id:
        member = (
            await session.execute(
                select(Member).where(
                    (Member.id == leader_id) | (Member.user_id == leader_id),
                    Member.church_id == actor.church_id,
                    Member.deleted_at.is_(None),
                )
            )
        ).scalars().first()
        if member:
            leader_member_id = str(member.id)

    tb = 0.0
    if payload.totalBudget is not None:
        try:
            tb = float(payload.totalBudget)
        except (ValueError, TypeError):
            tb = 0.0

    meta = {
        "totalBudget": tb,
        "category": (payload.category or "ministry").lower(),
        "location": payload.location or "Main Campus",
        "establishedDate": payload.establishedDate or _iso(_now()),
        "meetingDay": payload.meetingDay or [],
        "meetingTime": payload.meetingTime or [],
        "branchId": payload.branchId,
        "memberMeta": {},
    }
    if leader_member_id:
        meta["memberMeta"][leader_member_id] = {
            "role": "leader",
            "skills": ["Leadership"],
            "joinedDate": _iso(_now()),
            "isActive": True,
        }
    raw_desc = _build_dept_desc(payload.description, meta)

    dept = Department(
        name=name,
        church_id=actor.church_id,
        leader_member_id=leader_member_id,
        description=raw_desc,
        is_active=payload.isActive if payload.isActive is not None else True,
    )
    session.add(dept)
    await session.flush()

    if leader_member_id:
        # Check if member exists for foreign key
        mem_rec = (
            await session.execute(
                select(Member.id).where(
                    (Member.id == leader_member_id) | (Member.user_id == leader_member_id),
                    Member.church_id == actor.church_id,
                )
            )
        ).scalar_one_or_none()
        if mem_rec:
            existing_dm = (
                await session.execute(
                    select(DepartmentMember).where(
                        DepartmentMember.department_id == dept.id,
                        DepartmentMember.member_id == str(mem_rec),
                    )
                )
            ).scalar_one_or_none()
            if not existing_dm:
                session.add(
                    DepartmentMember(
                        department_id=dept.id,
                        member_id=str(mem_rec),
                        role=MemberRole.LEADER,
                    )
                )
            else:
                existing_dm.role = MemberRole.LEADER

    await session.commit()
    await session.refresh(dept)

    queue_audit(
        background_tasks,
        request,
        "CREATE_DEPARTMENT",
        "DEPARTMENT",
        dept.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    formatted = await _format_dept(session, dept)
    return {
        "status": "success",
        "message": "Department created successfully",
        "department": formatted,
        "data": formatted,
    }


@router.get("/{department_id}")
async def get_department(
    department_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")

    formatted = await _format_dept(session, dept)
    return {
        "department": formatted,
        "data": {"department": formatted},
    }


@router.put("/{department_id}")
async def update_department(
    department_id: str,
    payload: DepartmentPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")

    name = payload.departmentName or payload.name
    if name is not None:
        dept.name = name.strip()

    clean_desc, meta = _parse_dept_desc(dept.description)
    if payload.totalBudget is not None:
        try:
            meta["totalBudget"] = float(payload.totalBudget)
        except (ValueError, TypeError):
            meta["totalBudget"] = 0.0
    if payload.category is not None:
        meta["category"] = payload.category.lower()
    if payload.location is not None:
        meta["location"] = payload.location
    if payload.establishedDate is not None:
        meta["establishedDate"] = payload.establishedDate
    if payload.meetingDay is not None:
        meta["meetingDay"] = payload.meetingDay
    if payload.meetingTime is not None:
        meta["meetingTime"] = payload.meetingTime
    if payload.branchId is not None:
        meta["branchId"] = payload.branchId
    if payload.description is not None:
        clean_desc = payload.description

    if payload.leaderId is not None:
        if payload.leaderId:
            member = (
                await session.execute(
                    select(Member).where(
                        (Member.id == payload.leaderId) | (Member.user_id == payload.leaderId),
                        Member.church_id == actor.church_id,
                        Member.deleted_at.is_(None),
                    )
                )
            ).scalars().first()
            new_leader_id = str(member.id) if member else None
            dept.leader_member_id = new_leader_id

            # Update or add to DepartmentMember with MemberRole.LEADER
            mem_rec_id = new_leader_id

            if mem_rec_id:
                existing_dm = (
                    await session.execute(
                        select(DepartmentMember).where(
                            DepartmentMember.department_id == dept.id,
                            DepartmentMember.member_id == mem_rec_id,
                        )
                    )
                ).scalar_one_or_none()
                if not existing_dm:
                    session.add(
                        DepartmentMember(
                            department_id=dept.id,
                            member_id=mem_rec_id,
                            role=MemberRole.LEADER,
                        )
                    )
                else:
                    existing_dm.role = MemberRole.LEADER
                member_meta = meta.get("memberMeta") if isinstance(meta.get("memberMeta"), dict) else {}
                existing_mm = member_meta.get(mem_rec_id) if isinstance(member_meta.get(mem_rec_id), dict) else {}
                member_meta[mem_rec_id] = {
                    **existing_mm,
                    "role": "leader",
                    "skills": existing_mm.get("skills") or ["Leadership"],
                    "joinedDate": existing_mm.get("joinedDate") or _iso(_now()),
                    "isActive": existing_mm.get("isActive", True),
                }
                meta["memberMeta"] = member_meta
        else:
            dept.leader_member_id = None

    dept.description = _build_dept_desc(clean_desc, meta)

    if payload.isActive is not None:
        dept.is_active = payload.isActive

    dept.updated_at = _now()
    await session.commit()
    await session.refresh(dept)

    queue_audit(
        background_tasks,
        request,
        "UPDATE_DEPARTMENT",
        "DEPARTMENT",
        dept.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    formatted = await _format_dept(session, dept)
    return {
        "status": "success",
        "message": "Department updated successfully",
        "department": formatted,
        "data": formatted,
    }


@router.delete("/{department_id}")
async def delete_department(
    department_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    force: bool = Query(False),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")

    await session.execute(
        delete(DepartmentMember).where(DepartmentMember.department_id == dept.id)
    )
    await session.execute(delete(Department).where(Department.id == dept.id))
    await session.commit()

    queue_audit(
        background_tasks,
        request,
        "DELETE_DEPARTMENT",
        "DEPARTMENT",
        department_id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    return {"status": "success", "message": "Department deleted successfully"}


# =========================================================================
# Department Sub-resources (Members, Activities, Expenses, Goals)
# =========================================================================

class AddDeptMemberPayload(BaseModel):
    userId: Optional[str] = None
    memberId: Optional[str] = None
    role: Optional[str] = "member"
    skills: Optional[List[str]] = None
    joinedDate: Optional[str] = None
    isActive: Optional[bool] = True
    notes: Optional[str] = None


@router.get("/{department_id}/members")
async def list_department_members(
    department_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1),
    search: str = Query(""),
    role: Optional[str] = Query(None),
    isActive: Optional[bool] = Query(None),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")

    clean_desc, meta = _parse_dept_desc(dept.description)
    member_meta = meta.get("memberMeta") if isinstance(meta.get("memberMeta"), dict) else {}

    conds = [DepartmentMember.department_id == dept.id]
    dm_rows = (
        (
            await session.execute(
                select(DepartmentMember, Member, User)
                .join(Member, Member.id == DepartmentMember.member_id)
                .outerjoin(User, User.id == Member.user_id)
                .where(*conds)
            )
        )
        .all()
    )

    members_list = []
    has_leader = False
    for dm, m, u in dm_rows:
        first_name = (m.first_name if m and m.first_name else "") or ""
        last_name = (m.last_name if m and m.last_name else "") or ""
        if not first_name and not last_name and u and u.display_name:
            parts = u.display_name.strip().split(" ", 1)
            first_name = parts[0] if parts else ""
            last_name = parts[1] if len(parts) > 1 else ""
        mm = member_meta.get(str(m.id)) if isinstance(member_meta.get(str(m.id)), dict) else {}
        db_role_str = (dm.role.value if hasattr(dm.role, "value") else str(dm.role)).lower()
        role_str = str(mm.get("role") or db_role_str).lower()
        if role_str == "leader" or str(m.id) == str(dept.leader_member_id):
            has_leader = True
        skills_list = mm.get("skills") if isinstance(mm.get("skills"), list) else (
            ["Leadership"] if role_str == "leader" else ["Service"]
        )
        joined_date = mm.get("joinedDate") or _iso(dm.created_at)
        is_active_val = bool(mm.get("isActive", True))
        members_list.append(
            {
                "_id": str(dm.id),
                "id": str(dm.id),
                "memberId": str(m.id),
                "userId": {
                    "_id": str(m.id),
                    "id": str(m.id),
                    "firstName": first_name,
                    "lastName": last_name,
                    "email": (m.email if m and m.email else (u.email if u else "")) or "",
                    "phoneNumber": (
                        (m.phone_number if m and m.phone_number else (u.phone_number if u and hasattr(u, "phone_number") else ""))
                        or ""
                    ),
                    "profilePictureUrl": (
                        (m.photo_url if m and m.photo_url else (u.profile_picture_url if u and hasattr(u, "profile_picture_url") else ""))
                        or ""
                    ),
                },
                "role": role_str,
                "skills": skills_list,
                "joinedDate": joined_date,
                "isActive": is_active_val,
                "notes": mm.get("notes") or "",
            }
        )

    # If department has leader_member_id but leader is not in members_list, prepend the leader
    if dept.leader_member_id and not has_leader:
        lm = (
            await session.execute(
                select(Member, User)
                .outerjoin(User, User.id == Member.user_id)
                .where(
                    (Member.id == dept.leader_member_id)
                    | (Member.user_id == dept.leader_member_id)
                )
            )
        ).first()
        if lm:
            m, u = lm
            first_name = (m.first_name if m and m.first_name else "") or ""
            last_name = (m.last_name if m and m.last_name else "") or ""
            if not first_name and not last_name and u and u.display_name:
                parts = u.display_name.strip().split(" ", 1)
                first_name = parts[0] if parts else ""
                last_name = parts[1] if len(parts) > 1 else ""
            mm = member_meta.get(str(m.id)) if isinstance(member_meta.get(str(m.id)), dict) else {}
            leader_entry = {
                "_id": f"leader_{m.id}",
                "id": f"leader_{m.id}",
                "memberId": str(m.id),
                "userId": {
                    "_id": str(m.id),
                    "id": str(m.id),
                    "firstName": first_name,
                    "lastName": last_name,
                    "email": (m.email if m and m.email else (u.email if u else "")) or "",
                    "phoneNumber": (
                        (m.phone_number if m and m.phone_number else (u.phone_number if u and hasattr(u, "phone_number") else ""))
                        or ""
                    ),
                    "profilePictureUrl": (
                        (m.photo_url if m and m.photo_url else (u.profile_picture_url if u and hasattr(u, "profile_picture_url") else ""))
                        or ""
                    ),
                },
                "role": str(mm.get("role") or "leader").lower(),
                "skills": mm.get("skills") if isinstance(mm.get("skills"), list) else ["Leadership"],
                "joinedDate": mm.get("joinedDate") or _iso(dept.created_at),
                "isActive": bool(mm.get("isActive", True)),
                "notes": mm.get("notes") or "",
            }
            members_list.insert(0, leader_entry)
        else:
            usr = (
                await session.execute(
                    select(User).where(User.id == dept.leader_member_id)
                )
            ).scalar_one_or_none()
            if usr:
                parts = (usr.display_name or "").strip().split(" ", 1)
                first_name = parts[0] if parts else ""
                last_name = parts[1] if len(parts) > 1 else ""
                leader_entry = {
                    "_id": f"leader_{usr.id}",
                    "id": f"leader_{usr.id}",
                    "memberId": str(usr.id),
                    "userId": {
                        "_id": str(usr.id),
                        "firstName": first_name,
                        "lastName": last_name,
                        "email": usr.email or "",
                        "phoneNumber": usr.phone_number if hasattr(usr, "phone_number") and usr.phone_number else "",
                        "profilePictureUrl": usr.profile_picture_url if hasattr(usr, "profile_picture_url") and usr.profile_picture_url else "",
                    },
                    "role": "leader",
                    "skills": ["Leadership"],
                    "joinedDate": _iso(dept.created_at),
                    "isActive": True,
                    "notes": "",
                }
                members_list.insert(0, leader_entry)

    # Apply filters (search, role, isActive)
    if search:
        s = search.strip().lower()
        members_list = [
            item
            for item in members_list
            if s in f"{item['userId'].get('firstName', '')} {item['userId'].get('lastName', '')}".lower()
            or s in str(item["userId"].get("email", "")).lower()
            or s in str(item["userId"].get("phoneNumber", "")).lower()
            or any(s in str(sk).lower() for sk in (item.get("skills") or []))
        ]
    if role and role.strip().lower() not in ("", "all"):
        r_filter = role.strip().lower()
        members_list = [
            item for item in members_list if str(item.get("role", "")).lower() == r_filter
        ]
    if isActive is not None:
        members_list = [
            item for item in members_list if bool(item.get("isActive", True)) == isActive
        ]

    total = len(members_list)
    total_pages = max(1, math.ceil(total / limit)) if limit else 1
    offset = (page - 1) * limit
    paginated_members = members_list[offset : offset + limit]

    return {
        "success": True,
        "data": {
            "members": paginated_members,
            "department": {
                "id": str(dept.id),
                "departmentName": dept.name,
                "description": clean_desc,
            },
            "pagination": {
                "currentPage": page,
                "totalPages": total_pages,
                "totalMembers": total,
                "limit": limit,
                "hasNextPage": page < total_pages,
                "hasPrevPage": page > 1,
            },
        },
    }


@router.post("/{department_id}/members")
async def add_department_member(
    department_id: str,
    payload: AddDeptMemberPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")

    raw_mem_id = payload.userId or payload.memberId
    mem_id = None
    if raw_mem_id:
        found_mem = (
            await session.execute(
                select(Member.id).where(
                    or_(Member.id == raw_mem_id, Member.user_id == raw_mem_id),
                    Member.church_id == actor.church_id,
                    Member.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if found_mem:
            mem_id = str(found_mem)
    if not mem_id:
        raise HTTPException(status_code=400, detail="Valid Member ID is required")

    role_str = (payload.role or "member").strip().lower()
    role_enum = (
        MemberRole.LEADER
        if role_str == "leader"
        else MemberRole.ASSISTANT_LEADER
        if "assistant" in role_str
        else MemberRole.MEMBER
    )
    existing = (
        await session.execute(
            select(DepartmentMember).where(
                DepartmentMember.department_id == dept.id,
                DepartmentMember.member_id == mem_id,
            )
        )
    ).scalar_one_or_none()
    if not existing:
        dm = DepartmentMember(
            department_id=dept.id,
            member_id=mem_id,
            role=role_enum,
        )
        session.add(dm)
    else:
        existing.role = role_enum

    if role_enum == MemberRole.LEADER:
        dept.leader_member_id = mem_id

    clean_desc, meta = _parse_dept_desc(dept.description)
    member_meta = meta.get("memberMeta") if isinstance(meta.get("memberMeta"), dict) else {}
    member_meta[mem_id] = {
        "role": role_str,
        "skills": payload.skills if isinstance(payload.skills, list) else (
            ["Leadership"] if role_str == "leader" else ["Service"]
        ),
        "joinedDate": payload.joinedDate or _iso(_now()),
        "isActive": payload.isActive if payload.isActive is not None else True,
        "notes": payload.notes or "",
    }
    meta["memberMeta"] = member_meta
    dept.description = _build_dept_desc(clean_desc, meta)
    dept.updated_at = _now()

    await session.commit()
    return {"success": True, "message": "Member added to department"}


@router.put("/{department_id}/members/{member_id}")
async def update_department_member(
    department_id: str,
    member_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")

    raw_target = member_id.replace("leader_", "")
    dm = (
        await session.execute(
            select(DepartmentMember).where(
                DepartmentMember.department_id == dept.id,
                or_(
                    DepartmentMember.id == raw_target,
                    DepartmentMember.member_id == raw_target,
                ),
            )
        )
    ).scalar_one_or_none()

    target_mem_id = str(dm.member_id) if dm else raw_target
    if payload.get("userId"):
        resolved_mem = (
            await session.execute(
                select(Member.id).where(
                    or_(Member.id == payload["userId"], Member.user_id == payload["userId"]),
                    Member.church_id == actor.church_id,
                ).limit(1)
            )
        ).scalar_one_or_none()
        if resolved_mem:
            target_mem_id = str(resolved_mem)
            if dm and dm.member_id != target_mem_id:
                dm.member_id = target_mem_id

    role_str = (payload.get("role") or "member").strip().lower()
    role_enum = (
        MemberRole.LEADER
        if role_str == "leader"
        else MemberRole.ASSISTANT_LEADER
        if "assistant" in role_str
        else MemberRole.MEMBER
    )
    if dm:
        dm.role = role_enum
        if role_enum == MemberRole.LEADER:
            dept.leader_member_id = dm.member_id

    clean_desc, meta = _parse_dept_desc(dept.description)
    member_meta = meta.get("memberMeta") if isinstance(meta.get("memberMeta"), dict) else {}
    existing_mm = member_meta.get(target_mem_id) if isinstance(member_meta.get(target_mem_id), dict) else {}
    member_meta[target_mem_id] = {
        "role": role_str,
        "skills": payload.get("skills") if isinstance(payload.get("skills"), list) else existing_mm.get("skills", ["Service"]),
        "joinedDate": payload.get("joinedDate") or existing_mm.get("joinedDate") or _iso(_now()),
        "isActive": payload.get("isActive") if payload.get("isActive") is not None else existing_mm.get("isActive", True),
        "notes": payload.get("notes") if payload.get("notes") is not None else existing_mm.get("notes", ""),
    }
    meta["memberMeta"] = member_meta
    dept.description = _build_dept_desc(clean_desc, meta)
    dept.updated_at = _now()

    await session.commit()
    return {"success": True, "message": "Department member updated successfully"}


@router.delete("/{department_id}/members/{member_id}")
async def remove_department_member(
    department_id: str,
    member_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    raw_target = member_id.replace("leader_", "")
    dm = (
        await session.execute(
            select(DepartmentMember).where(
                DepartmentMember.department_id == department_id,
                or_(
                    DepartmentMember.id == raw_target,
                    DepartmentMember.member_id == raw_target,
                ),
            )
        )
    ).scalar_one_or_none()
    target_mem_id = str(dm.member_id) if dm else raw_target

    await session.execute(
        delete(DepartmentMember).where(
            DepartmentMember.department_id == department_id,
            or_(
                DepartmentMember.id == raw_target,
                DepartmentMember.member_id == raw_target,
            ),
        )
    )
    if dept:
        clean_desc, meta = _parse_dept_desc(dept.description)
        member_meta = meta.get("memberMeta") if isinstance(meta.get("memberMeta"), dict) else {}
        member_meta.pop(target_mem_id, None)
        meta["memberMeta"] = member_meta
        if str(dept.leader_member_id) == target_mem_id:
            dept.leader_member_id = None
        dept.description = _build_dept_desc(clean_desc, meta)
        dept.updated_at = _now()

    await session.commit()
    return {"success": True, "message": "Member removed from department"}


async def _resolve_activity_participants(
    session: DbSession,
    raw_participants: Any,
    church_id: str,
) -> List[Dict[str, Any]]:
    if not isinstance(raw_participants, list):
        return []
    resolved: List[Dict[str, Any]] = []
    for item in raw_participants:
        if isinstance(item, dict):
            pid = item.get("_id") or item.get("id")
            if item.get("fullName") or item.get("firstName"):
                fn = item.get("firstName") or ""
                ln = item.get("lastName") or ""
                full = item.get("fullName") or f"{fn} {ln}".strip()
                resolved.append({
                    "_id": str(pid or ""),
                    "id": str(pid or ""),
                    "firstName": fn,
                    "lastName": ln,
                    "fullName": full,
                    "isLocked": bool(item.get("isLocked", False)),
                })
                continue
            raw_id = str(pid) if pid else None
        else:
            raw_id = str(item) if item else None
        if raw_id:
            brief = await _resolve_member_brief(session, raw_id, church_id)
            if brief:
                resolved.append(brief)
    return resolved


@router.get("/{department_id}/activities")
async def list_department_activities(
    department_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1),
    search: str = Query(""),
    activityType: Optional[str] = Query(None),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    _, meta = _parse_dept_desc(dept.description)
    activities = list(meta.get("activities") or [])
    if search:
        s = search.lower()
        activities = [
            a for a in activities
            if s in (a.get("title") or "").lower() or s in (a.get("description") or "").lower()
        ]
    if activityType and activityType != "all":
        activities = [
            a for a in activities if (a.get("type") or "").lower() == activityType.lower()
        ]

    # Ensure participants are resolved objects
    for a in activities:
        a["participants"] = await _resolve_activity_participants(
            session, a.get("participants"), actor.church_id
        )

    total = len(activities)
    total_pages = max(1, math.ceil(total / limit))
    offset = (page - 1) * limit
    paginated = activities[offset : offset + limit]
    return {
        "success": True,
        "data": {
            "activities": paginated,
            "pagination": {
                "currentPage": page,
                "totalPages": total_pages,
                "totalActivities": total,
                "hasMore": page < total_pages,
            },
        },
    }


@router.post("/{department_id}/activities")
async def create_department_activity(
    department_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    clean_desc, meta = _parse_dept_desc(dept.description)
    activities = list(meta.get("activities") or [])
    aid = str(uuid.uuid4())
    resolved_participants = await _resolve_activity_participants(
        session, payload.get("participants"), actor.church_id
    )
    new_act = {
        "_id": aid,
        "id": aid,
        "title": payload.get("title") or payload.get("activity") or "Department Activity",
        "description": payload.get("description") or "",
        "type": (payload.get("type") or "meeting").lower(),
        "date": payload.get("date") or _iso(_now()),
        "startTime": payload.get("startTime") or "09:00",
        "endTime": payload.get("endTime") or "11:00",
        "location": payload.get("location") or meta.get("location", "Main Campus"),
        "participants": resolved_participants,
        "organizedBy": payload.get("organizedBy") or dept.name,
        "notes": payload.get("notes") or "",
        "isCompleted": bool(payload.get("isCompleted", False)),
        "createdAt": _iso(_now()),
        "updatedAt": _iso(_now()),
    }
    activities.insert(0, new_act)
    meta["activities"] = activities
    dept.description = _build_dept_desc(clean_desc, meta)
    dept.updated_at = _now()
    await session.commit()
    return {"success": True, "message": "Activity logged successfully", "data": new_act}


@router.put("/{department_id}/activities/{activity_id}")
async def update_department_activity(
    department_id: str,
    activity_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    clean_desc, meta = _parse_dept_desc(dept.description)
    activities = list(meta.get("activities") or [])
    for a in activities:
        if a.get("_id") == activity_id or a.get("id") == activity_id:
            for k in ("title", "description", "type", "date", "startTime", "endTime", "location", "notes", "isCompleted"):
                if k in payload and payload[k] is not None:
                    a[k] = payload[k]
            if "participants" in payload and payload["participants"] is not None:
                a["participants"] = await _resolve_activity_participants(
                    session, payload["participants"], actor.church_id
                )
            a["updatedAt"] = _iso(_now())
    meta["activities"] = activities
    dept.description = _build_dept_desc(clean_desc, meta)
    dept.updated_at = _now()
    await session.commit()
    return {"success": True, "message": "Activity updated successfully"}


@router.delete("/{department_id}/activities/{activity_id}")
async def delete_department_activity(
    department_id: str,
    activity_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if dept:
        clean_desc, meta = _parse_dept_desc(dept.description)
        meta["activities"] = [
            a for a in (meta.get("activities") or [])
            if a.get("_id") != activity_id and a.get("id") != activity_id
        ]
        dept.description = _build_dept_desc(clean_desc, meta)
        dept.updated_at = _now()
        await session.commit()
    return {"success": True, "message": "Activity deleted successfully"}


@router.get("/{department_id}/expenses")
async def list_department_expenses(
    department_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1),
    search: str = Query(""),
    category: Optional[str] = Query(None),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()

    clean_desc, meta = _parse_dept_desc(dept.description if dept else "")
    total_budget = float(meta.get("totalBudget") or 0)
    dept_name = dept.name if dept else "Department"
    expenses = list(meta.get("expenses") or [])
    total_spent = sum(float(e.get("amount") or 0) for e in expenses)

    if search:
        s = search.lower()
        expenses = [
            e for e in expenses
            if s in (e.get("description") or "").lower() or s in (e.get("vendor") or "").lower()
        ]
    if category and category != "all":
        expenses = [
            e for e in expenses if (e.get("category") or "").lower() == category.lower()
        ]

    filtered_total = sum(float(e.get("amount") or 0) for e in expenses)
    total = len(expenses)
    pages = max(1, math.ceil(total / limit))
    offset = (page - 1) * limit
    paginated = expenses[offset : offset + limit]

    return {
        "success": True,
        "expenses": paginated,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": pages,
            "hasNext": page < pages,
            "hasPrev": page > 1,
        },
        "summary": {
            "filteredTotal": filtered_total,
            "departmentName": dept_name,
            "totalBudget": total_budget,
            "totalSpent": total_spent,
            "remainingBudget": max(0.0, total_budget - total_spent),
            "budgetCategories": [],
        },
    }


@router.post("/{department_id}/expenses")
async def create_department_expense(
    department_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    clean_desc, meta = _parse_dept_desc(dept.description)
    expenses = list(meta.get("expenses") or [])
    eid = str(uuid.uuid4())
    new_exp = {
        "_id": eid,
        "id": eid,
        "category": (payload.get("category") or "supplies").lower(),
        "amount": float(payload.get("amount") or 0),
        "description": payload.get("description") or "",
        "date": payload.get("date") or _iso(_now()),
        "approvedBy": payload.get("approvedBy") or "",
        "receiptUrl": payload.get("receiptUrl") or "",
        "reference": payload.get("reference") or "",
        "vendor": payload.get("vendor") or "",
        "createdAt": _iso(_now()),
        "updatedAt": _iso(_now()),
    }
    expenses.insert(0, new_exp)
    meta["expenses"] = expenses
    dept.description = _build_dept_desc(clean_desc, meta)
    dept.updated_at = _now()
    await session.commit()
    return {"success": True, "message": "Expense recorded successfully", "data": new_exp}


@router.put("/{department_id}/expenses/{expense_id}")
async def update_department_expense(
    department_id: str,
    expense_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    clean_desc, meta = _parse_dept_desc(dept.description)
    expenses = list(meta.get("expenses") or [])
    for e in expenses:
        if e.get("_id") == expense_id or e.get("id") == expense_id:
            for k in ("category", "description", "date", "approvedBy", "receiptUrl", "reference", "vendor"):
                if k in payload and payload[k] is not None:
                    e[k] = payload[k]
            if "amount" in payload and payload["amount"] is not None:
                e["amount"] = float(payload["amount"])
            e["updatedAt"] = _iso(_now())
    meta["expenses"] = expenses
    dept.description = _build_dept_desc(clean_desc, meta)
    dept.updated_at = _now()
    await session.commit()
    return {"success": True, "message": "Expense updated successfully"}


@router.delete("/{department_id}/expenses/{expense_id}")
async def delete_department_expense(
    department_id: str,
    expense_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if dept:
        clean_desc, meta = _parse_dept_desc(dept.description)
        meta["expenses"] = [
            e for e in (meta.get("expenses") or [])
            if e.get("_id") != expense_id and e.get("id") != expense_id
        ]
        dept.description = _build_dept_desc(clean_desc, meta)
        dept.updated_at = _now()
        await session.commit()
    return {"success": True, "message": "Expense deleted successfully"}


@router.get("/{department_id}/goals")
async def list_department_goals(
    department_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1),
    search: str = Query(""),
    priority: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    assigneeId: Optional[str] = Query(None),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    _, meta = _parse_dept_desc(dept.description)
    goals = list(meta.get("goals") or [])

    # Ensure assignee is resolved
    for g in goals:
        raw_ass = g.get("assignee")
        if isinstance(raw_ass, str) and raw_ass:
            g["assignee"] = await _resolve_member_brief(session, raw_ass, actor.church_id)

    if search:
        s = search.lower()
        goals = [
            g for g in goals
            if s in (g.get("title") or "").lower() or s in (g.get("description") or "").lower()
        ]
    if priority and priority != "all":
        goals = [g for g in goals if (g.get("priority") or "").lower() == priority.lower()]
    if category and category != "all":
        goals = [g for g in goals if (g.get("category") or "").lower() == category.lower()]
    if status and status != "all":
        goals = [g for g in goals if (g.get("status") or "").lower() == status.lower()]
    if assigneeId and assigneeId != "all":
        goals = [
            g for g in goals
            if (isinstance(g.get("assignee"), dict) and g["assignee"].get("_id") == assigneeId)
            or g.get("assignee") == assigneeId
        ]
    total = len(goals)
    pages = max(1, math.ceil(total / limit))
    offset = (page - 1) * limit
    paginated = goals[offset : offset + limit]
    return {
        "success": True,
        "data": {
            "goals": paginated,
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


@router.post("/{department_id}/goals")
async def create_department_goal(
    department_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    clean_desc, meta = _parse_dept_desc(dept.description)
    goals = list(meta.get("goals") or [])
    gid = str(uuid.uuid4())
    raw_assignee = payload.get("assignee")
    assignee_obj = None
    if isinstance(raw_assignee, dict):
        assignee_obj = raw_assignee
    elif isinstance(raw_assignee, str) and raw_assignee.strip():
        assignee_obj = await _resolve_member_brief(session, raw_assignee.strip(), actor.church_id)

    new_goal = {
        "_id": gid,
        "id": gid,
        "title": payload.get("title") or "Department Goal",
        "description": payload.get("description") or "",
        "targetDate": payload.get("targetDate") or _iso(_now()),
        "status": (payload.get("status") or "planned").lower(),
        "progress": int(float(payload.get("progress") or 0)),
        "priority": (payload.get("priority") or "medium").lower(),
        "category": (payload.get("category") or "growth").lower(),
        "assignee": assignee_obj,
        "success": payload.get("success") or "",
        "createdBy": current_user.display_name or current_user.email or "Admin",
        "createdAt": _iso(_now()),
        "updatedAt": _iso(_now()),
    }
    goals.insert(0, new_goal)
    meta["goals"] = goals
    dept.description = _build_dept_desc(clean_desc, meta)
    dept.updated_at = _now()
    await session.commit()
    return {"success": True, "message": "Goal created successfully", "data": new_goal}


@router.put("/{department_id}/goals/{goal_id}")
async def update_department_goal(
    department_id: str,
    goal_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    clean_desc, meta = _parse_dept_desc(dept.description)
    goals = list(meta.get("goals") or [])
    for g in goals:
        if g.get("_id") == goal_id or g.get("id") == goal_id:
            for k in ("title", "description", "targetDate", "status", "priority", "category", "success"):
                if k in payload and payload[k] is not None:
                    g[k] = payload[k].lower() if k in ("status", "priority", "category") and isinstance(payload[k], str) else payload[k]
            if "progress" in payload and payload["progress"] is not None:
                g["progress"] = int(float(payload["progress"]))
            if "assignee" in payload:
                raw_ass = payload["assignee"]
                if isinstance(raw_ass, dict):
                    g["assignee"] = raw_ass
                elif isinstance(raw_ass, str) and raw_ass.strip():
                    g["assignee"] = await _resolve_member_brief(session, raw_ass.strip(), actor.church_id)
                elif not raw_ass:
                    g["assignee"] = None
            g["updatedAt"] = _iso(_now())
    meta["goals"] = goals
    dept.description = _build_dept_desc(clean_desc, meta)
    dept.updated_at = _now()
    await session.commit()
    return {"success": True, "message": "Goal updated successfully"}


@router.delete("/{department_id}/goals/{goal_id}")
async def delete_department_goal(
    department_id: str,
    goal_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    dept = (
        await session.execute(
            select(Department).where(
                Department.id == department_id,
                Department.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if dept:
        clean_desc, meta = _parse_dept_desc(dept.description)
        meta["goals"] = [
            g for g in (meta.get("goals") or [])
            if g.get("_id") != goal_id and g.get("id") != goal_id
        ]
        dept.description = _build_dept_desc(clean_desc, meta)
        dept.updated_at = _now()
        await session.commit()
    return {"success": True, "message": "Goal deleted successfully"}

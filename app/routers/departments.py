from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Dict, List, Optional
import uuid

from app.core.deps import CurrentUser, DbSession, church_id_from, require, resolve_actor
from app.enums import MemberRole
from app.models.auth import Account
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
    match = re.match(r"^<!--dept_meta:(.*?)-->\n?(.*)$", raw_description, re.DOTALL)
    if match:
        try:
            meta = json.loads(match.group(1))
            clean_desc = match.group(2)
        except Exception:
            pass
    return clean_desc, meta


def _build_dept_desc(clean_desc: Optional[str], meta: dict) -> str:
    desc = clean_desc or ""
    meta_json = json.dumps(meta)
    return f"<!--dept_meta:{meta_json}-->\n{desc}"


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

    category = meta.get("category", "General")
    location = meta.get("location", "Main Campus")
    established_date = meta.get("establishedDate", _iso(dept.created_at))
    meeting_day = meta.get("meetingDay", [])
    meeting_time = meta.get("meetingTime", [])

    return {
        "_id": str(dept.id),
        "id": str(dept.id),
        "churchId": str(dept.church_id),
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
        "category": category,
        "location": location,
        "establishedDate": established_date,
        "meetingDay": meeting_day,
        "meetingTime": meeting_time,
        "_count": {
            "members": member_count,
            "activities": len(meta.get("activities", []) if isinstance(meta.get("activities"), list) else []),
            "expenses": len(meta.get("expenses", []) if isinstance(meta.get("expenses"), list) else []),
        },
        "createdAt": _iso(dept.created_at),
        "updatedAt": _iso(dept.updated_at),
    }


class DepartmentPayload(BaseModel):
    departmentName: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    leaderId: Optional[str] = None
    category: Optional[str] = None
    establishedDate: Optional[str] = None
    location: Optional[str] = None
    totalBudget: Optional[Any] = None
    meetingDay: Optional[List[str]] = None
    meetingTime: Optional[List[str]] = None
    isActive: Optional[bool] = True


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
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Department.church_id == actor.church_id]
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
    pages = math.ceil(total / limit) if limit else 1
    response.headers["X-Total-Count"] = str(total)

    return {
        "departments": departments,
        "data": departments,
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
        "category": payload.category or "General",
        "location": payload.location or "Main Campus",
        "establishedDate": payload.establishedDate or _iso(_now()),
        "meetingDay": payload.meetingDay or [],
        "meetingTime": payload.meetingTime or [],
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
        meta["category"] = payload.category
    if payload.location is not None:
        meta["location"] = payload.location
    if payload.establishedDate is not None:
        meta["establishedDate"] = payload.establishedDate
    if payload.meetingDay is not None:
        meta["meetingDay"] = payload.meetingDay
    if payload.meetingTime is not None:
        meta["meetingTime"] = payload.meetingTime
    if payload.description is not None:
        clean_desc = payload.description

    dept.description = _build_dept_desc(clean_desc, meta)

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
        else:
            dept.leader_member_id = None
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
    memberId: Optional[str] = None
    role: Optional[str] = "member"


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

    conds = [DepartmentMember.department_id == dept.id]

    total = (
        await session.execute(select(func.count(DepartmentMember.id)).where(*conds))
    ).scalar() or 0

    offset = (page - 1) * limit
    dm_rows = (
        (
            await session.execute(
                select(DepartmentMember, Member, User)
                .join(Member, Member.id == DepartmentMember.member_id)
                .outerjoin(User, User.id == Member.user_id)
                .where(*conds)
                .offset(offset)
                .limit(limit)
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
        role_str = (dm.role.value if hasattr(dm.role, "value") else str(dm.role)).lower()
        if role_str == "leader":
            has_leader = True
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
                "skills": ["Leadership" if role_str == "leader" else "Service"],
                "joinedDate": _iso(dm.created_at),
                "isActive": True,
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
                "role": "leader",
                "skills": ["Leadership"],
                "joinedDate": _iso(dept.created_at),
                "isActive": True,
            }
            members_list.insert(0, leader_entry)
            total += 1
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
                }
                members_list.insert(0, leader_entry)
                total += 1

    clean_desc, _ = _parse_dept_desc(dept.description)
    total_pages = math.ceil(total / limit) if limit else 1
    return {
        "success": True,
        "data": {
            "members": members_list,
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

    raw_mem_id = payload.memberId
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
        first_mem = (
            await session.execute(
                select(Member).where(
                    Member.church_id == actor.church_id,
                    Member.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if first_mem:
            mem_id = str(first_mem.id)
        else:
            raise HTTPException(status_code=400, detail="Member ID is required")

    role_enum = (
        MemberRole.LEADER
        if (payload.role or "").lower() == "leader"
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

    dm = (
        await session.execute(
            select(DepartmentMember).where(
                DepartmentMember.department_id == dept.id,
                or_(
                    DepartmentMember.id == member_id,
                    DepartmentMember.member_id == member_id,
                ),
            )
        )
    ).scalar_one_or_none()
    role_str = (payload.get("role") or "member").lower()
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
    await session.execute(
        delete(DepartmentMember).where(
            DepartmentMember.department_id == department_id,
            or_(
                DepartmentMember.id == member_id,
                DepartmentMember.member_id == member_id,
            ),
        )
    )
    await session.commit()
    return {"success": True, "message": "Member removed from department"}


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
        "participants": payload.get("participants") if isinstance(payload.get("participants"), list) else [],
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
    new_goal = {
        "_id": gid,
        "id": gid,
        "title": payload.get("title") or "Department Goal",
        "description": payload.get("description") or "",
        "targetDate": payload.get("targetDate") or _iso(_now()),
        "status": (payload.get("status") or "planned").lower(),
        "progress": int(float(payload.get("progress") or 0)),
        "priority": payload.get("priority") or "Medium",
        "category": payload.get("category") or "Growth",
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
                    g[k] = payload[k]
            if "progress" in payload and payload["progress"] is not None:
                g["progress"] = int(float(payload["progress"]))
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

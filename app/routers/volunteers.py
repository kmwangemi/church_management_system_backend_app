from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional, Tuple
import uuid

from app.core.deps import CurrentUser, DbSession, church_id_from, resolve_actor
from app.enums import AssignmentStatus, MembershipStatus, MemberType
from app.models.church import Branch, Church
from app.models.detail import (
    AvailabilitySchedule,
    BackgroundCheck,
    VolunteerAssignment,
    VolunteerDetails,
    VolunteerRole,
)
from app.models.member import Member
from app.models.user import User
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
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


async def _get_or_create_default_branch(
    session: DbSession, church: Church
) -> Branch:
    branch = (
        await session.execute(
            select(Branch)
            .where(Branch.church_id == church.id, Branch.deleted_at.is_(None))
            .order_by(Branch.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if branch:
        return branch
    branch = Branch(
        name=f"{church.name} - Main Campus",
        church_id=church.id,
        email=church.email,
    )
    session.add(branch)
    await session.flush()
    return branch


class VolunteerPayload(BaseModel):
    memberId: Optional[str] = None
    volunteerName: Optional[str] = None
    volunteerEmail: Optional[str] = None
    volunteerPhone: Optional[str] = None
    role: Optional[str] = None
    roleIds: Optional[List[str]] = None
    skills: Optional[List[str]] = None
    availability: Optional[List[str]] = None
    hoursContributed: Optional[int] = 0
    hoursThisMonth: Optional[int] = None
    status: Optional[str] = "Active"
    notes: Optional[str] = ""


class VolunteerUpdatePayload(BaseModel):
    volunteerName: Optional[str] = None
    volunteerEmail: Optional[str] = None
    volunteerPhone: Optional[str] = None
    skills: Optional[List[str]] = None
    availability: Optional[List[str]] = None
    hoursContributed: Optional[int] = None
    hoursThisMonth: Optional[int] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    roleIds: Optional[List[str]] = None


class VolunteerRolePayload(BaseModel):
    role: Optional[str] = None
    title: Optional[str] = None
    department: Optional[str] = "General Ministry"
    description: Optional[str] = ""
    requiredSkills: Optional[List[str]] = None
    timeCommitment: Optional[str] = "2 hours/week"
    neededVolunteers: Optional[int] = 2
    priority: Optional[str] = "Medium"
    isActive: Optional[bool] = True


class VolunteerTaskPayload(BaseModel):
    title: str
    date: Optional[str] = None
    time: Optional[str] = None
    department: Optional[str] = "General Ministry"
    description: Optional[str] = ""
    requiredVolunteers: Optional[int] = 4
    priority: Optional[str] = "Medium"
    status: Optional[str] = None
    assignedVolunteerIds: Optional[List[str]] = None


async def _load_church_volunteer_context(
    session: DbSession, church_id: str
) -> Tuple[
    Church,
    Dict[str, Any],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
]:
    church = await _get_church(session, church_id)
    meta = dict(church.metadata_json or {})
    vol_meta_map: Dict[str, Any] = dict(meta.get("volunteer_meta") or {})
    role_meta_map: Dict[str, Any] = dict(meta.get("volunteer_role_meta") or {})
    tasks_raw: List[Dict[str, Any]] = list(meta.get("volunteer_tasks") or [])

    # Load all VolunteerRole rows for this church
    role_rows = (
        await session.execute(
            select(VolunteerRole)
            .where(VolunteerRole.church_id == church_id)
            .order_by(VolunteerRole.created_at.desc())
        )
    ).scalars().all()
    roles_by_id: Dict[str, VolunteerRole] = {str(r.id): r for r in role_rows}

    # Load all VolunteerDetails + Member + User for this church
    vol_rows = (
        await session.execute(
            select(VolunteerDetails, Member, User)
            .join(Member, Member.id == VolunteerDetails.member_id)
            .outerjoin(User, User.id == Member.user_id)
            .where(Member.church_id == church_id, Member.deleted_at.is_(None))
            .order_by(VolunteerDetails.created_at.desc())
        )
    ).all()

    # Load all active VolunteerAssignment rows for these volunteers
    vol_ids = [str(v.id) for v, _, _ in vol_rows]
    assignments_by_vol: Dict[str, List[str]] = {}
    assignments_by_role: Dict[str, List[str]] = {}
    if vol_ids:
        ass_rows = (
            await session.execute(
                select(VolunteerAssignment).where(
                    VolunteerAssignment.volunteer_details_id.in_(vol_ids),
                    VolunteerAssignment.is_active.is_(True),
                )
            )
        ).scalars().all()
        for a in ass_rows:
            vid = str(a.volunteer_details_id)
            rid = str(a.volunteer_role_id)
            assignments_by_vol.setdefault(vid, []).append(rid)
            assignments_by_role.setdefault(rid, []).append(vid)

    # Format volunteers
    formatted_volunteers: List[Dict[str, Any]] = []
    vol_name_by_id: Dict[str, str] = {}
    for vol, m, u in vol_rows:
        vid = str(vol.id)
        vmeta = dict(vol_meta_map.get(vid) or {})
        name = (
            vmeta.get("name")
            or (
                f"{m.first_name or ''} {m.last_name or ''}".strip()
                if m
                else (u.display_name if u and u.display_name else "Volunteer")
            )
            or "Volunteer"
        )
        vol_name_by_id[vid] = name
        email = (
            vmeta.get("email")
            or (m.email if m and m.email else (u.email if u else ""))
            or ""
        )
        phone = (
            vmeta.get("phone")
            or (m.phone_number if m and m.phone_number else "")
            or ""
        )
        skills = list(vmeta.get("skills") or [])
        availability = list(vmeta.get("availability") or [])
        notes = vmeta.get("notes") or ""
        status = vmeta.get("status") or "Active"
        assigned_rids = assignments_by_vol.get(vid, [])
        role_names = [
            roles_by_id[rid].role
            for rid in assigned_rids
            if rid in roles_by_id
        ]
        if not role_names and vmeta.get("customRoles"):
            role_names = list(vmeta.get("customRoles") or [])
        hours = int(
            vmeta.get("hoursThisMonth", vol.hours_contributed or 0) or 0
        )

        formatted_volunteers.append(
            {
                "_id": vid,
                "id": vid,
                "memberId": str(m.id),
                "name": name,
                "email": email,
                "phone": phone,
                "avatar": m.photo_url or "",
                "skills": skills,
                "availability": availability,
                "currentRoles": role_names,
                "roleIds": assigned_rids,
                "notes": notes,
                "joinDate": _iso(vol.created_at),
                "status": status,
                "hoursThisMonth": hours,
                "hoursContributed": vol.hours_contributed or hours,
            }
        )

    # Format roles
    formatted_roles: List[Dict[str, Any]] = []
    for r in role_rows:
        rid = str(r.id)
        rmeta = dict(role_meta_map.get(rid) or {})
        assigned_vids = [
            vid
            for vid in assignments_by_role.get(rid, [])
            if vid in vol_name_by_id
        ]
        assigned_names = [vol_name_by_id[vid] for vid in assigned_vids]
        current_cnt = len(assigned_vids)
        target_needed = max(0, int(rmeta.get("neededVolunteers", 2) or 0))
        open_spots = max(0, target_needed - current_cnt) if rmeta.get("isTargetTotal") else target_needed

        req_skills = rmeta.get("requiredSkills")
        if isinstance(req_skills, str):
            req_skills_list = [s.strip() for s in req_skills.split(",") if s.strip()]
        elif isinstance(req_skills, list):
            req_skills_list = [str(s).strip() for s in req_skills if str(s).strip()]
        else:
            req_skills_list = []

        formatted_roles.append(
            {
                "_id": rid,
                "id": rid,
                "title": r.role,
                "role": r.role,
                "department": r.department or "General Ministry",
                "description": r.description or "",
                "requiredSkills": req_skills_list,
                "timeCommitment": rmeta.get("timeCommitment") or "2 hours/week",
                "currentVolunteers": current_cnt,
                "neededVolunteers": open_spots,
                "assignedVolunteerIds": assigned_vids,
                "assignedVolunteers": assigned_names,
                "priority": rmeta.get("priority") or "Medium",
                "isActive": r.is_active,
                "createdAt": _iso(r.created_at),
            }
        )

    # Format tasks
    formatted_tasks: List[Dict[str, Any]] = []
    for t in tasks_raw:
        tid = t.get("id") or t.get("_id")
        vids = [
            vid
            for vid in (t.get("assignedVolunteerIds") or [])
            if vid in vol_name_by_id
        ]
        vnames = [vol_name_by_id[vid] for vid in vids]
        req_cnt = max(1, int(t.get("requiredVolunteers", 4) or 4))
        explicit_status = t.get("status")
        if explicit_status == "Completed":
            status_val = "Completed"
        elif len(vids) >= req_cnt:
            status_val = "Scheduled"
        elif len(vids) > 0:
            status_val = "Needs Volunteers"
        else:
            status_val = explicit_status if explicit_status in ("Scheduled", "Needs Volunteers", "Open") else "Open"

        formatted_tasks.append(
            {
                **t,
                "id": tid,
                "_id": tid,
                "title": t.get("title") or "Service Task",
                "department": t.get("department") or "General Ministry",
                "description": t.get("description") or "",
                "date": t.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "time": t.get("time") or "09:00 AM",
                "requiredVolunteers": req_cnt,
                "assignedVolunteerIds": vids,
                "assignedVolunteers": vnames,
                "status": status_val,
                "priority": t.get("priority") or "Medium",
            }
        )

    return church, meta, formatted_volunteers, formatted_roles, formatted_tasks


# ---------------------------------------------------------------------------
# Stats & Reports (/church/volunteers/stats)
# ---------------------------------------------------------------------------
@router.get("/stats")
async def get_volunteer_stats(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    _, _, volunteers, roles, tasks = await _load_church_volunteer_context(
        session, actor.church_id
    )

    now = _now()
    this_month_start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    total_volunteers = len(volunteers)
    active_volunteers = sum(
        1 for v in volunteers if (v.get("status") or "Active").lower() == "active"
    )
    inactive_volunteers = max(0, total_volunteers - active_volunteers)
    active_rate = (
        round((active_volunteers / total_volunteers) * 100)
        if total_volunteers > 0
        else 0
    )

    new_this_month = 0
    for v in volunteers:
        jd = v.get("joinDate")
        if jd:
            try:
                dt = datetime.fromisoformat(str(jd).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= this_month_start:
                    new_this_month += 1
            except Exception:
                pass

    hours_this_month = sum(
        int(v.get("hoursThisMonth", 0) or 0) for v in volunteers
    )
    avg_hours = (
        round(hours_this_month / total_volunteers, 1)
        if total_volunteers > 0
        else 0.0
    )

    open_role_positions = sum(
        max(0, int(r.get("neededVolunteers", 0) or 0))
        for r in roles
        if r.get("isActive", True)
    )
    open_task_positions = sum(
        max(
            0,
            int(t.get("requiredVolunteers", 0) or 0)
            - len(t.get("assignedVolunteerIds") or []),
        )
        for t in tasks
        if t.get("status") != "Completed"
    )
    open_positions = open_role_positions + open_task_positions

    top_volunteers = sorted(
        volunteers,
        key=lambda x: int(x.get("hoursThisMonth", 0) or 0),
        reverse=True,
    )[:5]

    return {
        "success": True,
        "data": {
            "totalVolunteers": total_volunteers,
            "newThisMonth": new_this_month,
            "newVolunteersThisMonth": new_this_month,
            "activeVolunteers": active_volunteers,
            "inactiveVolunteers": inactive_volunteers,
            "activeRate": active_rate,
            "retentionRate": active_rate,
            "hoursThisMonth": hours_this_month,
            "averageHoursPerVolunteer": avg_hours,
            "avgHoursPerVolunteer": avg_hours,
            "openPositions": open_positions,
            "openRolePositions": open_role_positions,
            "openTaskPositions": open_task_positions,
            "totalRoles": len(roles),
            "totalTasks": len(tasks),
            "topVolunteers": top_volunteers,
        },
    }


# ---------------------------------------------------------------------------
# Tasks & Scheduling (/church/volunteers/tasks)
# ---------------------------------------------------------------------------
@router.get("/tasks")
async def list_volunteer_tasks(
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
    _, _, _, _, tasks = await _load_church_volunteer_context(
        session, actor.church_id
    )
    if search:
        q = search.strip().lower()
        tasks = [
            t
            for t in tasks
            if q
            in f"{t.get('title', '')} {t.get('department', '')} {t.get('description', '')}".lower()
        ]
    if status and status.lower() not in ("all", ""):
        tasks = [
            t
            for t in tasks
            if (t.get("status") or "").lower() == status.strip().lower()
        ]
    return {
        "success": True,
        "tasks": tasks,
        "data": tasks,
    }


@router.post("/tasks")
async def create_volunteer_task(
    payload: VolunteerTaskPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    tasks: List[Dict[str, Any]] = list(meta.get("volunteer_tasks") or [])

    tid = f"tsk_{uuid.uuid4().hex[:10]}"
    now_iso = _iso(_now())
    assigned_ids = list(payload.assignedVolunteerIds or [])
    req_cnt = max(1, int(payload.requiredVolunteers or 4))
    if len(assigned_ids) >= req_cnt:
        status_val = "Scheduled"
    elif len(assigned_ids) > 0:
        status_val = "Needs Volunteers"
    else:
        status_val = payload.status or "Open"

    new_task = {
        "id": tid,
        "_id": tid,
        "title": payload.title.strip(),
        "date": payload.date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "time": payload.time or "09:00 AM",
        "department": (payload.department or "General Ministry").strip(),
        "description": payload.description or "",
        "requiredVolunteers": req_cnt,
        "assignedVolunteerIds": assigned_ids,
        "priority": payload.priority or "Medium",
        "status": status_val,
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }
    tasks.insert(0, new_task)
    meta["volunteer_tasks"] = tasks
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, _, _, formatted_tasks = await _load_church_volunteer_context(
        session, actor.church_id
    )
    created = next(
        (t for t in formatted_tasks if t["id"] == tid), new_task
    )
    return {
        "success": True,
        "message": "Volunteer task scheduled successfully",
        "data": created,
    }


@router.put("/tasks/{task_id}")
@router.patch("/tasks/{task_id}")
async def update_volunteer_task(
    task_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    tasks: List[Dict[str, Any]] = list(meta.get("volunteer_tasks") or [])

    target = next(
        (t for t in tasks if t.get("id") == task_id or t.get("_id") == task_id),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail="Volunteer task not found")

    for k in ("title", "date", "time", "department", "description", "priority", "status"):
        if k in payload and payload[k] is not None:
            target[k] = payload[k]
    if "requiredVolunteers" in payload and payload["requiredVolunteers"] is not None:
        target["requiredVolunteers"] = max(1, int(payload["requiredVolunteers"] or 1))
    if "assignedVolunteerIds" in payload and payload["assignedVolunteerIds"] is not None:
        target["assignedVolunteerIds"] = list(payload["assignedVolunteerIds"])

    target["updatedAt"] = _iso(_now())
    meta["volunteer_tasks"] = tasks
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, _, _, formatted_tasks = await _load_church_volunteer_context(
        session, actor.church_id
    )
    updated = next((t for t in formatted_tasks if t["id"] == task_id), target)
    return {
        "success": True,
        "message": "Volunteer task updated successfully",
        "data": updated,
    }


@router.post("/tasks/{task_id}/assign")
async def assign_volunteers_to_task(
    task_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    tasks: List[Dict[str, Any]] = list(meta.get("volunteer_tasks") or [])

    target = next(
        (t for t in tasks if t.get("id") == task_id or t.get("_id") == task_id),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail="Volunteer task not found")

    vol_ids = list(payload.get("volunteerIds") or [])
    target["assignedVolunteerIds"] = vol_ids
    req_cnt = max(1, int(target.get("requiredVolunteers", 4) or 4))
    if len(vol_ids) >= req_cnt:
        target["status"] = "Scheduled"
    elif len(vol_ids) > 0:
        target["status"] = "Needs Volunteers"
    else:
        target["status"] = "Open"
    target["updatedAt"] = _iso(_now())

    meta["volunteer_tasks"] = tasks
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, _, _, formatted_tasks = await _load_church_volunteer_context(
        session, actor.church_id
    )
    updated = next((t for t in formatted_tasks if t["id"] == task_id), target)
    return {
        "success": True,
        "message": "Volunteers assigned to task successfully",
        "data": updated,
    }


@router.delete("/tasks/{task_id}")
async def delete_volunteer_task(
    task_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    tasks: List[Dict[str, Any]] = list(meta.get("volunteer_tasks") or [])
    meta["volunteer_tasks"] = [
        t for t in tasks if t.get("id") != task_id and t.get("_id") != task_id
    ]
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True, "message": "Volunteer task deleted successfully"}


# ---------------------------------------------------------------------------
# Volunteer Roles (/church/volunteers/roles)
# ---------------------------------------------------------------------------
@router.get("/roles")
async def list_volunteer_roles(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    search: str = Query(""),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    _, _, _, roles, _ = await _load_church_volunteer_context(
        session, actor.church_id
    )
    if search:
        q = search.strip().lower()
        roles = [
            r
            for r in roles
            if q
            in f"{r.get('title', '')} {r.get('department', '')} {r.get('description', '')}".lower()
        ]
    return {
        "success": True,
        "roles": roles,
        "data": roles,
    }


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
    role_title = (payload.role or payload.title or "").strip()
    if not role_title:
        raise HTTPException(status_code=400, detail="Role title is required")

    role = VolunteerRole(
        church_id=actor.church_id,
        role=role_title,
        department=(payload.department or "General Ministry").strip(),
        description=payload.description or "",
        is_active=payload.isActive if payload.isActive is not None else True,
    )
    session.add(role)
    await session.flush()

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    role_meta_map: Dict[str, Any] = dict(meta.get("volunteer_role_meta") or {})
    role_meta_map[str(role.id)] = {
        "requiredSkills": list(payload.requiredSkills or []),
        "timeCommitment": (payload.timeCommitment or "2 hours/week").strip(),
        "neededVolunteers": max(0, int(payload.neededVolunteers if payload.neededVolunteers is not None else 2)),
        "priority": payload.priority or "Medium",
    }
    meta["volunteer_role_meta"] = role_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    await session.commit()
    await session.refresh(role)

    _, _, _, roles, _ = await _load_church_volunteer_context(
        session, actor.church_id
    )
    created = next((r for r in roles if r["id"] == str(role.id)), None)
    return {
        "success": True,
        "message": "Volunteer role created successfully",
        "data": created,
        **(created or {}),
    }


@router.put("/roles/{role_id}")
@router.patch("/roles/{role_id}")
async def update_volunteer_role(
    role_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    role = (
        await session.execute(
            select(VolunteerRole).where(
                VolunteerRole.id == role_id,
                VolunteerRole.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Volunteer role not found")

    title_val = payload.get("role") or payload.get("title")
    if title_val is not None and str(title_val).strip():
        role.role = str(title_val).strip()
    if "department" in payload and payload["department"] is not None:
        role.department = str(payload["department"]).strip()
    if "description" in payload and payload["description"] is not None:
        role.description = str(payload["description"])
    if "isActive" in payload and payload["isActive"] is not None:
        role.is_active = bool(payload["isActive"])

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    role_meta_map: Dict[str, Any] = dict(meta.get("volunteer_role_meta") or {})
    rmeta = dict(role_meta_map.get(str(role.id)) or {})

    if "requiredSkills" in payload and payload["requiredSkills"] is not None:
        rs = payload["requiredSkills"]
        if isinstance(rs, str):
            rmeta["requiredSkills"] = [s.strip() for s in rs.split(",") if s.strip()]
        elif isinstance(rs, list):
            rmeta["requiredSkills"] = [str(s).strip() for s in rs if str(s).strip()]
    if "timeCommitment" in payload and payload["timeCommitment"] is not None:
        rmeta["timeCommitment"] = str(payload["timeCommitment"]).strip()
    if "neededVolunteers" in payload and payload["neededVolunteers"] is not None:
        rmeta["neededVolunteers"] = max(0, int(payload["neededVolunteers"] or 0))
    if "priority" in payload and payload["priority"] is not None:
        rmeta["priority"] = str(payload["priority"])

    role_meta_map[str(role.id)] = rmeta
    meta["volunteer_role_meta"] = role_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, _, roles, _ = await _load_church_volunteer_context(
        session, actor.church_id
    )
    updated = next((r for r in roles if r["id"] == str(role.id)), None)
    return {
        "success": True,
        "message": "Volunteer role updated successfully",
        "data": updated,
    }


@router.post("/roles/{role_id}/assign")
async def assign_volunteers_to_role(
    role_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    role = (
        await session.execute(
            select(VolunteerRole).where(
                VolunteerRole.id == role_id,
                VolunteerRole.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Volunteer role not found")

    vol_ids = [str(v) for v in (payload.get("volunteerIds") or []) if v]
    # Replace active assignments for this role
    await session.execute(
        delete(VolunteerAssignment).where(
            VolunteerAssignment.volunteer_role_id == role.id
        )
    )
    for vid in vol_ids:
        session.add(
            VolunteerAssignment(
                volunteer_details_id=vid,
                volunteer_role_id=role.id,
                status=AssignmentStatus.CONFIRMED,
                is_active=True,
            )
        )
    await session.commit()

    _, _, _, roles, _ = await _load_church_volunteer_context(
        session, actor.church_id
    )
    updated = next((r for r in roles if r["id"] == str(role.id)), None)
    return {
        "success": True,
        "message": "Volunteers assigned to role successfully",
        "data": updated,
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
                VolunteerRole.id == role_id,
                VolunteerRole.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()

    if not role:
        raise HTTPException(status_code=404, detail="Volunteer role not found")

    await session.execute(
        delete(VolunteerAssignment).where(
            VolunteerAssignment.volunteer_role_id == role.id
        )
    )
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    role_meta_map: Dict[str, Any] = dict(meta.get("volunteer_role_meta") or {})
    role_meta_map.pop(str(role.id), None)
    meta["volunteer_role_meta"] = role_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    await session.delete(role)
    await session.commit()
    return {
        "success": True,
        "status": "success",
        "message": "Volunteer role deleted successfully",
    }


# ---------------------------------------------------------------------------
# Volunteers List & CRUD (/church/volunteers)
# ---------------------------------------------------------------------------
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
    status: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    _, _, all_volunteers, _, _ = await _load_church_volunteer_context(
        session, actor.church_id
    )

    filtered = []
    for v in all_volunteers:
        if search:
            q = search.strip().lower()
            haystack = f"{v.get('name', '')} {v.get('email', '')} {v.get('phone', '')} {' '.join(v.get('skills') or [])} {' '.join(v.get('currentRoles') or [])}".lower()
            if q not in haystack:
                continue
        if status and status.lower() not in ("all", ""):
            if (v.get("status") or "Active").lower() != status.strip().lower():
                continue
        filtered.append(v)

    total = len(filtered)
    pages = max(1, math.ceil(total / limit)) if limit else 1
    offset = (page - 1) * limit
    paginated = filtered[offset : offset + limit]
    response.headers["X-Total-Count"] = str(total)

    return {
        "success": True,
        "volunteers": paginated,
        "data": paginated,
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
    church = await _get_church(session, actor.church_id)

    raw_mem_id = payload.memberId
    mem_obj: Optional[Member] = None

    if raw_mem_id:
        mem_obj = (
            await session.execute(
                select(Member)
                .where(
                    or_(Member.id == raw_mem_id, Member.user_id == raw_mem_id),
                    Member.church_id == actor.church_id,
                    Member.deleted_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    if not mem_obj and payload.volunteerEmail:
        mem_obj = (
            await session.execute(
                select(Member)
                .outerjoin(User, User.id == Member.user_id)
                .where(
                    or_(
                        Member.email == payload.volunteerEmail.strip(),
                        User.email == payload.volunteerEmail.strip(),
                    ),
                    Member.church_id == actor.church_id,
                    Member.deleted_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    # If still no member found, create a new Member row for this volunteer
    if not mem_obj:
        full_name = (payload.volunteerName or "Volunteer").strip()
        parts = full_name.split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else "Volunteer"
        branch = await _get_or_create_default_branch(session, church)
        mem_obj = Member(
            church_id=actor.church_id,
            branch_id=branch.id,
            first_name=first_name,
            last_name=last_name,
            email=(payload.volunteerEmail or "").strip() or None,
            phone_number=(payload.volunteerPhone or "").strip() or None,
            status=MembershipStatus.ACTIVE,
            member_type=MemberType.MEMBER,
        )
        session.add(mem_obj)
        await session.flush()

    mem_id = str(mem_obj.id)
    existing = (
        await session.execute(
            select(VolunteerDetails).where(VolunteerDetails.member_id == mem_id)
        )
    ).scalar_one_or_none()

    hours_val = (
        payload.hoursThisMonth
        if payload.hoursThisMonth is not None
        else (payload.hoursContributed or 0)
    )

    if existing:
        existing.hours_contributed = int(hours_val or existing.hours_contributed or 0)
        vol = existing
    else:
        vol = VolunteerDetails(
            member_id=mem_id,
            hours_contributed=int(hours_val or 0),
        )
        session.add(vol)
        await session.flush()

    # Sync role assignments if roleIds provided
    if payload.roleIds is not None:
        await session.execute(
            delete(VolunteerAssignment).where(
                VolunteerAssignment.volunteer_details_id == vol.id
            )
        )
        for rid in payload.roleIds:
            session.add(
                VolunteerAssignment(
                    volunteer_details_id=vol.id,
                    volunteer_role_id=rid,
                    status=AssignmentStatus.CONFIRMED,
                    is_active=True,
                )
            )

    meta = dict(church.metadata_json or {})
    vol_meta_map: Dict[str, Any] = dict(meta.get("volunteer_meta") or {})
    vol_meta_map[str(vol.id)] = {
        "name": (payload.volunteerName or mem_obj.full_name).strip(),
        "email": (payload.volunteerEmail or mem_obj.email or "").strip(),
        "phone": (payload.volunteerPhone or mem_obj.phone_number or "").strip(),
        "skills": list(payload.skills or []),
        "availability": list(payload.availability or []),
        "notes": payload.notes or "",
        "status": payload.status or "Active",
        "hoursThisMonth": int(hours_val or 0),
    }
    meta["volunteer_meta"] = vol_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

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

    _, _, all_volunteers, _, _ = await _load_church_volunteer_context(
        session, actor.church_id
    )
    created = next(
        (v for v in all_volunteers if v["id"] == str(vol.id)), None
    )
    return {
        "success": True,
        "status": "success",
        "message": "Volunteer registered successfully",
        "data": created,
    }


@router.put("/{volunteer_id}")
@router.patch("/{volunteer_id}")
async def update_volunteer(
    volunteer_id: str,
    payload: VolunteerUpdatePayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    row = (
        await session.execute(
            select(VolunteerDetails, Member)
            .join(Member, Member.id == VolunteerDetails.member_id)
            .where(
                VolunteerDetails.id == volunteer_id,
                Member.church_id == actor.church_id,
            )
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Volunteer not found")

    vol, mem_obj = row
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    vol_meta_map: Dict[str, Any] = dict(meta.get("volunteer_meta") or {})
    vmeta = dict(vol_meta_map.get(str(vol.id)) or {})

    if payload.volunteerName is not None and payload.volunteerName.strip():
        vmeta["name"] = payload.volunteerName.strip()
    if payload.volunteerEmail is not None:
        vmeta["email"] = payload.volunteerEmail.strip()
    if payload.volunteerPhone is not None:
        vmeta["phone"] = payload.volunteerPhone.strip()
    if payload.skills is not None:
        vmeta["skills"] = list(payload.skills)
    if payload.availability is not None:
        vmeta["availability"] = list(payload.availability)
    if payload.notes is not None:
        vmeta["notes"] = payload.notes
    if payload.status is not None:
        vmeta["status"] = payload.status

    new_hours = (
        payload.hoursThisMonth
        if payload.hoursThisMonth is not None
        else payload.hoursContributed
    )
    if new_hours is not None:
        vol.hours_contributed = max(0, int(new_hours))
        vmeta["hoursThisMonth"] = max(0, int(new_hours))

    if payload.roleIds is not None:
        await session.execute(
            delete(VolunteerAssignment).where(
                VolunteerAssignment.volunteer_details_id == vol.id
            )
        )
        for rid in payload.roleIds:
            session.add(
                VolunteerAssignment(
                    volunteer_details_id=vol.id,
                    volunteer_role_id=rid,
                    status=AssignmentStatus.CONFIRMED,
                    is_active=True,
                )
            )

    vol_meta_map[str(vol.id)] = vmeta
    meta["volunteer_meta"] = vol_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, _, all_volunteers, _, _ = await _load_church_volunteer_context(
        session, actor.church_id
    )
    updated = next(
        (v for v in all_volunteers if v["id"] == str(vol.id)), None
    )
    return {
        "success": True,
        "status": "success",
        "message": "Volunteer updated successfully",
        "data": updated,
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
            .where(
                VolunteerDetails.id == volunteer_id,
                Member.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()

    if not vol:
        raise HTTPException(status_code=404, detail="Volunteer not found")

    await session.execute(
        delete(VolunteerAssignment).where(
            VolunteerAssignment.volunteer_details_id == vol.id
        )
    )
    await session.execute(
        delete(AvailabilitySchedule).where(
            AvailabilitySchedule.volunteer_details_id == vol.id
        )
    )
    await session.execute(
        delete(BackgroundCheck).where(
            BackgroundCheck.volunteer_details_id == vol.id
        )
    )

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    vol_meta_map: Dict[str, Any] = dict(meta.get("volunteer_meta") or {})
    vol_meta_map.pop(str(vol.id), None)
    meta["volunteer_meta"] = vol_meta_map
    # Also remove volunteer from any scheduled tasks
    tasks: List[Dict[str, Any]] = list(meta.get("volunteer_tasks") or [])
    for t in tasks:
        if "assignedVolunteerIds" in t and isinstance(t["assignedVolunteerIds"], list):
            t["assignedVolunteerIds"] = [
                vid for vid in t["assignedVolunteerIds"] if vid != str(vol.id)
            ]
    meta["volunteer_tasks"] = tasks
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    await session.delete(vol)
    await session.commit()
    return {
        "success": True,
        "status": "success",
        "message": "Volunteer removed successfully",
    }

from datetime import datetime, timezone
import json
import math
import uuid
from typing import Any, Dict, List, Optional

from app.core.deps import CurrentUser, DbSession, church_id_from, require, resolve_actor
from app.models.church import Branch
from app.models.group import Department, Group, GroupMember
from app.models.member import Member
from app.models.user import User
from app.schemas.group import GroupCreate, GroupResponse
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select

router = APIRouter()

META_PREFIX = "<!--group_meta:"
META_SUFFIX = "-->\n"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_group_meta(raw_desc: Optional[str]) -> tuple[str, Dict[str, Any]]:
    raw = raw_desc or ""
    if raw.startswith(META_PREFIX) and META_SUFFIX in raw:
        idx = raw.index(META_SUFFIX)
        meta_str = raw[len(META_PREFIX) : idx]
        clean_desc = raw[idx + len(META_SUFFIX) :]
        try:
            return clean_desc, json.loads(meta_str)
        except Exception:
            return clean_desc, {}
    return raw, {}


def _build_group_desc(clean_desc: str, meta: Dict[str, Any]) -> str:
    if not meta:
        return clean_desc
    return f"{META_PREFIX}{json.dumps(meta)}{META_SUFFIX}{clean_desc}"


async def _resolve_member_brief(session: DbSession, raw_id: Optional[str], church_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
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
        "phoneNumber": (m.phone_number if m and m.phone_number else (u.phone_number if u else "")) or "",
        "profilePictureUrl": (m.photo_url if m and m.photo_url else (u.profile_picture_url if u else "")) or "",
        "membershipNumber": m.membership_number or "",
        "isLocked": False,
    }


async def _format_group(session: DbSession, group: Group) -> Dict[str, Any]:
    clean_desc, meta = _parse_group_meta(group.description)
    leader_info = await _resolve_member_brief(session, group.leader_member_id, group.church_id)

    member_count = (
        await session.execute(
            select(func.count(GroupMember.id)).where(GroupMember.group_id == group.id)
        )
    ).scalar() or 0

    try:
        capacity = int(meta.get("capacity", 20))
    except (ValueError, TypeError):
        capacity = 20

    goals = meta.get("goals") if isinstance(meta.get("goals"), list) else []
    activities = meta.get("activities") if isinstance(meta.get("activities"), list) else []
    completed_goals = sum(1 for g in goals if g.get("status") == "completed")

    avg_attendance = 0
    if activities and member_count > 0:
        rates = [
            min(100, round((len(a.get("participants", [])) / max(1, member_count)) * 100))
            for a in activities
        ]
        avg_attendance = round(sum(rates) / len(rates)) if rates else 0

    return {
        "_id": str(group.id),
        "id": str(group.id),
        "churchId": group.church_id,
        "groupName": group.name,
        "name": group.name,
        "description": clean_desc,
        "isActive": group.is_active,
        "is_active": group.is_active,
        "branchId": group.branch_id,
        "departmentId": group.department_id,
        "leaderId": leader_info,
        "leader": leader_info,
        "capacity": capacity,
        "memberCount": member_count,
        "members": member_count,
        "location": meta.get("location") or "Church Facility",
        "category": meta.get("category") or "Small Group",
        "establishedDate": meta.get("establishedDate") or _iso(group.created_at),
        "meetingDay": meta.get("meetingDay") if isinstance(meta.get("meetingDay"), list) else ["Wednesday"],
        "meetingTime": meta.get("meetingTime") if isinstance(meta.get("meetingTime"), list) else ["06:00 PM"],
        "stats": {
            "totalMembers": member_count,
            "activeMembers": member_count,
            "averageAttendance": avg_attendance,
            "totalActivities": len(activities),
            "completedGoals": completed_goals,
        },
        "createdAt": _iso(group.created_at),
        "updatedAt": _iso(group.updated_at),
    }


class GroupPayload(BaseModel):
    groupName: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    leaderId: Optional[str] = None
    leader_member_id: Optional[str] = None
    branchId: Optional[str] = None
    branch_id: Optional[str] = None
    departmentId: Optional[str] = None
    department_id: Optional[str] = None
    category: Optional[str] = None
    capacity: Optional[Any] = None
    location: Optional[str] = None
    establishedDate: Optional[str] = None
    meetingDay: Optional[List[str]] = None
    meetingTime: Optional[List[str]] = None
    isActive: Optional[bool] = None


def _normalize_category_key(cat: Optional[str]) -> str:
    raw = (cat or "others").strip().lower().replace("_", "-").replace(" ", "-")
    return raw


def _format_category_label(cat_key: str) -> str:
    return " ".join(w.capitalize() for w in cat_key.replace("-", " ").split())


CATEGORY_COLORS = {
    "bible-study": "#3b82f6",
    "youth": "#10b981",
    "prayer": "#f59e0b",
    "fellowship": "#ef4444",
    "worship": "#8b5cf6",
    "children": "#06b6d4",
    "marriage": "#84cc16",
    "contribution": "#ec4899",
    "others": "#64748b",
}


async def _compute_groups_analytics(session: DbSession, church_id: str) -> Dict[str, Any]:
    all_groups = (
        await session.execute(
            select(Group).where(Group.church_id == church_id).order_by(Group.name, Group.id)
        )
    ).scalars().all()

    formatted_all = [await _format_group(session, g) for g in all_groups]

    total_groups = len(formatted_all)
    active_groups = sum(1 for g in formatted_all if g.get("isActive"))
    total_members = sum(int(g.get("memberCount", 0) or 0) for g in formatted_all)
    total_capacity = sum(int(g.get("capacity", 0) or 0) for g in formatted_all)

    now = _now()
    current_ym = now.strftime("%Y-%m")

    # Last 6 months labels & buckets
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
            "year": y_num,
            "month": m_num,
        })

    # Gather all GroupMember rows with joined dates for this church
    gm_rows = (
        await session.execute(
            select(GroupMember, Group)
            .join(Group, Group.id == GroupMember.group_id)
            .where(Group.church_id == church_id)
        )
    ).all()

    member_events: List[Dict[str, Any]] = []
    for gm, grp in gm_rows:
        _, meta = _parse_group_meta(grp.description)
        roles_map = meta.get("memberRoles") if isinstance(meta.get("memberRoles"), dict) else {}
        entry = roles_map.get(str(gm.member_id), {})
        j_str = entry.get("joinedDate") or _iso(gm.joined_at) or _iso(grp.created_at) or _iso(now)
        is_act = bool(entry.get("isActive", True))
        ym = str(j_str)[:7] if j_str else current_ym
        member_events.append({"ym": ym, "isActive": is_act})

    attendance_data: List[Dict[str, Any]] = []
    for b in month_buckets:
        ym = b["ym"]
        cum_total = sum(1 for e in member_events if e["ym"] <= ym)
        cum_active = sum(1 for e in member_events if e["ym"] <= ym and e["isActive"])
        new_in_m = sum(1 for e in member_events if e["ym"] == ym)
        attendance_data.append({
            "month": b["label"],
            "total": cum_total,
            "active": cum_active,
            "new": new_in_m,
        })

    # Category distribution (by group count, or member count if requested, plus group count)
    cat_counts: Dict[str, int] = {}
    for g in formatted_all:
        ckey = _normalize_category_key(g.get("category"))
        cat_counts[ckey] = cat_counts.get(ckey, 0) + 1

    category_distribution: List[Dict[str, Any]] = []
    palette = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4", "#84cc16", "#ec4899", "#64748b"]
    for idx, (ckey, cnt) in enumerate(cat_counts.items()):
        category_distribution.append({
            "name": _format_category_label(ckey),
            "category": ckey,
            "value": cnt,
            "color": CATEGORY_COLORS.get(ckey, palette[idx % len(palette)]),
        })

    # Meeting days distribution
    days_order = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    day_counts = {d.lower(): 0 for d in days_order}
    for g in formatted_all:
        m_days = g.get("meetingDay") if isinstance(g.get("meetingDay"), list) else []
        for d in m_days:
            dl = str(d).strip().lower()
            if dl in day_counts:
                day_counts[dl] += 1

    meeting_days = [{"day": d, "groups": day_counts[d.lower()]} for d in days_order]

    # Monthly activities & activity trends across last 6 months
    monthly_activities_count = 0
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

    for g in all_groups:
        _, meta = _parse_group_meta(g.description)
        ckey = _normalize_category_key(meta.get("category"))
        acts = meta.get("activities") if isinstance(meta.get("activities"), list) else []
        for a in acts:
            a_date = str(a.get("date") or a.get("createdAt") or "")
            a_ym = a_date[:7] if len(a_date) >= 7 else current_ym
            if a_ym == current_ym:
                monthly_activities_count += 1
            if a_ym in act_buckets:
                bucket = act_buckets[a_ym]
                bucket["total"] += 1
                a_type = str(a.get("type") or "").lower()
                if "worship" in ckey or a_type == "rehearsal":
                    bucket["worship"] += 1
                elif "children" in ckey:
                    bucket["children"] += 1
                elif "youth" in ckey:
                    bucket["youth"] += 1
                elif "outreach" in a_type or "outreach" in ckey:
                    bucket["outreach"] += 1
                elif "bible" in ckey or "training" in a_type or "workshop" in a_type:
                    bucket["education"] += 1
                elif "prayer" in ckey or "prayer" in a_type:
                    bucket["prayer"] += 1
                else:
                    bucket["worship"] += 1

    activity_data = [act_buckets[b["ym"]] for b in month_buckets]

    return {
        "totalGroups": total_groups,
        "activeGroups": active_groups,
        "totalMembers": total_members,
        "totalCapacity": total_capacity,
        "capacityUtilization": round((total_members / max(1, total_capacity)) * 100) if total_capacity > 0 else 0,
        "availableSpots": max(0, total_capacity - total_members),
        "monthlyActivities": monthly_activities_count,
        "attendanceData": attendance_data,
        "categoryDistribution": category_distribution,
        "meetingDays": meeting_days,
        "activityData": activity_data,
    }


@router.get("/analytics")
async def get_groups_analytics(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    analytics = await _compute_groups_analytics(session, actor.church_id)
    return {
        "success": True,
        "data": analytics,
        "analytics": analytics,
    }


@router.get("")
@router.get("/")
async def get_groups(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
    branch_id: Optional[str] = Query(None),
    department_id: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    category: Optional[str] = Query(None),
    meetingDay: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    search: str = Query("", max_length=100),
    offset: Optional[int] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Group.church_id == actor.church_id]
    if branch_id:
        conds.append(Group.branch_id == branch_id)
    if department_id:
        conds.append(Group.department_id == department_id)
    if is_active is not None:
        conds.append(Group.is_active == is_active)
    if search:
        s = f"%{search.strip()}%"
        conds.append(or_(Group.name.ilike(s), Group.description.ilike(s)))

    rows = (
        (
            await session.execute(
                select(Group)
                .where(*conds)
                .order_by(Group.name, Group.id)
            )
        )
        .scalars()
        .all()
    )

    all_formatted = [await _format_group(session, g) for g in rows]

    # Apply metadata-based filters (category & meetingDay)
    filtered_groups: List[Dict[str, Any]] = []
    for fg in all_formatted:
        if category and category.strip().lower() not in ("", "all"):
            want_cat = _normalize_category_key(category)
            have_cat = _normalize_category_key(fg.get("category"))
            if want_cat != have_cat:
                continue
        if meetingDay and meetingDay.strip().lower() not in ("", "all"):
            want_day = meetingDay.strip().lower()
            have_days = [str(d).strip().lower() for d in (fg.get("meetingDay") or [])]
            if want_day not in have_days:
                continue
        filtered_groups.append(fg)

    total = len(filtered_groups)
    if offset is None:
        offset = (page - 1) * limit

    paginated_groups = filtered_groups[offset : offset + limit]
    pages = max(1, math.ceil(total / limit)) if limit else 1
    response.headers["X-Total-Count"] = str(total)

    analytics = await _compute_groups_analytics(session, actor.church_id)

    # Return structure matching frontend GroupListResponse: { groups: [...], pagination: { ... }, analytics: { ... } }
    return {
        "groups": paginated_groups,
        "data": paginated_groups,
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
async def create_group(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: GroupPayload,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    name = (payload.groupName or payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")

    raw_leader_id = payload.leaderId or payload.leader_member_id
    leader_id = None
    if raw_leader_id:
        leader_exists = (
            await session.execute(
                select(Member.id).where(
                    or_(Member.id == raw_leader_id, Member.user_id == raw_leader_id),
                    Member.church_id == actor.church_id,
                    Member.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if leader_exists:
            leader_id = str(leader_exists)

    branch_id = payload.branchId or payload.branch_id
    if branch_id:
        branch_exists = (
            await session.execute(
                select(Branch.id).where(
                    Branch.id == branch_id, Branch.church_id == actor.church_id
                )
            )
        ).scalar_one_or_none()
        if not branch_exists:
            branch_id = None

    dept_id = payload.departmentId or payload.department_id

    meta: Dict[str, Any] = {}
    if payload.category:
        meta["category"] = payload.category
    if payload.capacity is not None:
        meta["capacity"] = payload.capacity
    if payload.location:
        meta["location"] = payload.location
    if payload.establishedDate:
        meta["establishedDate"] = payload.establishedDate
    if payload.meetingDay:
        meta["meetingDay"] = payload.meetingDay
    if payload.meetingTime:
        meta["meetingTime"] = payload.meetingTime

    group = Group(
        name=name,
        church_id=actor.church_id,
        branch_id=branch_id,
        department_id=dept_id,
        leader_member_id=leader_id,
        description=_build_group_desc(payload.description or "", meta),
        is_active=payload.isActive if payload.isActive is not None else True,
    )
    session.add(group)
    await session.flush()

    if leader_id:
        session.add(
            GroupMember(
                group_id=group.id,
                member_id=leader_id,
            )
        )
        member_roles = meta.get("memberRoles") if isinstance(meta.get("memberRoles"), dict) else {}
        member_roles[str(leader_id)] = {
            "role": "leader",
            "joinedDate": payload.establishedDate or _iso(_now()),
            "isActive": True,
            "notes": "",
        }
        meta["memberRoles"] = member_roles
        group.description = _build_group_desc(payload.description or "", meta)

    await session.commit()
    await session.refresh(group)

    queue_audit(
        background_tasks,
        request,
        "CREATE_GROUP",
        "GROUP",
        group.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    formatted = await _format_group(session, group)
    return {
        "status": "success",
        "message": "Group created successfully",
        "group": formatted,
        "data": formatted,
    }


@router.get("/{group_id}")
async def get_group(
    group_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(
                Group.id == group_id, Group.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    formatted = await _format_group(session, group)
    return {
        "group": formatted,
        "data": {"group": formatted},
    }


@router.put("/{group_id}")
async def update_group(
    group_id: str,
    payload: GroupPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(
                Group.id == group_id, Group.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    clean_desc, meta = _parse_group_meta(group.description)
    name = payload.groupName or payload.name
    if name is not None:
        group.name = name.strip()
    if payload.description is not None:
        clean_desc, _ = _parse_group_meta(payload.description)
    if payload.category is not None:
        meta["category"] = payload.category
    if payload.capacity is not None:
        meta["capacity"] = payload.capacity
    if payload.location is not None:
        meta["location"] = payload.location
    if payload.establishedDate is not None:
        meta["establishedDate"] = payload.establishedDate
    if payload.meetingDay is not None:
        meta["meetingDay"] = payload.meetingDay
    if payload.meetingTime is not None:
        meta["meetingTime"] = payload.meetingTime
    group.description = _build_group_desc(clean_desc, meta)

    if payload.leaderId is not None or payload.leader_member_id is not None:
        raw_leader_id = payload.leaderId or payload.leader_member_id
        if raw_leader_id:
            leader_exists = (
                await session.execute(
                    select(Member.id).where(
                        or_(Member.id == raw_leader_id, Member.user_id == raw_leader_id),
                        Member.church_id == actor.church_id,
                        Member.deleted_at.is_(None),
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if leader_exists:
                group.leader_member_id = str(leader_exists)
                existing_gm = (
                    await session.execute(
                        select(GroupMember).where(
                            GroupMember.group_id == group.id,
                            GroupMember.member_id == str(leader_exists),
                        ).limit(1)
                    )
                ).scalar_one_or_none()
                if not existing_gm:
                    session.add(GroupMember(group_id=group.id, member_id=str(leader_exists)))
                member_roles = meta.get("memberRoles") if isinstance(meta.get("memberRoles"), dict) else {}
                entry = member_roles.get(str(leader_exists), {})
                entry["role"] = "leader"
                entry.setdefault("joinedDate", _iso(_now()))
                entry.setdefault("isActive", True)
                member_roles[str(leader_exists)] = entry
                meta["memberRoles"] = member_roles
                group.description = _build_group_desc(clean_desc, meta)
            else:
                group.leader_member_id = None
        else:
            group.leader_member_id = None
    if payload.isActive is not None:
        group.is_active = payload.isActive

    group.updated_at = _now()
    await session.commit()
    await session.refresh(group)

    queue_audit(
        background_tasks,
        request,
        "UPDATE_GROUP",
        "GROUP",
        group.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    formatted = await _format_group(session, group)
    return {
        "status": "success",
        "message": "Group updated successfully",
        "group": formatted,
        "data": formatted,
    }


@router.delete("/{group_id}")
async def delete_group(
    group_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    force: bool = Query(False),
):
    group = (
        await session.execute(select(Group).where(Group.id == group_id))
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    actor = await resolve_actor(session, current_user, group.church_id)
    await session.execute(delete(GroupMember).where(GroupMember.group_id == group.id))
    await session.execute(delete(Group).where(Group.id == group.id))
    await session.commit()

    queue_audit(
        background_tasks,
        request,
        "DELETE_GROUP",
        "GROUP",
        group_id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    return {"status": "success", "message": "Group deleted successfully"}


# ==================== GROUP MEMBERS ====================


class GroupMemberPayload(BaseModel):
    userId: Optional[str] = None
    memberId: Optional[str] = None
    role: Optional[str] = None
    joinedDate: Optional[str] = None
    isActive: Optional[bool] = None
    notes: Optional[str] = None


@router.get("/{group_id}/members")
async def list_group_members(
    group_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=500),
    search: str = Query(""),
    role: Optional[str] = Query(None),
    isActive: Optional[bool] = Query(None),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    _, meta = _parse_group_meta(group.description)
    member_roles = meta.get("memberRoles") if isinstance(meta.get("memberRoles"), dict) else {}
    try:
        capacity = int(meta.get("capacity", 20))
    except (ValueError, TypeError):
        capacity = 20

    rows = (
        await session.execute(
            select(GroupMember, Member, User)
            .join(Member, Member.id == GroupMember.member_id)
            .outerjoin(User, User.id == Member.user_id)
            .where(GroupMember.group_id == group.id)
        )
    ).all()

    all_members: List[Dict[str, Any]] = []
    for gm, m, u in rows:
        first_name = (m.first_name if m and m.first_name else "") or ""
        last_name = (m.last_name if m and m.last_name else "") or ""
        if not first_name and not last_name and u and u.display_name:
            parts = u.display_name.strip().split(" ", 1)
            first_name = parts[0] if parts else ""
            last_name = parts[1] if len(parts) > 1 else ""
        meta_entry = member_roles.get(str(m.id), {})
        m_role = meta_entry.get("role") or ("leader" if group.leader_member_id == m.id else "member")
        m_joined = meta_entry.get("joinedDate") or _iso(gm.joined_at) or _iso(_now())
        m_active = meta_entry.get("isActive", True)
        m_notes = meta_entry.get("notes") or ""

        email = (m.email if m and m.email else (u.email if u else "")) or ""
        phone = (m.phone_number if m and m.phone_number else (u.phone_number if u else "")) or ""
        avatar = (m.photo_url if m and m.photo_url else (u.profile_picture_url if u else "")) or ""

        if search:
            q = search.strip().lower()
            if q not in f"{first_name} {last_name} {email} {phone}".lower():
                continue
        if role and role.lower() != "all" and m_role.lower() != role.lower():
            continue
        if isActive is not None and bool(m_active) != isActive:
            continue

        all_members.append(
            {
                "_id": str(gm.id),
                "id": str(gm.id),
                "memberId": str(m.id),
                "userId": {
                    "_id": str(m.id),
                    "id": str(m.id),
                    "firstName": first_name,
                    "lastName": last_name,
                    "email": email,
                    "phoneNumber": phone,
                    "profilePictureUrl": avatar,
                },
                "role": m_role,
                "joinedDate": m_joined,
                "isActive": bool(m_active),
                "notes": m_notes,
            }
        )

    total = len(all_members)
    active_count = sum(1 for x in all_members if x["isActive"])
    inactive_count = total - active_count
    leaders_count = sum(1 for x in all_members if x["role"] == "leader")
    assistant_count = sum(1 for x in all_members if x["role"] in ("assistant", "assistant_leader", "co-leader"))
    regular_count = max(0, total - leaders_count - assistant_count)

    total_pages = max(1, math.ceil(total / limit)) if limit else 1
    start = (page - 1) * limit
    paginated = all_members[start : start + limit]

    return {
        "success": True,
        "data": {
            "members": paginated,
            "summary": {
                "total": total,
                "active": active_count,
                "inactive": inactive_count,
                "leaders": leaders_count,
                "assistantLeaders": assistant_count,
                "members": regular_count,
                "capacity": capacity,
                "capacityUsed": min(100, round((total / max(1, capacity)) * 100)),
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


@router.post("/{group_id}/members")
async def add_group_member(
    group_id: str,
    payload: GroupMemberPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    raw_id = payload.userId or payload.memberId
    if not raw_id:
        raise HTTPException(status_code=400, detail="Member ID is required")

    member = (
        await session.execute(
            select(Member).where(
                or_(Member.id == raw_id, Member.user_id == raw_id),
                Member.church_id == actor.church_id,
                Member.deleted_at.is_(None),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    existing = (
        await session.execute(
            select(GroupMember).where(
                GroupMember.group_id == group.id,
                GroupMember.member_id == member.id,
            ).limit(1)
        )
    ).scalar_one_or_none()

    if not existing:
        existing = GroupMember(
            group_id=group.id,
            member_id=member.id,
        )
        session.add(existing)
        await session.flush()

    clean_desc, meta = _parse_group_meta(group.description)
    member_roles = meta.get("memberRoles") if isinstance(meta.get("memberRoles"), dict) else {}
    member_roles[str(member.id)] = {
        "role": payload.role or "member",
        "joinedDate": payload.joinedDate or _iso(_now()),
        "isActive": payload.isActive if payload.isActive is not None else True,
        "notes": payload.notes or "",
    }
    meta["memberRoles"] = member_roles
    if payload.role == "leader":
        group.leader_member_id = str(member.id)
    group.description = _build_group_desc(clean_desc, meta)
    group.updated_at = _now()

    await session.commit()
    return {
        "success": True,
        "message": "Group member added successfully",
        "data": {"id": str(existing.id), "memberId": str(member.id)},
    }


@router.put("/{group_id}/members/{member_id}")
async def update_group_member(
    group_id: str,
    member_id: str,
    payload: GroupMemberPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    gm = (
        await session.execute(
            select(GroupMember).where(
                GroupMember.group_id == group.id,
                or_(GroupMember.id == member_id, GroupMember.member_id == member_id),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not gm:
        raise HTTPException(status_code=404, detail="Group member not found")

    clean_desc, meta = _parse_group_meta(group.description)
    member_roles = meta.get("memberRoles") if isinstance(meta.get("memberRoles"), dict) else {}
    current_entry = member_roles.get(str(gm.member_id), {})
    if payload.role is not None:
        current_entry["role"] = payload.role
        if payload.role == "leader":
            group.leader_member_id = str(gm.member_id)
    if payload.joinedDate is not None:
        current_entry["joinedDate"] = payload.joinedDate
    if payload.isActive is not None:
        current_entry["isActive"] = payload.isActive
    if payload.notes is not None:
        current_entry["notes"] = payload.notes

    member_roles[str(gm.member_id)] = current_entry
    meta["memberRoles"] = member_roles
    group.description = _build_group_desc(clean_desc, meta)
    group.updated_at = _now()
    await session.commit()

    return {"success": True, "message": "Group member updated successfully"}


@router.delete("/{group_id}/members/{member_id}")
async def delete_group_member(
    group_id: str,
    member_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    gm = (
        await session.execute(
            select(GroupMember).where(
                GroupMember.group_id == group.id,
                or_(GroupMember.id == member_id, GroupMember.member_id == member_id),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if gm:
        clean_desc, meta = _parse_group_meta(group.description)
        member_roles = meta.get("memberRoles") if isinstance(meta.get("memberRoles"), dict) else {}
        member_roles.pop(str(gm.member_id), None)
        meta["memberRoles"] = member_roles
        group.description = _build_group_desc(clean_desc, meta)
        await session.delete(gm)
        await session.commit()

    return {"success": True, "message": "Group member removed successfully"}


# ==================== GROUP GOALS ====================


class GroupGoalPayload(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    targetDate: Optional[str] = None
    category: Optional[str] = None
    assignee: Optional[Any] = None
    status: Optional[str] = None
    progress: Optional[int] = None
    success: Optional[str] = None


@router.get("/{group_id}/goals")
async def list_group_goals(
    group_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=500),
    search: str = Query(""),
    priority: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    assigneeId: Optional[str] = Query(None),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    _, meta = _parse_group_meta(group.description)
    goals: List[Dict[str, Any]] = meta.get("goals") if isinstance(meta.get("goals"), list) else []

    filtered: List[Dict[str, Any]] = []
    for g in goals:
        if search:
            q = search.strip().lower()
            if q not in f"{g.get('title', '')} {g.get('description', '')}".lower():
                continue
        if priority and priority.lower() != "all" and str(g.get("priority", "")).lower() != priority.lower():
            continue
        if category and category.lower() != "all" and str(g.get("category", "")).lower() != category.lower():
            continue
        if status and status.lower() != "all" and str(g.get("status", "")).lower() != status.lower():
            continue
        if assigneeId:
            assignee_obj = g.get("assignee")
            a_id = assignee_obj.get("_id") if isinstance(assignee_obj, dict) else str(assignee_obj or "")
            if a_id != assigneeId:
                continue
        filtered.append(g)

    total = len(filtered)
    planned = sum(1 for g in filtered if g.get("status") == "planned")
    in_progress = sum(1 for g in filtered if g.get("status") in ("in-progress", "in_progress"))
    completed = sum(1 for g in filtered if g.get("status") == "completed")
    cancelled = sum(1 for g in filtered if g.get("status") == "cancelled")
    overdue = sum(1 for g in filtered if g.get("status") == "overdue")
    avg_progress = round(sum(int(g.get("progress", 0) or 0) for g in filtered) / total) if total > 0 else 0

    pages = max(1, math.ceil(total / limit)) if limit else 1
    start = (page - 1) * limit
    paginated = filtered[start : start + limit]

    return {
        "success": True,
        "data": {
            "goals": paginated,
            "summary": {
                "total": total,
                "planned": planned,
                "inProgress": in_progress,
                "completed": completed,
                "cancelled": cancelled,
                "overdue": overdue,
                "averageProgress": avg_progress,
            },
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


@router.post("/{group_id}/goals")
async def create_group_goal(
    group_id: str,
    payload: GroupGoalPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    clean_desc, meta = _parse_group_meta(group.description)
    goals: List[Dict[str, Any]] = meta.get("goals") if isinstance(meta.get("goals"), list) else []

    raw_assignee = payload.assignee.get("_id") if isinstance(payload.assignee, dict) else payload.assignee
    assignee_info = await _resolve_member_brief(session, str(raw_assignee) if raw_assignee else None, actor.church_id)

    now_iso = _iso(_now())
    goal_id = str(uuid.uuid4())
    new_goal = {
        "_id": goal_id,
        "id": goal_id,
        "title": (payload.title or "").strip(),
        "description": (payload.description or "").strip(),
        "priority": payload.priority or "medium",
        "targetDate": payload.targetDate or now_iso,
        "category": payload.category or "spiritual",
        "status": payload.status or "planned",
        "progress": payload.progress if payload.progress is not None else 0,
        "assignee": assignee_info,
        "success": payload.success or "",
        "createdBy": str(current_user.id),
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }
    goals.append(new_goal)
    meta["goals"] = goals
    group.description = _build_group_desc(clean_desc, meta)
    group.updated_at = _now()
    await session.commit()

    return {"success": True, "message": "Goal added successfully", "data": new_goal, "goal": new_goal}


@router.put("/{group_id}/goals/{goal_id}")
async def update_group_goal(
    group_id: str,
    goal_id: str,
    payload: GroupGoalPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    clean_desc, meta = _parse_group_meta(group.description)
    goals: List[Dict[str, Any]] = meta.get("goals") if isinstance(meta.get("goals"), list) else []

    target = None
    for g in goals:
        if g.get("_id") == goal_id or g.get("id") == goal_id:
            target = g
            break
    if not target:
        raise HTTPException(status_code=404, detail="Goal not found")

    if payload.title is not None:
        target["title"] = payload.title.strip()
    if payload.description is not None:
        target["description"] = payload.description.strip()
    if payload.priority is not None:
        target["priority"] = payload.priority
    if payload.targetDate is not None:
        target["targetDate"] = payload.targetDate
    if payload.category is not None:
        target["category"] = payload.category
    if payload.status is not None:
        target["status"] = payload.status
    if payload.progress is not None:
        target["progress"] = payload.progress
    if payload.success is not None:
        target["success"] = payload.success
    if payload.assignee is not None:
        raw_assignee = payload.assignee.get("_id") if isinstance(payload.assignee, dict) else payload.assignee
        target["assignee"] = await _resolve_member_brief(session, str(raw_assignee) if raw_assignee else None, actor.church_id)

    target["updatedAt"] = _iso(_now())
    meta["goals"] = goals
    group.description = _build_group_desc(clean_desc, meta)
    group.updated_at = _now()
    await session.commit()

    return {"success": True, "message": "Goal updated successfully", "data": target, "goal": target}


@router.delete("/{group_id}/goals/{goal_id}")
async def delete_group_goal(
    group_id: str,
    goal_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    clean_desc, meta = _parse_group_meta(group.description)
    goals: List[Dict[str, Any]] = meta.get("goals") if isinstance(meta.get("goals"), list) else []
    meta["goals"] = [g for g in goals if g.get("_id") != goal_id and g.get("id") != goal_id]
    group.description = _build_group_desc(clean_desc, meta)
    group.updated_at = _now()
    await session.commit()

    return {"success": True, "message": "Goal deleted successfully"}


# ==================== GROUP ACTIVITIES & ATTENDANCE SUMMARY ====================


class GroupActivityPayload(BaseModel):
    type: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    date: Optional[str] = None
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    location: Optional[str] = None
    participants: Optional[List[Any]] = None
    isCompleted: Optional[bool] = None


@router.get("/{group_id}/activities")
async def list_group_activities(
    group_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=500),
    search: str = Query(""),
    activityType: Optional[str] = Query(None),
    startDate: Optional[str] = Query(None),
    endDate: Optional[str] = Query(None),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    _, meta = _parse_group_meta(group.description)
    activities: List[Dict[str, Any]] = meta.get("activities") if isinstance(meta.get("activities"), list) else []

    filtered: List[Dict[str, Any]] = []
    for a in activities:
        if search:
            q = search.strip().lower()
            if q not in f"{a.get('title', '')} {a.get('description', '')} {a.get('location', '')}".lower():
                continue
        if activityType and activityType.lower() != "all" and str(a.get("type", "")).lower() != activityType.lower():
            continue
        filtered.append(a)

    total = len(filtered)
    pages = max(1, math.ceil(total / limit)) if limit else 1
    start = (page - 1) * limit
    paginated = filtered[start : start + limit]

    return {
        "success": True,
        "activities": paginated,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": pages,
        },
    }


@router.post("/{group_id}/activities")
async def create_group_activity(
    group_id: str,
    payload: GroupActivityPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    clean_desc, meta = _parse_group_meta(group.description)
    activities: List[Dict[str, Any]] = meta.get("activities") if isinstance(meta.get("activities"), list) else []

    resolved_participants: List[Dict[str, Any]] = []
    for p in payload.participants or []:
        p_id = p.get("_id") if isinstance(p, dict) else str(p)
        brief = await _resolve_member_brief(session, p_id, actor.church_id)
        if brief:
            resolved_participants.append(brief)

    now_iso = _iso(_now())
    act_id = str(uuid.uuid4())
    new_act = {
        "_id": act_id,
        "id": act_id,
        "title": (payload.title or "").strip(),
        "description": (payload.description or "").strip(),
        "type": payload.type or "meeting",
        "date": payload.date or now_iso,
        "startTime": payload.startTime or "09:00",
        "endTime": payload.endTime or "10:30",
        "location": (payload.location or "Church Facility").strip(),
        "participants": resolved_participants,
        "attendance": resolved_participants,
        "isCompleted": payload.isCompleted if payload.isCompleted is not None else True,
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }
    activities.append(new_act)
    meta["activities"] = activities
    group.description = _build_group_desc(clean_desc, meta)
    group.updated_at = _now()
    await session.commit()

    return {"success": True, "message": "Activity logged successfully", "activity": new_act, "data": new_act}


@router.put("/{group_id}/activities/{activity_id}")
async def update_group_activity(
    group_id: str,
    activity_id: str,
    payload: GroupActivityPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    clean_desc, meta = _parse_group_meta(group.description)
    activities: List[Dict[str, Any]] = meta.get("activities") if isinstance(meta.get("activities"), list) else []

    target = None
    for a in activities:
        if a.get("_id") == activity_id or a.get("id") == activity_id:
            target = a
            break
    if not target:
        raise HTTPException(status_code=404, detail="Activity not found")

    if payload.title is not None:
        target["title"] = payload.title.strip()
    if payload.description is not None:
        target["description"] = payload.description.strip()
    if payload.type is not None:
        target["type"] = payload.type
    if payload.date is not None:
        target["date"] = payload.date
    if payload.startTime is not None:
        target["startTime"] = payload.startTime
    if payload.endTime is not None:
        target["endTime"] = payload.endTime
    if payload.location is not None:
        target["location"] = payload.location.strip()
    if payload.isCompleted is not None:
        target["isCompleted"] = payload.isCompleted
    if payload.participants is not None:
        resolved_participants: List[Dict[str, Any]] = []
        for p in payload.participants:
            p_id = p.get("_id") if isinstance(p, dict) else str(p)
            brief = await _resolve_member_brief(session, p_id, actor.church_id)
            if brief:
                resolved_participants.append(brief)
        target["participants"] = resolved_participants
        target["attendance"] = resolved_participants

    target["updatedAt"] = _iso(_now())
    meta["activities"] = activities
    group.description = _build_group_desc(clean_desc, meta)
    group.updated_at = _now()
    await session.commit()

    return {"success": True, "message": "Activity updated successfully", "activity": target, "data": target}


@router.delete("/{group_id}/activities/{activity_id}")
async def delete_group_activity(
    group_id: str,
    activity_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    clean_desc, meta = _parse_group_meta(group.description)
    activities: List[Dict[str, Any]] = meta.get("activities") if isinstance(meta.get("activities"), list) else []
    meta["activities"] = [a for a in activities if a.get("_id") != activity_id and a.get("id") != activity_id]
    group.description = _build_group_desc(clean_desc, meta)
    group.updated_at = _now()
    await session.commit()

    return {"success": True, "message": "Activity deleted successfully"}


@router.get("/{group_id}/attendance/summary")
async def get_group_attendance_summary(
    group_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    startDate: Optional[str] = Query(None),
    endDate: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=200),
    includeRecords: bool = Query(False),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    group = (
        await session.execute(
            select(Group).where(Group.id == group_id, Group.church_id == actor.church_id)
        )
    ).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    member_count = (
        await session.execute(
            select(func.count(GroupMember.id)).where(GroupMember.group_id == group.id)
        )
    ).scalar() or 0

    _, meta = _parse_group_meta(group.description)
    activities: List[Dict[str, Any]] = meta.get("activities") if isinstance(meta.get("activities"), list) else []

    summaries: List[Dict[str, Any]] = []
    for a in activities[:limit]:
        participants = a.get("participants") if isinstance(a.get("participants"), list) else []
        present = len(participants)
        expected = max(member_count, present, 1)
        absent = max(0, expected - present)
        rate = round((present / expected) * 100) if expected > 0 else 0
        summary_item: Dict[str, Any] = {
            "date": a.get("date") or a.get("createdAt") or _iso(_now()),
            "activityId": a.get("_id") or a.get("id") or "",
            "title": a.get("title") or "Group Activity",
            "type": a.get("type") or "meeting",
            "isCompleted": bool(a.get("isCompleted", True)),
            "totalExpected": expected,
            "totalPresent": present,
            "totalLate": 0,
            "totalAbsent": absent,
            "totalExcused": 0,
            "attendanceRate": rate,
        }
        if includeRecords:
            summary_item["records"] = participants
        summaries.append(summary_item)

    total_acts = len(summaries)
    total_expected = sum(s["totalExpected"] for s in summaries)
    total_present = sum(s["totalPresent"] for s in summaries)
    total_absent = sum(s["totalAbsent"] for s in summaries)
    rates = [s["attendanceRate"] for s in summaries]
    avg_rate = round(sum(rates) / len(rates)) if rates else 0
    best_rate = max(rates) if rates else 0
    worst_rate = min(rates) if rates else 0

    best_act = None
    worst_act = None
    if summaries:
        best_s = max(summaries, key=lambda x: x["attendanceRate"])
        worst_s = min(summaries, key=lambda x: x["attendanceRate"])
        best_act = {"title": best_s["title"], "date": best_s["date"], "rate": best_s["attendanceRate"]}
        worst_act = {"title": worst_s["title"], "date": worst_s["date"], "rate": worst_s["attendanceRate"]}

    return {
        "success": True,
        "data": {
            "summaries": summaries,
            "overallStats": {
                "totalActivities": len(activities),
                "activitiesWithAttendance": total_acts,
                "averageAttendanceRate": avg_rate,
                "totalExpected": total_expected,
                "totalPresent": total_present,
                "totalLate": 0,
                "totalAbsent": total_absent,
                "totalExcused": 0,
                "bestAttendanceRate": best_rate,
                "worstAttendanceRate": worst_rate,
            },
            "insights": {
                "bestAttendanceActivity": best_act,
                "worstAttendanceActivity": worst_act,
            },
            "filters": {
                "dateRange": {
                    "startDate": startDate,
                    "endDate": endDate,
                },
                "limit": limit,
                "includeRecords": includeRecords,
            },
        },
    }

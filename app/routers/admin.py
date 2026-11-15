import enum
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.orm.attributes import flag_modified
from app.core.deps import (
    Actor,
    CurrentUser,
    DbSession,
    authorize,
    church_id_from,
    require,
    resolve_actor,
)
from app.core.security import get_password_hash, verify_password
from app.core.seed import (
    ADMIN_ROLE_NAMES,
    DEFAULT_ROLE_PERMISSIONS,
    PERMISSIONS,
    ROLE_DESCRIPTIONS,
    seed_permissions,
)
from app.enums import (
    AccountStatus,
    EventStatus,
    Gender,
    MemberType,
    MembershipStatus,
    PrayerStatus,
)
from app.models.auth import MemberRoleAssignment, Permission, Role, role_permission
from app.models.church import Branch, Church
from app.models.detail import (
    Address,
    AdminDetails,
    BishopDetails,
    EmergencyContact,
    MemberDetails,
    PastorAssignment,
    PastorDetails,
    StaffDetails,
    VisitorDetails,
    VolunteerDetails,
)
from app.models.event import Attendance, Event
from app.models.finance import Fund, Offering, Pledge
from app.models.group import Department, DepartmentMember, Group, GroupMember
from app.models.member import Guardian, Household, Member
from app.models.operation import Asset, MaintenanceRecord, PrayerRequest
from app.models.user import User
from app.schemas.admin import (
    AdminBranchCreateRequest,
    AdminBranchUpdateRequest,
    AdminMemberCreateRequest,
    AdminMemberUpdateRequest,
    BranchMemberAssignRequest,
)
from app.schemas.common import AddressInput
from app.services.audit import queue_audit
from app.services.membership_service import (
    ensure_member_membership_number,
    generate_unique_membership_number,
)
from app.services.roles import (
    default_role_names,
    resolve_roles,
    role_names_for_members,
    set_member_roles,
)
from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query, Request
from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.exc import IntegrityError

router = APIRouter()


# ============================================================================
# Helpers
# ============================================================================
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _plain(v: Any) -> Any:
    return v.value if isinstance(v, enum.Enum) else v


def _pagination(total: int, page: int, size: int) -> Dict[str, Any]:
    return {
        "total": total,
        "page": page,
        "pageSize": size,
        "limit": size,
        "totalPages": (total + size - 1) // size if size > 0 else 1,
        "hasMore": (page * size) < total,
    }


def _status_enum(status: Optional[str]) -> Optional[MembershipStatus]:
    if not status or status.lower() == "all":
        return None
    try:
        return MembershipStatus[status.upper()]
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown status '{status}'")


def _search_filter(search: str):
    sf = f"%{search.strip().lower()}%"
    cols = [
        Member.email,
        User.email,
        Member.membership_number,
        Member.position,
        Member.phone_number,
    ]
    return or_(
        func.lower(Member.first_name).like(sf),
        func.lower(Member.last_name).like(sf),
        func.lower(Member.first_name + " " + Member.last_name).like(sf),
        *[func.lower(func.coalesce(c, "")).like(sf) for c in cols],
    )


async def _branch_in_church(session, branch_id: str, church_id: str) -> Branch:
    branch = (
        await session.execute(
            select(Branch).where(
                Branch.id == branch_id,
                Branch.church_id == church_id,
                Branch.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    return branch


async def _member_in_church(
    session, ident: str, church_id: str
) -> Tuple[Member, Optional[User], Optional[Branch]]:
    """Find a member by Member id or User id, ALWAYS scoped to one church."""
    row = (
        await session.execute(
            select(Member, User, Branch)
            .outerjoin(User, Member.user_id == User.id)
            .outerjoin(Branch, Member.branch_id == Branch.id)
            .where(
                Member.church_id == church_id,
                Member.deleted_at.is_(None),
                or_(Member.id == ident, Member.user_id == ident),
            )
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Member not found")
    return row[0], row[1], row[2]


async def _apply_address(session, member: Member, addr: AddressInput) -> None:
    data = addr.model_dump(exclude_unset=True)
    row = await session.get(Address, member.address_id) if member.address_id else None
    if row is None:
        row = Address()
        session.add(row)
        await session.flush()
        member.address_id = row.id
    for k, v in data.items():
        if k == "country" and v is None:
            continue
        setattr(row, k, v)


async def _upsert_detail(
    session,
    model,
    member_id: str,
    data: Dict[str, Any],
    defaults: Optional[Dict[str, Any]] = None,
):
    valid_cols = {c.key for c in model.__table__.columns}
    not_null_cols = {
        c.key for c in model.__table__.columns if not c.nullable and c.key != "id"
    }
    row = (
        await session.execute(select(model).where(model.member_id == member_id))
    ).scalar_one_or_none()
    clean_data = {
        k: _plain(v)
        for k, v in data.items()
        if k in valid_cols and not (v is None and k in not_null_cols)
    }
    clean_defaults = {
        k: _plain(v)
        for k, v in (defaults or {}).items()
        if k in valid_cols
    }
    if row is None:
        row = model(member_id=member_id, **{**clean_defaults, **clean_data})
        session.add(row)
    else:
        for k, v in clean_data.items():
            setattr(row, k, v)
    return row


async def _check_members_in_church(session, ids: List[str], church_id: str) -> None:
    ids = [i for i in set(ids) if i]
    if not ids:
        return
    found = set(
        (
            await session.execute(
                select(Member.id).where(
                    Member.id.in_(ids),
                    Member.church_id == church_id,
                    Member.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if found != set(ids):
        raise HTTPException(
            status_code=404, detail="Referenced member not found in this church"
        )


async def _replace_emergency_contacts(session, member_id: str, contacts) -> None:
    await session.execute(
        delete(EmergencyContact).where(EmergencyContact.member_id == member_id)
    )
    for c in contacts or []:
        fname = (c.full_name or "").strip()
        phone = (c.phone_number or "").strip()
        if not fname and not phone:
            continue
        session.add(
            EmergencyContact(
                member_id=member_id,
                full_name=fname or "Emergency Contact",
                phone_number=phone or "N/A",
                relationship_type=(c.relationship_type or "Relative").strip()
                or "Relative",
                email=c.email,
                address=c.address,
                notes=c.notes,
            )
        )


def _teammembers(b: Optional[Branch], user_id: str) -> List[dict]:
    if not b:
        return []
    return [
        {
            "id": str(b.id),
            "teamId": str(b.id),
            "userId": user_id,
            "team": {"id": str(b.id), "name": b.name},
        }
    ]


def format_member_row(
    m: Member, u: Optional[User], b: Optional[Branch], roles: List[str]
) -> dict:
    roles_arr = roles or ["MEMBER"]
    mem_code = m.membership_number or ""
    email = (u.email if u else m.email) or ""
    phone = (m.phone_number or (u.phone_number if u else None)) or ""
    image = (m.photo_url or (u.profile_picture_url if u else None)) or ""
    uid = str(u.id) if u else ""
    gender = m.gender.value if m.gender else None
    return {
        "id": str(m.id),
        "_id": str(m.id),
        "userId": uid or None,
        "hasLogin": u is not None,
        "membershipNumber": mem_code,
        "memberId": mem_code or str(m.id),
        "memberType": m.member_type.value,
        "firstName": m.first_name,
        "lastName": m.last_name,
        "name": m.full_name,
        "email": email,
        "phoneNumber": phone,
        "profilePictureUrl": image,
        "image": image,
        "gender": gender,
        "role": roles_arr,
        "status": m.status.value,
        "createdAt": _iso(m.created_at) or "",
        "organizationId": str(m.church_id),
        "position": m.position or "Member",
        "teamId": str(m.branch_id),
        "branchId": {"id": str(b.id), "name": b.name} if b else None,
        "user": {
            "id": uid or str(m.id),
            "_id": uid or str(m.id),
            "firstName": m.first_name,
            "lastName": m.last_name,
            "name": m.full_name,
            "email": email,
            "phoneNumber": phone,
            "image": image,
            "profilePictureUrl": image,
            "status": m.status.value,
            "gender": gender,
            "membershipNumber": mem_code,
            "memberId": mem_code or str(m.id),
            "teammembers": _teammembers(b, uid or str(m.id)),
        },
    }


# ============================================================================
# MEMBERS MANAGEMENT (/admin/members)
# ============================================================================
STAFF_AND_LEADERSHIP_ROLES = {
    "OWNER",
    "ADMIN",
    "PASTOR",
    "BISHOP",
    "STAFF",
    "TREASURER",
    "VOLUNTEER",
}


async def _compute_church_member_stats(session: DbSession, church_id: str) -> dict:
    base = [Member.church_id == church_id, Member.deleted_at.is_(None)]
    by_status = dict(
        (
            await session.execute(
                select(Member.status, func.count(Member.id))
                .where(*base)
                .group_by(Member.status)
            )
        ).all()
    )
    by_type = dict(
        (
            await session.execute(
                select(Member.member_type, func.count(Member.id))
                .where(*base)
                .group_by(Member.member_type)
            )
        ).all()
    )
    admins = (
        await session.execute(
            select(func.count(func.distinct(Member.id)))
            .join(MemberRoleAssignment, MemberRoleAssignment.member_id == Member.id)
            .join(Role, Role.id == MemberRoleAssignment.role_id)
            .where(*base, func.upper(Role.name).in_(ADMIN_ROLE_NAMES))
        )
    ).scalar() or 0
    staff_count = (
        await session.execute(
            select(func.count(func.distinct(Member.id)))
            .join(MemberRoleAssignment, MemberRoleAssignment.member_id == Member.id)
            .join(Role, Role.id == MemberRoleAssignment.role_id)
            .where(*base, func.upper(Role.name).in_(STAFF_AND_LEADERSHIP_ROLES))
        )
    ).scalar() or 0
    total = sum(by_status.values())
    active = by_status.get(MembershipStatus.ACTIVE, 0)
    suspended = (
        await session.execute(
            select(func.count(Member.id))
            .join(User, User.id == Member.user_id)
            .where(*base, User.status == AccountStatus.SUSPENDED)
        )
    ).scalar() or 0
    inactive = max(0, total - active - suspended)
    return {
        "total": total,
        "active": active,
        "inactive": inactive,
        "staff": staff_count,
        "totalMembers": total,
        "activeMembers": active,
        "inactiveMembers": inactive,
        "suspendedMembers": suspended,
        "admins": admins,
        "visitors": by_type.get(MemberType.VISITOR, 0),
        "children": by_type.get(MemberType.CHILD, 0),
    }


@router.get("/members")
@router.get("/users")
async def get_organization_members(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None, description="Church/Organization ID"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(10, ge=1, le=100),
    search: str = Query(
        "", description="Search by name, email, phone, membership number"
    ),
    role: str = Query("all"),
    status: str = Query("all"),
    memberType: Optional[str] = Query(None, description="MEMBER, VISITOR or CHILD"),
    sortOrder: str = Query("desc"),
):
    """Paginated members of one church."""
    actor = await authorize(
        session, current_user, church_id_from(request, organizationId), "member.read"
    )
    conds = [Member.church_id == actor.church_id, Member.deleted_at.is_(None)]
    if search:
        conds.append(_search_filter(search))
    st = _status_enum(status)
    if st:
        conds.append(Member.status == st)
    if memberType:
        try:
            conds.append(Member.member_type == MemberType[memberType.upper()])
        except KeyError:
            raise HTTPException(
                status_code=400, detail=f"Unknown memberType '{memberType}'"
            )
    if role and role.lower() != "all":
        conds.append(
            Member.id.in_(
                select(MemberRoleAssignment.member_id)
                .join(Role, Role.id == MemberRoleAssignment.role_id)
                .where(func.upper(Role.name) == role.upper())
            )
        )
    total = (
        await session.execute(
            select(func.count(Member.id))
            .select_from(Member)
            .outerjoin(User, Member.user_id == User.id)
            .where(*conds)
        )
    ).scalar() or 0
    order = (
        Member.created_at.asc()
        if sortOrder.lower() == "asc"
        else Member.created_at.desc()
    )
    rows = (
        await session.execute(
            select(Member, User, Branch)
            .outerjoin(User, Member.user_id == User.id)
            .outerjoin(Branch, Member.branch_id == Branch.id)
            .where(*conds)
            .order_by(order, Member.id)
            .offset((page - 1) * pageSize)
            .limit(pageSize)
        )
    ).all()
    roles_by_member = await role_names_for_members(session, [m.id for m, _, _ in rows])
    items = [
        format_member_row(m, u, b, roles_by_member.get(m.id, [])) for m, u, b in rows
    ]
    statistics = await _compute_church_member_stats(session, actor.church_id)
    return {
        "members": items,
        "users": items,
        "statistics": statistics,
        "pagination": _pagination(total, page, pageSize),
    }


@router.get("/members/statistics")
async def get_member_statistics(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await authorize(
        session, current_user, church_id_from(request, organizationId), "member.read"
    )
    return await _compute_church_member_stats(session, actor.church_id)


@router.get("/members/{user_id}")
@router.get("/users/{user_id}")
async def get_member_details(
    user_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    """One member, by Member id or User id, within the caller's church."""
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    member, user, branch = await _member_in_church(session, user_id, actor.church_id)
    await require(session, actor, "member.read", member.branch_id)
    mem_code = member.membership_number or ""
    if not mem_code:  # backfill numbers for older members
        try:
            mem_code = await ensure_member_membership_number(session, member)
        except Exception:
            mem_code = ""
    roles = (await role_names_for_members(session, [member.id])).get(member.id, [])
    church = await session.get(Church, actor.church_id)
    profile_meta = dict(
        ((church.metadata_json if church else None) or {})
        .get("member_profiles", {})
        .get(str(member.id), {})
    )

    def one(model):
        return select(model).where(model.member_id == member.id)

    md = (await session.execute(one(MemberDetails))).scalar_one_or_none()
    vol = (await session.execute(one(VolunteerDetails))).scalar_one_or_none()
    staff = (await session.execute(one(StaffDetails))).scalar_one_or_none()
    pastor = (await session.execute(one(PastorDetails))).scalar_one_or_none()
    bishop = (await session.execute(one(BishopDetails))).scalar_one_or_none()
    admin_d = (await session.execute(one(AdminDetails))).scalar_one_or_none()
    visitor = (await session.execute(one(VisitorDetails))).scalar_one_or_none()
    contacts = (
        (
            await session.execute(
                one(EmergencyContact).order_by(EmergencyContact.created_at)
            )
        )
        .scalars()
        .all()
    )
    addr = await session.get(Address, member.address_id) if member.address_id else None
    guardians = (
        await session.execute(
            select(Guardian, Member)
            .join(Member, Member.id == Guardian.guardian_member_id)
            .where(Guardian.child_member_id == member.id)
        )
    ).all()

    # Fetch member's departments & small groups
    dept_rows = (
        await session.execute(
            select(Department, DepartmentMember)
            .join(DepartmentMember, DepartmentMember.department_id == Department.id)
            .where(
                DepartmentMember.member_id == member.id,
                Department.church_id == actor.church_id,
            )
            .order_by(Department.name)
        )
    ).all()
    dept_objs = [
        {
            "_id": str(d.id),
            "id": str(d.id),
            "departmentName": d.name,
            "name": d.name,
            "joinedAt": _iso(dm.created_at),
        }
        for d, dm in dept_rows
    ]

    group_rows = (
        await session.execute(
            select(Group, GroupMember)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .where(
                GroupMember.member_id == member.id,
                Group.church_id == actor.church_id,
            )
            .order_by(Group.name)
        )
    ).all()
    group_objs = [
        {
            "_id": str(g.id),
            "id": str(g.id),
            "groupName": g.name,
            "name": g.name,
            "joinedAt": _iso(gm.joined_at),
        }
        for g, gm in group_rows
    ]

    # Pastor assigned branches
    assigned_pastor_branches = []
    if pastor:
        pa_rows = (
            await session.execute(
                select(Branch)
                .join(PastorAssignment, PastorAssignment.branch_id == Branch.id)
                .where(
                    PastorAssignment.pastor_details_id == pastor.id,
                    PastorAssignment.is_active.is_(True),
                )
            )
        ).scalars().all()
        assigned_pastor_branches = [
            {
                "_id": str(b.id),
                "id": str(b.id),
                "branchName": b.name,
                "name": b.name,
            }
            for b in pa_rows
        ]
    if not assigned_pastor_branches and branch:
        assigned_pastor_branches = [
            {
                "_id": str(branch.id),
                "id": str(branch.id),
                "branchName": branch.name,
                "name": branch.name,
            }
        ]

    # Build 6-month window for Giving & Attendance analytics
    now_dt = _now()
    month_keys: List[Tuple[int, int, str]] = []
    for offset in range(5, -1, -1):
        m_num = now_dt.month - offset
        y_num = now_dt.year
        while m_num <= 0:
            m_num += 12
            y_num -= 1
        label = datetime(y_num, m_num, 1).strftime("%b")
        month_keys.append((y_num, m_num, label))

    # Member Offerings
    offering_rows = (
        await session.execute(
            select(Offering, Fund)
            .outerjoin(Fund, Fund.id == Offering.fund_id)
            .where(
                Offering.church_id == actor.church_id,
                Offering.member_id == member.id,
                Offering.deleted_at.is_(None),
            )
            .order_by(Offering.received_at.desc())
        )
    ).all()
    total_giving = round(
        sum(float(o.amount or 0) for o, _ in offering_rows), 2
    )
    giving_by_ym: Dict[Tuple[int, int], float] = {
        (y, m): 0.0 for y, m, _ in month_keys
    }
    for o, _ in offering_rows:
        dt = o.received_at or o.created_at
        if dt:
            ym = (dt.year, dt.month)
            if ym in giving_by_ym:
                giving_by_ym[ym] = round(giving_by_ym[ym] + float(o.amount or 0), 2)
    giving_data = [
        {"month": label, "amount": round(giving_by_ym[(y, m)], 2)}
        for y, m, label in month_keys
    ]
    six_month_giving_total = sum(d["amount"] for d in giving_data)
    active_giving_months = sum(1 for d in giving_data if d["amount"] > 0)
    avg_monthly_giving = (
        round(six_month_giving_total / 6, 2)
        if six_month_giving_total > 0
        else round(total_giving / max(1, len(offering_rows)), 2)
    )
    giving_consistency_pct = (
        round((active_giving_months / 6) * 100)
        if six_month_giving_total > 0
        else (100 if total_giving > 0 else 0)
    )
    prev_3m = sum(d["amount"] for d in giving_data[:3])
    recent_3m = sum(d["amount"] for d in giving_data[3:])
    if prev_3m > 0:
        giving_growth_pct = round(((recent_3m - prev_3m) / prev_3m) * 100, 1)
    elif recent_3m > 0:
        giving_growth_pct = 100.0
    else:
        giving_growth_pct = 0.0

    # Member Attendance
    att_rows = (
        await session.execute(
            select(Attendance, Event)
            .join(Event, Event.id == Attendance.event_id)
            .where(
                Event.church_id == actor.church_id,
                Attendance.member_id == member.id,
            )
            .order_by(Attendance.checked_in_at.desc())
        )
    ).all()
    church_events = (
        await session.execute(
            select(Event).where(
                Event.church_id == actor.church_id,
            )
        )
    ).scalars().all()
    events_by_ym: Dict[Tuple[int, int], int] = {
        (y, m): 0 for y, m, _ in month_keys
    }
    for ev in church_events:
        dt = ev.start_time or ev.created_at
        if dt and (dt.year, dt.month) in events_by_ym:
            events_by_ym[(dt.year, dt.month)] += 1

    att_by_ym: Dict[Tuple[int, int], int] = {
        (y, m): 0 for y, m, _ in month_keys
    }
    svc_type_counts: Dict[str, int] = {}
    attended_weeks: set[Tuple[int, int]] = set()
    for att, ev in att_rows:
        dt = att.checked_in_at or ev.start_time or att.created_at
        if dt:
            ym = (dt.year, dt.month)
            if ym in att_by_ym:
                att_by_ym[ym] += 1
            iso_cal = dt.isocalendar()
            attended_weeks.add((iso_cal[0], iso_cal[1]))
        svc_title = (ev.name or "Sunday Service").strip()
        svc_type_counts[svc_title] = svc_type_counts.get(svc_title, 0) + 1

    attendance_data = []
    best_month_label = "N/A"
    best_month_pct = -1
    for y, m, label in month_keys:
        att_cnt = att_by_ym[(y, m)]
        svc_cnt = max(att_cnt, events_by_ym[(y, m)], 4 if att_cnt > 0 else 0)
        pct = round((att_cnt / svc_cnt) * 100) if svc_cnt > 0 else 0
        attendance_data.append(
            {
                "month": label,
                "attendance": pct,
                "attended": att_cnt,
                "services": svc_cnt,
            }
        )
        if att_cnt > 0 and pct > best_month_pct:
            best_month_pct = pct
            best_month_label = f"{label} ({pct}%)"

    services_attended = len(att_rows)
    total_services = max(services_attended, len(church_events))
    avg_attendance = (
        round((services_attended / total_services) * 100)
        if total_services > 0
        else 0
    )
    # Consecutive weeks streak from most recent attended week
    sorted_weeks = sorted(attended_weeks, reverse=True)
    consecutive_weeks = 0
    if sorted_weeks:
        consecutive_weeks = 1
        prev_y, prev_w = sorted_weeks[0]
        for cur_y, cur_w in sorted_weeks[1:]:
            if (prev_y == cur_y and prev_w - cur_w == 1) or (
                prev_y - cur_y == 1 and prev_w == 1 and cur_w >= 51
            ):
                consecutive_weeks += 1
                prev_y, prev_w = cur_y, cur_w
            else:
                break

    service_preferences = [
        {
            "name": name,
            "count": cnt,
            "percentage": round((cnt / max(1, services_attended)) * 100),
        }
        for name, cnt in sorted(
            svc_type_counts.items(), key=lambda x: x[1], reverse=True
        )[:4]
    ]

    # Involvement breakdown (Departments & Small Groups + Volunteer Hours)
    vol_hours = int(vol.hours_contributed if vol else 0)
    palette = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ec4899", "#06b6d4"]
    involvement_items = [d["departmentName"] for d in dept_objs] + [
        g["groupName"] for g in group_objs
    ]
    involvement_data = []
    if involvement_items:
        base_hours = (
            max(1, round(vol_hours / len(involvement_items)))
            if vol_hours > 0
            else 4
        )
        for idx, item_name in enumerate(involvement_items):
            involvement_data.append(
                {
                    "name": item_name,
                    "hours": base_hours,
                    "color": palette[idx % len(palette)],
                }
            )
    elif vol_hours > 0:
        involvement_data.append(
            {"name": "General Ministry", "hours": vol_hours, "color": palette[0]}
        )

    # Dynamic Recognitions & Leadership Roles
    recognitions = [
        {
            "title": member.position or "Church Member",
            "subtitle": f"Role: {', '.join(roles or ['MEMBER'])}",
            "category": "role",
        }
    ]
    for d in dept_objs:
        recognitions.append(
            {
                "title": d["departmentName"],
                "subtitle": "Active Department Member",
                "category": "department",
            }
        )
    for g in group_objs:
        recognitions.append(
            {
                "title": g["groupName"],
                "subtitle": "Small Group Member",
                "category": "group",
            }
        )
    if vol_hours > 0:
        recognitions.append(
            {
                "title": "Faithful Volunteer",
                "subtitle": f"{vol_hours} Volunteer Hours Contributed",
                "category": "award",
            }
        )

    # Combined chronological Recent Activities
    row = format_member_row(member, user, branch, roles)
    created = _iso(member.created_at) or ""
    first = contacts[0] if contacts else None
    mem_date = (
        _iso(md.membership_date)
        if md and md.membership_date
        else (_iso(member.joined_at) or created)
    )
    activity_events: List[Dict[str, Any]] = []
    for o, f in offering_rows[:8]:
        dt_iso = _iso(o.received_at or o.created_at) or created
        fund_label = f.name if f else "General Offering"
        activity_events.append(
            {
                "date": dt_iso[:10],
                "timestamp": dt_iso,
                "activity": f"Contributed KES {float(o.amount or 0):,.0f} ({fund_label})",
                "type": "Giving",
            }
        )
    for att, ev in att_rows[:8]:
        dt_iso = _iso(att.checked_in_at or ev.start_time or att.created_at) or created
        activity_events.append(
            {
                "date": dt_iso[:10],
                "timestamp": dt_iso,
                "activity": f"Attended {ev.name}",
                "type": "Attendance",
            }
        )
    for d, dm in dept_rows:
        dt_iso = _iso(dm.created_at) or created
        activity_events.append(
            {
                "date": dt_iso[:10],
                "timestamp": dt_iso,
                "activity": f"Member of {d.name} Department",
                "type": "Ministry",
            }
        )
    for g, gm in group_rows:
        dt_iso = _iso(gm.joined_at) or created
        activity_events.append(
            {
                "date": dt_iso[:10],
                "timestamp": dt_iso,
                "activity": f"Joined {g.name} Small Group",
                "type": "Service",
            }
        )
    activity_events.append(
        {
            "date": (mem_date or created)[:10],
            "timestamp": mem_date or created,
            "activity": f"Joined {branch.name if branch else 'Church'} as {member.position or 'Member'}",
            "type": "Ministry",
        }
    )
    activity_events.sort(key=lambda x: x.get("timestamp") or "", reverse=True)

    saved_skills = profile_meta.get("skills") or []
    saved_notes = profile_meta.get("notes") or ""
    marital_val = md.marital_status.value if md and md.marital_status else None
    occupation_val = (md.occupation if md else None) or ""

    return {
        "id": str(member.id),
        "_id": str(member.id),
        "membershipNumber": mem_code,
        "memberId": mem_code or str(member.id),
        "userId": str(user.id) if user else None,
        "hasLogin": user is not None,
        "memberType": member.member_type.value,
        "role": roles or ["MEMBER"],
        "roles": roles or ["MEMBER"],
        "position": member.position or "Member",
        "status": member.status.value,
        "membershipStatus": member.status.value,
        "membershipDate": mem_date,
        "joinedAt": _iso(member.joined_at) or created,
        "maritalStatus": marital_val,
        "occupation": occupation_val,
        "skills": saved_skills,
        "notes": saved_notes,
        "householdId": member.household_id,
        "householdRole": (
            member.household_role.value if member.household_role else None
        ),
        "organizationId": str(member.church_id),
        "createdAt": created,
        # Live analytics fields
        "totalGiving": total_giving,
        "averageMonthlyGiving": avg_monthly_giving,
        "givingConsistencyPct": giving_consistency_pct,
        "givingMonthsActive": active_giving_months,
        "givingGrowthPct": giving_growth_pct,
        "givingData": giving_data,
        "averageAttendance": avg_attendance,
        "servicesAttended": services_attended,
        "totalServices": total_services,
        "consecutiveWeeks": consecutive_weeks,
        "bestMonth": best_month_label,
        "attendanceData": attendance_data,
        "servicePreferences": service_preferences,
        "involvementData": involvement_data,
        "recognitions": recognitions,
        "recentActivities": activity_events[:15],
        "user": {
            **row["user"],
            "dateOfBirth": (
                member.date_of_birth.isoformat() if member.date_of_birth else None
            ),
            "maritalStatus": marital_val,
            "occupation": occupation_val,
            "skills": saved_skills,
            "notes": saved_notes,
        },
        "member": {
            "id": str(member.id),
            "_id": str(member.id),
            "membershipNumber": mem_code,
            "memberId": mem_code or str(member.id),
            "role": roles or ["MEMBER"],
            "roles": roles or ["MEMBER"],
            "memberType": member.member_type.value,
            "position": member.position or "Member",
            "churchId": str(member.church_id),
            "organizationId": str(member.church_id),
            "branchId": str(member.branch_id),
            "teamId": str(member.branch_id),
            "householdId": member.household_id,
            "householdRole": (
                member.household_role.value if member.household_role else None
            ),
            "status": member.status.value,
            "membershipStatus": member.status.value,
            "createdAt": created,
            "joinedDate": member.joined_at.isoformat() if member.joined_at else created,
            "membershipDate": mem_date,
        },
        "address": {
            "street": (addr.street if addr else "") or "",
            "city": (addr.city if addr else "") or "",
            "state": (addr.state if addr else "") or "",
            "zipCode": (addr.zip_code if addr else "") or "",
            "country": (addr.country if addr else "Kenya") or "Kenya",
        },
        # first contact in the old flat shape + the full list
        "emergencyContact": {
            "emergencyContactFullName": first.full_name if first else "",
            "emergencyContactEmail": (first.email if first else "") or "",
            "emergencyContactPhoneNumber": first.phone_number if first else "",
            "emergencyContactRelationship": first.relationship_type if first else "",
            "emergencyContactAddress": (first.address if first else "") or "",
            "emergencyContactNotes": (first.notes if first else "") or "",
        },
        "emergencyContacts": [
            {
                "id": c.id,
                "fullName": c.full_name,
                "email": c.email,
                "phoneNumber": c.phone_number,
                "relationshipType": c.relationship_type,
                "address": c.address,
                "notes": c.notes,
            }
            for c in contacts
        ],
        "guardians": [
            {
                "memberId": g.guardian_member_id,
                "name": gm.full_name,
                "relationshipType": g.relationship_type,
                "isPrimary": g.is_primary,
                "canPickup": g.can_pickup,
            }
            for g, gm in guardians
        ],
        "memberDetails": {
            "memberId": mem_code or str(member.id),
            "joinedDate": mem_date,
            "membershipDate": mem_date,
            "membershipStatus": member.status.value,
            "baptismDate": _iso(md.baptism_date) if md else None,
            "maritalStatus": marital_val,
            "occupation": occupation_val,
            "departments": dept_objs,
            "groups": group_objs,
            "departmentIds": dept_objs,
            "groupIds": group_objs,
        },
        "volunteerDetails": {
            "volunteerId": str(vol.id) if vol else (mem_code or str(member.id)),
            "hoursContributed": vol.hours_contributed if vol else 0,
            "volunteerStatus": profile_meta.get("volunteerStatus") or "ACTIVE",
            "availabilitySchedule": profile_meta.get("availabilitySchedule")
            or {
                "days": [],
                "timeSlots": [],
                "preferredTime": "",
            },
            "departments": dept_objs,
            "roles": roles or ["MEMBER"],
        },
        "staffDetails": {
            "staffId": str(staff.id) if staff else (mem_code or str(member.id)),
            "department": (staff.department if staff else "") or "General",
            "jobTitle": (staff.job_title if staff else "")
            or (member.position or "Staff"),
            "employmentType": profile_meta.get("employmentType") or "FULL_TIME",
            "salary": float(profile_meta.get("salary") or 0),
            "isActive": bool(profile_meta.get("isActive", True)),
            "startDate": _iso(staff.start_date) if staff else created,
            "endDate": _iso(staff.end_date) if staff else None,
        },
        "pastorDetails": {
            "pastorId": mem_code or str(member.id),
            "ordinationDate": _iso(pastor.ordination_date) if pastor else None,
            "sermonCount": pastor.sermon_count if pastor else 0,
            "counselingSessions": int(profile_meta.get("counselingSessions") or 0),
            "qualifications": profile_meta.get("qualifications") or [],
            "specializations": profile_meta.get("specializations") or [],
            "assignedBranches": assigned_pastor_branches,
            "biography": (pastor.biography if pastor else "") or "",
        },
        "bishopDetails": {
            "bishopId": mem_code or str(member.id),
            "appointmentDate": _iso(bishop.appointment_date) if bishop else None,
            "jurisdictionArea": (bishop.jurisdiction_area if bishop else "") or "",
            "oversightBranches": assigned_pastor_branches,
            "achievements": profile_meta.get("achievements") or [],
            "biography": (bishop.biography if bishop else "") or "",
        },
        "adminDetails": {
            "adminId": mem_code or str(member.id),
            "accessLevel": (admin_d.access_level if admin_d else "BRANCH"),
            "assignedBranches": assigned_pastor_branches,
            "permissions": profile_meta.get("adminPermissions") or [],
        },
        "visitorDetails": {
            "visitDate": _iso(visitor.visit_date) if visitor else None,
            "howDidYouHear": visitor.how_did_you_hear if visitor else None,
            "followUpStatus": visitor.follow_up_status if visitor else None,
            "followUpNotes": (visitor.follow_up_notes if visitor else "") or "",
            "interestedInMembership": (
                visitor.interested_in_membership if visitor else False
            ),
            "invitedByMemberId": visitor.invited_by_member_id if visitor else None,
            "convertedAt": _iso(visitor.converted_at) if visitor else None,
        },
    }


@router.post("/members")
@router.post("/users")
async def add_organization_member(
    payload: AdminMemberCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    """
    Add a person to a church. A login is created only when createLogin is true
    (visitors and children normally have none).
    """
    actor = await resolve_actor(
        session, current_user, church_id_from(request, payload.organizationId)
    )
    church_id = actor.church_id
    branch = await _branch_in_church(session, payload.branchId, church_id)
    await require(session, actor, "member.create", branch.id)
    if payload.roles:
        await require(
            session, actor, "role.assign"
        )  # stops a member-creator from granting ADMIN
    roles = await resolve_roles(
        session, church_id, payload.roles or default_role_names(payload.memberType)
    )
    role_names = [r.name for r in roles]
    if payload.householdId:
        if not (
            await session.execute(
                select(Household.id).where(
                    Household.id == payload.householdId,
                    Household.church_id == church_id,
                )
            )
        ).first():
            raise HTTPException(status_code=404, detail="Household not found")
    await _check_members_in_church(
        session, [g.guardian_member_id for g in payload.guardians or []], church_id
    )
    if payload.visitorDetails and payload.visitorDetails.invited_by_member_id:
        await _check_members_in_church(
            session, [payload.visitorDetails.invited_by_member_id], church_id
        )
    # ---- optional login ----
    user: Optional[User] = None
    if payload.createLogin:
        if not payload.password:
            # Invitation e-mails are not implemented yet, so a password must be supplied.
            raise HTTPException(
                status_code=400, detail="password is required when createLogin is true"
            )
        email = payload.email.strip().lower()
        user = (
            await session.execute(select(User).where(func.lower(User.email) == email))
        ).scalar_one_or_none()
        if user:
            prior = (
                (
                    await session.execute(
                        select(Member).where(
                            Member.church_id == church_id, Member.user_id == user.id
                        )
                    )
                )
                .scalars()
                .first()
            )
            if prior:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This person was removed from the church earlier"
                        if prior.deleted_at
                        else "This user is already a member of this church"
                    ),
                )
            # An existing account keeps its own password; the supplied one is ignored.
        else:
            if (
                payload.phoneNumber
                and (
                    await session.execute(
                        select(User.id).where(User.phone_number == payload.phoneNumber)
                    )
                ).first()
            ):
                raise HTTPException(
                    status_code=409,
                    detail="A user with this phone number already exists",
                )
            user = User(
                email=email,
                display_name=f"{payload.firstName} {payload.lastName}".strip(),
                phone_number=payload.phoneNumber,
                hashed_password=get_password_hash(payload.password),
                status=AccountStatus.ACTIVE,
            )
            session.add(user)
            await session.flush()
    is_admin = any(n.upper() in ADMIN_ROLE_NAMES for n in role_names)
    position = payload.position
    if is_admin and (not position or position.lower() == "member"):
        position = "Church Administrator"
    number = await generate_unique_membership_number(
        session=session,
        church_id=church_id,
        roles=role_names,
        position=position,
        member_type=payload.memberType,
    )
    member = Member(
        church_id=church_id,
        branch_id=branch.id,
        user_id=user.id if user else None,
        household_id=payload.householdId,
        member_type=payload.memberType,
        first_name=payload.firstName.strip(),
        last_name=payload.lastName.strip(),
        date_of_birth=payload.dateOfBirth,
        gender=payload.gender,
        phone_number=payload.phoneNumber,
        email=payload.email.strip().lower() if payload.email else None,
        position=position,
        household_role=payload.householdRole,
        membership_number=number,
        joined_at=payload.joinedAt,
    )
    session.add(member)
    await session.flush()
    await set_member_roles(session, member.id, roles, replace=False)
    if payload.address:
        await _apply_address(session, member, payload.address)

    if payload.memberType == MemberType.MEMBER:
        await _upsert_detail(
            session, MemberDetails, member.id, {}, {"membership_date": _now()}
        )
    if is_admin:
        await _upsert_detail(
            session, AdminDetails, member.id, {}, {"access_level": "NATIONAL"}
        )
    if any("STAFF" in n.upper() for n in role_names):
        await _upsert_detail(
            session,
            StaffDetails,
            member.id,
            {},
            {
                "job_title": position or "Staff",
                "department": "General",
                "start_date": _now(),
            },
        )
    if any("VOLUNTEER" in n.upper() for n in role_names):
        await _upsert_detail(session, VolunteerDetails, member.id, {})
    if payload.memberType == MemberType.VISITOR:
        data = (
            payload.visitorDetails.model_dump(exclude_unset=True)
            if payload.visitorDetails
            else {}
        )
        data.setdefault("visit_date", _now())
        await _upsert_detail(session, VisitorDetails, member.id, data)
    await _replace_emergency_contacts(session, member.id, payload.emergencyContacts)
    for g in payload.guardians or []:
        session.add(
            Guardian(
                child_member_id=member.id,
                guardian_member_id=g.guardian_member_id,
                relationship_type=g.relationship_type,
                is_primary=g.is_primary,
                can_pickup=g.can_pickup,
            )
        )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Conflict: duplicate email, phone or membership number",
        )
    queue_audit(
        background_tasks,
        request,
        "ADD_MEMBER",
        "MEMBER",
        member.id,
        actor_user_id=current_user.id,
        church_id=church_id,
        metadata_json={
            "memberType": member.member_type.value,
            "hasLogin": user is not None,
            "roles": role_names,
        },
    )
    return {
        "success": True,
        "message": "Member added successfully",
        "id": str(member.id),
        "membershipNumber": number,
        "memberId": number,
        "memberType": member.member_type.value,
        "role": role_names,
        "createdAt": _iso(member.created_at) or "",
        "organizationId": str(church_id),
        "branchId": str(branch.id),
        "position": member.position,
        "user": {
            "id": str(user.id) if user else str(member.id),
            "name": member.full_name,
            "email": (user.email if user else member.email) or "",
            "phoneNumber": member.phone_number or "",
            "status": member.status.value,
            "hasLogin": user is not None,
            "membershipNumber": number,
            "memberId": number,
        },
    }


@router.put("/members/{user_id}")
@router.put("/users/{user_id}")
async def update_organization_member(
    user_id: str,
    payload: AdminMemberUpdateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    """
    Update a member's profile within ONE church. Login data (the account's email,
    phone, password, picture) is deliberately NOT changed here: a login can belong to
    several churches, so one church's admin must not be able to alter it.
    """
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    member, user, _ = await _member_in_church(session, user_id, actor.church_id)
    await require(session, actor, "member.update", member.branch_id)
    fields = payload.model_fields_set
    changed: List[str] = []
    simple = {
        "firstName": ("first_name", lambda v: v.strip()),
        "lastName": ("last_name", lambda v: v.strip()),
        "dateOfBirth": ("date_of_birth", None),
        "gender": ("gender", None),
        "phoneNumber": ("phone_number", None),
        "position": ("position", None),
        "memberType": ("member_type", None),
        "householdRole": ("household_role", None),
        "membershipNumber": ("membership_number", None),
        "joinedAt": ("joined_at", None),
        "status": ("status", None),
        "profilePictureUrl": ("photo_url", None),
    }
    for key, (attr, fn) in simple.items():
        if key in fields:
            v = getattr(payload, key)
            if v is None and key in ("firstName", "lastName", "memberType", "status"):
                continue  # NOT NULL columns
            setattr(member, attr, fn(v) if (fn and v is not None) else v)
            changed.append(attr)
    if (
        "status" not in fields
        and payload.memberDetails
        and payload.memberDetails.membership_status is not None
    ):
        member.status = payload.memberDetails.membership_status
        changed.append("status")
    if "email" in fields:
        member.email = payload.email.strip().lower() if payload.email else None
        changed.append("email")
    if "householdId" in fields:
        if (
            payload.householdId
            and not (
                await session.execute(
                    select(Household.id).where(
                        Household.id == payload.householdId,
                        Household.church_id == actor.church_id,
                    )
                )
            ).first()
        ):
            raise HTTPException(status_code=404, detail="Household not found")
        member.household_id = payload.householdId
        changed.append("household_id")
    if payload.branchId and payload.branchId != member.branch_id:
        new_branch = await _branch_in_church(session, payload.branchId, actor.church_id)
        await require(session, actor, "member.update", new_branch.id)
        member.branch_id = new_branch.id
        changed.append("branch_id")
    if payload.roles is not None:
        await require(session, actor, "role.assign")
        roles = await resolve_roles(
            session,
            actor.church_id,
            payload.roles or default_role_names(member.member_type),
        )
        if member.id == (actor.member.id if actor.member else None):
            current_roles = (
                await role_names_for_members(session, [member.id])
            ).get(member.id, [])
            had_admin = any(r.upper() in ADMIN_ROLE_NAMES for r in current_roles)
            will_have_admin = any(
                r.name.upper() in ADMIN_ROLE_NAMES for r in roles
            )
            if had_admin and not will_have_admin:
                raise HTTPException(
                    status_code=400,
                    detail="You cannot remove your own administrator role",
                )
        await set_member_roles(session, member.id, roles, replace=True)
        changed.append("roles")
    if payload.address:
        await _apply_address(session, member, payload.address)
        changed.append("address")
    if payload.emergencyContacts is not None:
        await _replace_emergency_contacts(session, member.id, payload.emergencyContacts)
        changed.append("emergencyContacts")

    # Sync top-level maritalStatus and occupation into MemberDetails
    md_extra: Dict[str, Any] = {}
    if "maritalStatus" in fields and payload.maritalStatus is not None:
        md_extra["marital_status"] = payload.maritalStatus
    if "occupation" in fields:
        md_extra["occupation"] = payload.occupation
    if md_extra and payload.memberDetails is None:
        await _upsert_detail(session, MemberDetails, member.id, md_extra, {})
        changed.append("memberDetails")

    # Sync DepartmentMember and GroupMember if departmentIds / groupIds provided
    if payload.memberDetails is not None:
        if payload.memberDetails.department_ids is not None:
            want_dept_ids = {
                str(did).strip()
                for did in payload.memberDetails.department_ids
                if did and str(did).strip()
            }
            valid_dept_ids = set(
                (
                    await session.execute(
                        select(Department.id).where(
                            Department.church_id == actor.church_id,
                            Department.id.in_(want_dept_ids),
                        )
                    )
                )
                .scalars()
                .all()
            ) if want_dept_ids else set()
            existing_dms = (
                (
                    await session.execute(
                        select(DepartmentMember)
                        .join(Department, Department.id == DepartmentMember.department_id)
                        .where(
                            DepartmentMember.member_id == member.id,
                            Department.church_id == actor.church_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            existing_dept_ids = {str(dm.department_id) for dm in existing_dms}
            for dm in existing_dms:
                if str(dm.department_id) not in valid_dept_ids:
                    await session.delete(dm)
            for did in valid_dept_ids - existing_dept_ids:
                session.add(
                    DepartmentMember(department_id=did, member_id=member.id)
                )
            changed.append("departments")

        if payload.memberDetails.group_ids is not None:
            want_group_ids = {
                str(gid).strip()
                for gid in payload.memberDetails.group_ids
                if gid and str(gid).strip()
            }
            valid_group_ids = set(
                (
                    await session.execute(
                        select(Group.id).where(
                            Group.church_id == actor.church_id,
                            Group.id.in_(want_group_ids),
                        )
                    )
                )
                .scalars()
                .all()
            ) if want_group_ids else set()
            existing_gms = (
                (
                    await session.execute(
                        select(GroupMember)
                        .join(Group, Group.id == GroupMember.group_id)
                        .where(
                            GroupMember.member_id == member.id,
                            Group.church_id == actor.church_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            existing_group_ids = {str(gm.group_id) for gm in existing_gms}
            for gm in existing_gms:
                if str(gm.group_id) not in valid_group_ids:
                    await session.delete(gm)
            for gid in valid_group_ids - existing_group_ids:
                session.add(GroupMember(group_id=gid, member_id=member.id))
            changed.append("groups")

    detail_models = [
        ("adminDetails", AdminDetails, {"access_level": "BRANCH"}),
        (
            "staffDetails",
            StaffDetails,
            {
                "job_title": member.position or "Staff",
                "department": "General",
                "start_date": _now(),
            },
        ),
        ("volunteerDetails", VolunteerDetails, {}),
        ("memberDetails", MemberDetails, {}),
        ("visitorDetails", VisitorDetails, {"visit_date": _now()}),
        ("pastorDetails", PastorDetails, {}),
        ("bishopDetails", BishopDetails, {}),
    ]
    for key, model, defaults in detail_models:
        d = getattr(payload, key)
        if d is not None:
            data = d.model_dump(exclude_unset=True)
            if key == "memberDetails" and md_extra:
                data.update(md_extra)
            if key == "staffDetails":
                has_staff_info = bool(
                    (data.get("job_title") or "").strip()
                    or (data.get("department") or "").strip()
                )
                existing_staff = (
                    await session.execute(
                        select(StaffDetails.id).where(
                            StaffDetails.member_id == member.id
                        )
                    )
                ).first()
                if not has_staff_info and not existing_staff:
                    continue
                if not (data.get("job_title") or "").strip():
                    data["job_title"] = member.position or "Staff"
                if not (data.get("department") or "").strip():
                    data["department"] = "General"
                if not data.get("start_date"):
                    data["start_date"] = _now()
            if data.get("invited_by_member_id"):
                await _check_members_in_church(
                    session, [data["invited_by_member_id"]], actor.church_id
                )
            await _upsert_detail(session, model, member.id, data, defaults)
            changed.append(key)

    # Persist skills, notes, and extended profile metadata on Church.metadata_json
    church = await session.get(Church, actor.church_id)
    if church:
        meta = dict(church.metadata_json or {})
        profiles = dict(meta.get("member_profiles") or {})
        p_meta = dict(profiles.get(str(member.id)) or {})
        if "skills" in fields and payload.skills is not None:
            p_meta["skills"] = [
                str(s).strip() for s in payload.skills if s and str(s).strip()
            ]
        if "notes" in fields and payload.notes is not None:
            p_meta["notes"] = payload.notes
        if payload.volunteerDetails:
            if payload.volunteerDetails.volunteer_status:
                p_meta["volunteerStatus"] = payload.volunteerDetails.volunteer_status
            if payload.volunteerDetails.availability_schedule is not None:
                p_meta["availabilitySchedule"] = (
                    payload.volunteerDetails.availability_schedule
                )
        if payload.staffDetails:
            if payload.staffDetails.employment_type:
                p_meta["employmentType"] = payload.staffDetails.employment_type
            if payload.staffDetails.salary is not None:
                p_meta["salary"] = float(payload.staffDetails.salary)
            if payload.staffDetails.is_active is not None:
                p_meta["isActive"] = bool(payload.staffDetails.is_active)
        if payload.pastorDetails:
            if payload.pastorDetails.counseling_sessions is not None:
                p_meta["counselingSessions"] = int(
                    payload.pastorDetails.counseling_sessions
                )
            if payload.pastorDetails.qualifications is not None:
                p_meta["qualifications"] = payload.pastorDetails.qualifications
            if payload.pastorDetails.specializations is not None:
                p_meta["specializations"] = payload.pastorDetails.specializations
        if payload.bishopDetails and payload.bishopDetails.achievements is not None:
            p_meta["achievements"] = payload.bishopDetails.achievements
        if payload.adminDetails and payload.adminDetails.permissions is not None:
            p_meta["adminPermissions"] = payload.adminDetails.permissions
        profiles[str(member.id)] = p_meta
        meta["member_profiles"] = profiles
        church.metadata_json = meta
        flag_modified(church, "metadata_json")

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Conflict: duplicate membership number"
        )
    queue_audit(
        background_tasks,
        request,
        "UPDATE_MEMBER",
        "MEMBER",
        member.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
        metadata_json={"fields": changed},  # field names only, never values (PII)
    )
    return {
        "success": True,
        "message": "Member updated successfully",
        "user": {
            "id": str(user.id) if user else str(member.id),
            "userId": str(user.id) if user else None,
            "name": member.full_name,
            "email": (user.email if user else member.email) or "",
        },
    }


@router.patch("/members/{user_id}/role")
@router.patch("/users/{user_id}/role")
async def patch_organization_member_role(
    user_id: str,
    payload: Dict[str, Any],
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    member, user, _ = await _member_in_church(session, user_id, actor.church_id)
    await require(session, actor, "role.assign")
    raw_roles = (
        payload.get("roles")
        or payload.get("role")
        or payload.get("newRole")
        or ["MEMBER"]
    )
    if isinstance(raw_roles, str):
        raw_roles = [raw_roles]
    roles = await resolve_roles(session, actor.church_id, raw_roles)
    if member.id == (actor.member.id if actor.member else None):
        current_roles = (
            await role_names_for_members(session, [member.id])
        ).get(member.id, [])
        had_admin = any(r.upper() in ADMIN_ROLE_NAMES for r in current_roles)
        will_have_admin = any(r.name.upper() in ADMIN_ROLE_NAMES for r in roles)
        if had_admin and not will_have_admin:
            raise HTTPException(
                status_code=400,
                detail="You cannot remove your own administrator role",
            )
    await set_member_roles(session, member.id, roles, replace=True)
    await session.commit()
    role_names = [r.name for r in roles]
    queue_audit(
        background_tasks,
        request,
        "UPDATE_MEMBER_ROLE",
        "MEMBER",
        member.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
        metadata_json={"roles": role_names},
    )
    return {
        "success": True,
        "message": "Member role updated successfully",
        "user": {
            "id": str(user.id) if user else str(member.id),
            "name": member.full_name,
            "email": (user.email if user else member.email) or "",
            "role": role_names[0] if role_names else "MEMBER",
            "roles": role_names,
            "updatedAt": _iso(_now()) or "",
        },
    }


@router.delete("/members/{user_id}")
@router.delete("/users/{user_id}")
async def remove_organization_member(
    user_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    """
    Remove a member from ONE church (soft delete: giving and attendance history is kept).
    The person's login and their memberships in other churches are untouched.
    """
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    member, _, _ = await _member_in_church(session, user_id, actor.church_id)
    await require(session, actor, "member.delete", member.branch_id)
    if actor.member and member.id == actor.member.id:
        raise HTTPException(status_code=400, detail="You cannot remove yourself")
    member.soft_delete()
    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "REMOVE_MEMBER",
        "MEMBER",
        member.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )
    return {"message": "Member removed successfully"}


# ============================================================================
# BRANCHES (the old "teams"): /admin/branches and /admin/teams
# ============================================================================
async def _member_counts(session, church_id: str) -> Dict[str, int]:
    rows = (
        await session.execute(
            select(Member.branch_id, func.count(Member.id))
            .where(Member.church_id == church_id, Member.deleted_at.is_(None))
            .group_by(Member.branch_id)
        )
    ).all()
    return {str(b): c for b, c in rows}


async def _pastors_for(session, branch_ids: List[str]) -> Dict[str, dict]:
    if not branch_ids:
        return {}
    rows = (
        await session.execute(
            select(
                PastorAssignment.branch_id,
                Member.id,
                Member.first_name,
                Member.last_name,
                Member.email,
            )
            .join(PastorDetails, PastorDetails.id == PastorAssignment.pastor_details_id)
            .join(Member, Member.id == PastorDetails.member_id)
            .where(
                PastorAssignment.branch_id.in_(branch_ids),
                PastorAssignment.is_active.is_(True),
                or_(
                    PastorAssignment.end_date.is_(None),
                    PastorAssignment.end_date > _now(),
                ),
                Member.deleted_at.is_(None),
            )
            .order_by(PastorAssignment.start_date.desc())
        )
    ).all()
    out: Dict[str, dict] = {}
    for bid, mid, first, last, email in rows:
        out.setdefault(
            str(bid), {"id": mid, "first": first, "last": last, "email": email}
        )
    return out


async def _set_branch_pastor(
    session, branch: Branch, church_id: str, pastor_member_id: Optional[str]
) -> None:
    """Ends the branch's current pastor assignment and (unless id is empty) starts a new one."""
    now = _now()
    current = (
        (
            await session.execute(
                select(PastorAssignment).where(
                    PastorAssignment.branch_id == branch.id,
                    PastorAssignment.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for a in current:
        a.is_active = False
        a.end_date = now
    if not pastor_member_id:
        return
    resolved_member_id = (
        await session.execute(
            select(Member.id).where(
                Member.church_id == church_id,
                Member.deleted_at.is_(None),
                or_(Member.id == pastor_member_id, Member.user_id == pastor_member_id),
            )
        )
    ).scalar_one_or_none()
    if not resolved_member_id:
        raise HTTPException(
            status_code=400,
            detail="One or more referenced members do not belong to this church",
        )
    details = await _upsert_detail(session, PastorDetails, str(resolved_member_id), {})
    await session.flush()
    session.add(
        PastorAssignment(
            pastor_details_id=details.id,
            branch_id=branch.id,
            position="Resident Pastor",
            start_date=now,
            is_active=True,
        )
    )


def format_branch_response(
    b: Branch,
    member_count: int = 0,
    address: Optional[Address] = None,
    pastor: Optional[dict] = None,
) -> dict:
    pastor = pastor or {}
    p_first, p_last = pastor.get("first", "Resident"), pastor.get("last", "Pastor")
    pastor_name = f"{p_first} {p_last}".strip()
    meta = dict(b.metadata_json or {})
    addresses = []
    if address:
        addresses.append(
            {
                "id": str(address.id),
                "_id": str(address.id),
                "street": address.street or "",
                "city": address.city or "",
                "state": address.state or "",
                "zipCode": address.zip_code or "",
                "country": address.country or "Kenya",
            }
        )
    return {
        "id": str(b.id),
        "_id": str(b.id),
        "name": b.name,
        "branchName": b.name,
        "description": meta.get("description") or "",
        "organizationId": str(b.church_id),
        "churchId": str(b.church_id),
        "memberCount": member_count,
        "members": member_count,
        "capacity": b.capacity or 500,
        "growth": int(meta.get("growth", 0) or 0),
        "email": b.email or "",
        "phone": b.phone_number or "",
        "phoneNumber": b.phone_number or "",
        "isActive": b.is_active,
        "isDeleted": b.is_deleted,
        "establishedDate": meta.get("establishedDate") or _iso(b.created_at) or "",
        "createdAt": _iso(b.created_at) or "",
        "updatedAt": _iso(b.updated_at) or "",
        "_count": {"teammembers": member_count, "members": member_count},
        "pastorId": {
            "id": str(pastor.get("id", "")),
            "_id": str(pastor.get("id", "")),
            "firstName": p_first,
            "lastName": p_last,
            "name": pastor_name,
            "email": pastor.get("email", ""),
        },
        "teammembers": [],
        "address": addresses,
        "organization": {"id": str(b.church_id), "name": b.name, "slug": ""},
    }


async def _branch_actor(
    session, request: Request, user, branch_id: str, permission: str
) -> Tuple[Branch, Actor]:
    """Loads a branch and authorizes against ITS church (never trusts a client-supplied church id)."""
    branch = (
        await session.execute(
            select(Branch).where(Branch.id == branch_id, Branch.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    actor = await authorize(
        session, user, branch.church_id, permission, branch_id=branch.id
    )
    return branch, actor


async def _branch_payload(session, branch: Branch) -> dict:
    count = (await _member_counts(session, branch.church_id)).get(str(branch.id), 0)
    addr = await session.get(Address, branch.address_id) if branch.address_id else None
    pastor = (await _pastors_for(session, [branch.id])).get(str(branch.id))
    return format_branch_response(branch, count, addr, pastor)


def _compute_branches_analytics(all_branches: List[Branch], counts: Dict[str, int]) -> Dict[str, Any]:
    palette = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"]
    total_branches = len(all_branches)
    active_branches = sum(1 for b in all_branches if b.is_active)
    total_members = sum(counts.get(str(b.id), 0) for b in all_branches)
    total_capacity = sum((b.capacity or 500) for b in all_branches)
    capacity_utilization = (
        round((total_members / total_capacity) * 100) if total_capacity > 0 else 0
    )
    seats_available = max(total_capacity - total_members, 0)

    growth_vals = []
    branch_distribution = []
    attendance_series = []
    branch_avg_attendance: Dict[str, int] = {}

    for idx, b in enumerate(all_branches):
        b_id = str(b.id)
        m_count = counts.get(b_id, 0)
        color = palette[idx % len(palette)]
        meta = dict(b.metadata_json or {})
        schedules = _extract_branch_schedules(b)
        sched_att = (
            round(sum(int(s.get("attendance") or 0) for s in schedules) / len(schedules))
            if schedules
            else m_count
        )
        branch_avg_attendance[f"b_{idx}"] = sched_att if sched_att > 0 else m_count

        if "growth" in meta and meta.get("growth") is not None:
            g_val = int(meta.get("growth") or 0)
        else:
            cap = b.capacity or 500
            g_val = round((m_count / cap) * 20) if cap > 0 and m_count > 0 else 0
        growth_vals.append(g_val)

        branch_distribution.append(
            {
                "name": b.name,
                "value": m_count,
                "color": color,
            }
        )
        attendance_series.append(
            {
                "key": f"b_{idx}",
                "branchId": b_id,
                "label": b.name,
                "color": color,
            }
        )

    avg_growth = round(sum(growth_vals) / len(growth_vals)) if growth_vals else 0

    now = _now()
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    factors = [0.85, 0.88, 0.92, 0.95, 0.98, 1.0]
    attendance_data = []
    for m_offset, factor in enumerate(factors):
        m_idx = (now.month - 1 - (5 - m_offset)) % 12
        row: Dict[str, Any] = {"month": month_names[m_idx], "total": 0}
        total_m = 0
        for s in attendance_series:
            key = s["key"]
            base_att = branch_avg_attendance.get(key, 0)
            val = round(base_att * factor)
            row[key] = val
            total_m += val
        row["total"] = total_m
        attendance_data.append(row)

    return {
        "totalBranches": total_branches,
        "activeBranches": active_branches,
        "totalMembers": total_members,
        "totalCapacity": total_capacity,
        "capacityUtilization": capacity_utilization,
        "seatsAvailable": seats_available,
        "averageGrowth": avg_growth,
        "branchDistribution": branch_distribution,
        "attendanceSeries": attendance_series,
        "attendanceData": attendance_data,
    }


@router.get("/teams")
@router.get("/branches")
async def get_organization_teams(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None, description="Church ID"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(50, ge=1, le=200),
    search: str = Query(""),
    status: Optional[str] = Query(None),
):
    actor = await authorize(
        session, current_user, church_id_from(request, organizationId), "branch.read"
    )
    base_conds = [Branch.church_id == actor.church_id, Branch.deleted_at.is_(None)]
    all_church_branches = (
        (
            await session.execute(
                select(Branch)
                .where(*base_conds)
                .order_by(Branch.created_at.asc(), Branch.name.asc())
            )
        )
        .scalars()
        .all()
    )
    counts = await _member_counts(session, actor.church_id)
    analytics = _compute_branches_analytics(all_church_branches, counts)

    conds = list(base_conds)
    if search:
        conds.append(Branch.name.ilike(f"%{search.strip()}%"))
    if status and status.lower() != "all":
        st_lower = status.strip().lower()
        if st_lower == "active":
            conds.append(Branch.is_active.is_(True))
        elif st_lower == "inactive":
            conds.append(Branch.is_active.is_(False))
        elif st_lower == "growing":
            conds.append(Branch.is_active.is_(True))

    total = (
        await session.execute(select(func.count(Branch.id)).where(*conds))
    ).scalar() or 0
    branches = (
        (
            await session.execute(
                select(Branch)
                .where(*conds)
                .order_by(Branch.created_at.asc(), Branch.name.asc())
                .offset((page - 1) * pageSize)
                .limit(pageSize)
            )
        )
        .scalars()
        .all()
    )
    pastors = await _pastors_for(session, [b.id for b in branches])
    addr_ids = [b.address_id for b in branches if b.address_id]
    addrs = (
        {
            a.id: a
            for a in (
                await session.execute(select(Address).where(Address.id.in_(addr_ids)))
            )
            .scalars()
            .all()
        }
        if addr_ids
        else {}
    )
    items = [
        format_branch_response(
            b, counts.get(str(b.id), 0), addrs.get(b.address_id), pastors.get(str(b.id))
        )
        for b in branches
    ]
    if status and status.strip().lower() == "growing":
        items = [it for it in items if (it.get("memberCount", 0) > 0 or it.get("growth", 0) > 0)]
        total = len(items)
    return {
        "teams": items,
        "branches": items,
        "analytics": analytics,
        "pagination": _pagination(total, page, pageSize),
    }


@router.get("/teams/statistics")
@router.get("/branches/statistics")
async def get_teams_statistics(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await authorize(
        session, current_user, church_id_from(request, organizationId), "branch.read"
    )
    base = [Branch.church_id == actor.church_id, Branch.deleted_at.is_(None)]
    total = (
        await session.execute(select(func.count(Branch.id)).where(*base))
    ).scalar() or 0
    active = (
        await session.execute(
            select(func.count(Branch.id)).where(*base, Branch.is_active.is_(True))
        )
    ).scalar() or 0
    members = sum((await _member_counts(session, actor.church_id)).values())
    return {
        "totalTeams": total,
        "activeTeams": active,
        "totalBranches": total,
        "activeBranches": active,
        "totalMembers": members,
    }


@router.post("/teams")
@router.post("/branches")
async def create_organization_team(
    payload: AdminBranchCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await authorize(
        session,
        current_user,
        church_id_from(request, payload.organizationId),
        "branch.create",
    )
    meta: Dict[str, Any] = {}
    if payload.description is not None:
        meta["description"] = payload.description.strip()
    if payload.establishedDate is not None:
        meta["establishedDate"] = payload.establishedDate
    branch = Branch(
        name=(payload.name or payload.branchName).strip(),
        church_id=actor.church_id,
        email=payload.email,
        phone_number=payload.phoneNumber or payload.phone,
        capacity=payload.capacity if payload.capacity is not None else 500,
        is_active=payload.isActive if payload.isActive is not None else True,
        metadata_json=meta,
    )
    session.add(branch)
    await session.flush()
    if payload.address:
        addr = Address()
        session.add(addr)
        await session.flush()
        for k, v in payload.address.model_dump(exclude_unset=True).items():
            if not (k == "country" and v is None):
                setattr(addr, k, v)
        branch.address_id = addr.id
    if payload.pastorId:
        await _set_branch_pastor(session, branch, actor.church_id, payload.pastorId)
    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "CREATE_BRANCH",
        "BRANCH",
        branch.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )
    formatted = await _branch_payload(session, branch)
    return {**formatted, "branch": formatted, "team": formatted}


@router.get("/teams/{branch_id}")
@router.get("/branches/{branch_id}")
async def get_organization_branch_by_id(
    branch_id: str, request: Request, current_user: CurrentUser, session: DbSession
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.read"
    )
    formatted = await _branch_payload(session, branch)
    return {"branch": formatted, "team": formatted, **formatted}


@router.put("/teams/{branch_id}")
@router.put("/branches/{branch_id}")
async def update_organization_branch_by_id(
    branch_id: str,
    payload: AdminBranchUpdateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    branch, actor = await _branch_actor(
        session, request, current_user, branch_id, "branch.update"
    )
    fields = payload.model_fields_set
    name = payload.name or payload.branchName
    if name:
        branch.name = name.strip()
    if "email" in fields:
        branch.email = payload.email
    if payload.phoneNumber or payload.phone:
        branch.phone_number = payload.phoneNumber or payload.phone
    if payload.capacity is not None:
        branch.capacity = payload.capacity
    if payload.isActive is not None:
        branch.is_active = payload.isActive
    elif payload.status is not None:
        branch.is_active = payload.status.strip().lower() == "active"

    meta = dict(branch.metadata_json or {})
    meta_changed = False
    if "description" in fields:
        meta["description"] = (payload.description or "").strip()
        meta_changed = True
    if "establishedDate" in fields and payload.establishedDate is not None:
        meta["establishedDate"] = payload.establishedDate
        meta_changed = True
    if meta_changed:
        branch.metadata_json = meta
        flag_modified(branch, "metadata_json")

    if payload.address:
        addr = (
            await session.get(Address, branch.address_id) if branch.address_id else None
        )
        if addr is None:
            addr = Address()
            session.add(addr)
            await session.flush()
            branch.address_id = addr.id
        for k, v in payload.address.model_dump(exclude_unset=True).items():
            if not (k == "country" and v is None):
                setattr(addr, k, v)
    if "pastorId" in fields:  # "" clears the pastor
        await _set_branch_pastor(session, branch, actor.church_id, payload.pastorId)
    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "UPDATE_BRANCH",
        "BRANCH",
        branch.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )
    formatted = await _branch_payload(session, branch)
    return {"branch": formatted, "team": formatted, **formatted}


@router.delete("/teams/{branch_id}")
@router.delete("/branches/{branch_id}")
async def delete_organization_branch_by_id(
    branch_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    """Soft delete. Refused while the branch still has members (every member needs a branch)."""
    branch, actor = await _branch_actor(
        session, request, current_user, branch_id, "branch.delete"
    )
    members = (
        await session.execute(
            select(func.count(Member.id)).where(
                Member.branch_id == branch.id, Member.deleted_at.is_(None)
            )
        )
    ).scalar() or 0
    if members:
        raise HTTPException(
            status_code=409,
            detail=f"Move or remove the {members} member(s) in this branch first",
        )
    branch.soft_delete()
    branch.is_active = False
    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "DELETE_BRANCH",
        "BRANCH",
        branch.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )
    return {"message": "Branch deleted successfully"}


# ============================================================================
# BRANCH SUB-RESOURCES (Members, Departments, Schedules, Activities)
# ============================================================================
@router.get("/teams/{branch_id}/members")
@router.get("/branches/{branch_id}/members")
async def get_branch_members(
    branch_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    pageSize: Optional[int] = Query(None, ge=1, le=100),
    search: str = Query(""),
    status: Optional[str] = Query(None),
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "member.read"
    )
    size = pageSize or limit
    conds = [Member.branch_id == branch.id, Member.deleted_at.is_(None)]
    if search:
        conds.append(_search_filter(search))
    st = _status_enum(status)
    if st:
        conds.append(Member.status == st)
    total = (
        await session.execute(
            select(func.count(Member.id))
            .select_from(Member)
            .outerjoin(User, Member.user_id == User.id)
            .where(*conds)
        )
    ).scalar() or 0
    rows = (
        await session.execute(
            select(Member, User)
            .outerjoin(User, Member.user_id == User.id)
            .where(*conds)
            .order_by(Member.created_at.desc(), Member.id)
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    roles_by_member = await role_names_for_members(session, [m.id for m, _ in rows])
    items = []
    for m, u in rows:
        base = format_member_row(m, u, branch, roles_by_member.get(m.id, []))
        roles_arr = base["role"]
        items.append(
            {
                **base,
                "gender": (base["gender"] or "").lower() or None,
                "role": roles_arr[0].upper(),
                "roles": roles_arr,
                "status": m.status.value.lower(),
                "branchId": {"id": str(branch.id), "name": branch.name},
            }
        )
    return {
        "users": items,
        "members": items,
        "pagination": _pagination(total, page, size),
    }


@router.post("/teams/{branch_id}/members")
@router.post("/branches/{branch_id}/members")
async def add_or_assign_branch_member(
    branch_id: str,
    payload: BranchMemberAssignRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    """
    Move an existing member into this branch (a member belongs to exactly one).
    To add a brand-new person use POST /members with this branchId.
    """
    branch, actor = await _branch_actor(
        session, request, current_user, branch_id, "member.update"
    )
    target = payload.memberId or payload.userId
    member = (
        (
            await session.execute(
                select(Member).where(
                    Member.church_id == branch.church_id,
                    Member.deleted_at.is_(None),
                    or_(Member.id == target, Member.user_id == target),
                )
            )
        )
        .scalars()
        .first()
    )
    if not member:
        raise HTTPException(
            status_code=404,
            detail="Member not found in this church. Create the member first (POST /members).",
        )
    if member.branch_id != branch.id:
        await require(
            session, actor, "member.update", member.branch_id
        )  # permission to move them OUT too
        member.branch_id = branch.id
    if payload.position:
        member.position = payload.position
    if payload.roles:
        await require(session, actor, "role.assign", branch.id)
        roles = await resolve_roles(session, branch.church_id, payload.roles)
        await set_member_roles(
            session, member.id, roles, branch_id=branch.id, replace=False
        )  # branch-scoped
    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "ASSIGN_MEMBER_BRANCH",
        "MEMBER",
        member.id,
        actor_user_id=current_user.id,
        church_id=branch.church_id,
    )
    count = (await _member_counts(session, branch.church_id)).get(str(branch.id), 0)
    return {
        "success": True,
        "message": f"Member successfully added to branch {branch.name}",
        "memberId": str(member.id),
        "branchId": str(branch.id),
        "memberCount": count,
    }


@router.get("/teams/{branch_id}/departments")
@router.get("/branches/{branch_id}/departments")
async def get_branch_departments(
    branch_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1),
    search: str = Query(""),
):
    """Departments are church-wide or branch-specific; shown under each branch."""
    from app.routers.departments import _parse_dept_desc

    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.read"
    )
    conds = [Department.church_id == branch.church_id, Department.is_active.is_(True)]
    if search:
        conds.append(Department.name.ilike(f"%{search.strip()}%"))
    departments = (
        (
            await session.execute(
                select(Department).where(*conds).order_by(Department.name)
            )
        )
        .scalars()
        .all()
    )
    counts = dict(
        (
            await session.execute(
                select(DepartmentMember.department_id, func.count(DepartmentMember.id))
                .where(
                    DepartmentMember.department_id.in_(
                        [d.id for d in departments] or [""]
                    )
                )
                .group_by(DepartmentMember.department_id)
            )
        ).all()
    )
    items = []
    for d in departments:
        clean_desc, meta = _parse_dept_desc(d.description)
        dept_branch_id = meta.get("branchId")
        if dept_branch_id and str(dept_branch_id) != str(branch.id):
            continue
        m_count = counts.get(d.id, 0)
        budget_items = meta.get("budgetCategories", meta.get("budget", []))
        total_budget = (
            sum(float(b.get("allocatedAmount", 0)) for b in budget_items)
            if budget_items
            else float(meta.get("totalBudget", 0))
        )
        leader = None
        if d.leader_member_id:
            mem = await session.get(Member, d.leader_member_id)
            if mem:
                leader = {
                    "_id": str(mem.id),
                    "id": str(mem.id),
                    "firstName": mem.first_name,
                    "lastName": mem.last_name,
                    "email": mem.email or "",
                    "phoneNumber": mem.phone_number or "",
                }
        items.append(
            {
                "id": str(d.id),
                "_id": str(d.id),
                "name": d.name,
                "departmentName": d.name,
                "description": clean_desc,
                "category": meta.get("category", "ministry"),
                "location": meta.get("location", branch.name or "Main Sanctuary"),
                "meetingDay": meta.get("meetingDay", []),
                "meetingTime": meta.get("meetingTime", []),
                "totalBudget": total_budget,
                "leaderId": leader,
                "isActive": d.is_active,
                "branchId": str(dept_branch_id or branch.id),
                "churchId": str(branch.church_id),
                "createdAt": _iso(d.created_at) or "",
                "updatedAt": _iso(d.updated_at) or "",
                "memberCount": m_count,
                "_count": {"members": m_count},
            }
        )
    total = len(items)
    offset = (page - 1) * limit
    paginated = items[offset : offset + limit]
    return {
        "departments": paginated,
        "pagination": _pagination(total, page, limit),
    }


async def _resolve_facilitator(
    session, raw_facilitator: Any, church_id: str
) -> Optional[Dict[str, Any]]:
    if not raw_facilitator:
        return None
    if isinstance(raw_facilitator, dict):
        fid = raw_facilitator.get("_id") or raw_facilitator.get("id") or ""
        if raw_facilitator.get("firstName"):
            return {
                "_id": str(fid),
                "id": str(fid),
                "firstName": raw_facilitator.get("firstName", ""),
                "lastName": raw_facilitator.get("lastName", ""),
                "email": raw_facilitator.get("email", ""),
                "phoneNumber": raw_facilitator.get("phoneNumber", ""),
            }
        raw_facilitator = fid
    if isinstance(raw_facilitator, str) and raw_facilitator.strip():
        target = raw_facilitator.strip()
        m = (
            await session.execute(
                select(Member).where(
                    Member.church_id == church_id,
                    Member.deleted_at.is_(None),
                    or_(Member.id == target, Member.user_id == target),
                )
            )
        ).scalars().first()
        if m:
            return {
                "_id": str(m.id),
                "id": str(m.id),
                "firstName": m.first_name,
                "lastName": m.last_name,
                "email": m.email or "",
                "phoneNumber": m.phone_number or "",
            }
        u = await session.get(User, target)
        if u:
            parts = (u.full_name or u.email or "Facilitator").split(" ", 1)
            return {
                "_id": str(u.id),
                "id": str(u.id),
                "firstName": parts[0],
                "lastName": parts[1] if len(parts) > 1 else "",
                "email": u.email or "",
                "phoneNumber": u.phone or "",
            }
    return None


def _normalize_stored_facilitator(fac: Any) -> Optional[Dict[str, Any]]:
    if not fac:
        return None
    if isinstance(fac, dict) and fac.get("firstName"):
        fid = str(fac.get("_id") or fac.get("id") or "")
        return {
            "_id": fid,
            "id": fid,
            "firstName": fac.get("firstName", ""),
            "lastName": fac.get("lastName", ""),
            "email": fac.get("email", ""),
            "phoneNumber": fac.get("phoneNumber", ""),
        }
    return None


def _extract_branch_schedules(branch: Branch) -> List[Dict[str, Any]]:
    meta = dict(branch.metadata_json or {})
    stored = meta.get("serviceSchedules")
    if isinstance(stored, list) and len(stored) > 0:
        out = []
        for s in stored:
            if not isinstance(s, dict):
                continue
            svc_name = s.get("service") or s.get("name") or "Sunday Service"
            sid = str(s.get("id") or s.get("_id") or f"{branch.id}-0")
            out.append(
                {
                    **s,
                    "id": sid,
                    "_id": sid,
                    "name": svc_name,
                    "service": svc_name,
                    "day": (s.get("day") or "sunday").lower(),
                    "time": s.get("time") or "09:00 AM",
                    "type": (s.get("type") or "worship").lower(),
                    "attendance": int(s.get("attendance") or 100),
                    "duration": int(s.get("duration") or 90),
                    "location": s.get("location") or branch.name or "Main Sanctuary",
                    "facilitator": _normalize_stored_facilitator(s.get("facilitator")),
                    "recurring": bool(s.get("recurring", True)),
                    "isActive": bool(s.get("isActive", True)),
                    "churchId": str(branch.church_id),
                    "branchId": {
                        "_id": str(branch.id),
                        "id": str(branch.id),
                        "branchName": branch.name,
                        "name": branch.name,
                        "address": "",
                        "country": "Kenya",
                    },
                    "createdAt": s.get("createdAt") or _iso(branch.created_at) or "",
                    "updatedAt": s.get("updatedAt") or _iso(branch.updated_at) or "",
                }
            )
        return out

    services = meta.get("services", ["Sunday 9:00 AM", "Sunday 11:00 AM"])
    schedules = []
    for i, svc in enumerate(services):
        svc_name = svc if isinstance(svc, str) else svc.get("name", "Sunday Service")
        sid = f"{branch.id}-{i}"
        schedules.append(
            {
                "id": sid,
                "_id": sid,
                "name": svc_name,
                "service": svc_name,
                "day": "sunday",
                "time": "09:00" if i == 0 else "11:00",
                "type": "worship",
                "attendance": int(branch.capacity or 200),
                "duration": 90,
                "location": branch.name or "Main Sanctuary",
                "facilitator": None,
                "recurring": True,
                "isActive": True,
                "churchId": str(branch.church_id),
                "branchId": {
                    "_id": str(branch.id),
                    "id": str(branch.id),
                    "branchName": branch.name,
                    "name": branch.name,
                    "address": "",
                    "country": "Kenya",
                },
                "createdAt": _iso(branch.created_at) or "",
                "updatedAt": _iso(branch.updated_at) or "",
            }
        )
    return schedules


def _extract_branch_activities(branch: Branch) -> List[Dict[str, Any]]:
    meta = dict(branch.metadata_json or {})
    stored = meta.get("activities")
    if not isinstance(stored, list):
        return []
    out = []
    for a in stored:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or a.get("_id") or uuid.uuid4())
        out.append(
            {
                **a,
                "id": aid,
                "_id": aid,
                "churchId": str(branch.church_id),
                "branchId": str(branch.id),
                "activity": a.get("activity") or a.get("name") or "Branch Activity",
                "date": a.get("date") or _iso(branch.created_at) or "",
                "participants": int(a.get("participants") or 0),
                "type": (a.get("type") or "event").lower(),
                "status": (a.get("status") or "planned").lower(),
                "startTime": a.get("startTime") or "09:00",
                "endTime": a.get("endTime") or "11:00",
                "location": a.get("location") or branch.name or "Main Hall",
                "facilitator": _normalize_stored_facilitator(a.get("facilitator")),
                "budget": float(a.get("budget") or 0),
                "description": a.get("description") or "",
                "createdAt": a.get("createdAt") or _iso(branch.created_at) or "",
                "updatedAt": a.get("updatedAt") or _iso(branch.updated_at) or "",
            }
        )
    return out


@router.patch("/members/{user_id}/role")
@router.patch("/users/{user_id}/role")
async def patch_member_role(
    user_id: str,
    payload: Dict[str, Any],
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    org_id = payload.get("organizationId")
    actor = await authorize(
        session, current_user, church_id_from(request, org_id), "role.assign"
    )
    member, _, _ = await _member_in_church(session, user_id, actor.church_id)
    new_role = payload.get("newRole") or payload.get("role") or payload.get("roles")
    role_list = [new_role] if isinstance(new_role, str) else (new_role or ["MEMBER"])
    roles = await resolve_roles(session, actor.church_id, role_list)
    await set_member_roles(session, member.id, roles, replace=True)
    await session.commit()
    return {"success": True, "message": "Member role updated successfully"}


@router.patch("/teams/{branch_id}/status")
@router.patch("/branches/{branch_id}/status")
async def toggle_branch_status(
    branch_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.update"
    )
    branch.is_active = not branch.is_active
    branch.updated_at = _now()
    await session.commit()
    return {
        "success": True,
        "message": "Branch status updated successfully",
        "isActive": branch.is_active,
        "status": "ACTIVE" if branch.is_active else "INACTIVE",
    }


@router.get("/service-schedules")
async def get_all_service_schedules(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1),
    search: str = Query(""),
    day: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    branchId: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    conds = [Branch.church_id == actor.church_id, Branch.deleted_at.is_(None)]
    if branchId:
        conds.append(Branch.id == branchId)
    branches = (
        await session.execute(select(Branch).where(*conds).order_by(Branch.name))
    ).scalars().all()
    all_schedules: List[Dict[str, Any]] = []
    for b in branches:
        all_schedules.extend(_extract_branch_schedules(b))
    if not all_schedules and not branchId:
        all_schedules = [
            {
                "id": f"{actor.church_id}-0",
                "_id": f"{actor.church_id}-0",
                "name": "Sunday Morning Service",
                "service": "Sunday Morning Service",
                "day": "sunday",
                "time": "09:00",
                "type": "worship",
                "attendance": 200,
                "duration": 120,
                "location": "Main Sanctuary",
                "recurring": True,
                "isActive": True,
                "churchId": str(actor.church_id),
                "branchId": {
                    "_id": str(actor.church_id),
                    "id": str(actor.church_id),
                    "branchName": "Main Campus",
                    "name": "Main Campus",
                },
            }
        ]
    if search:
        s = search.lower()
        all_schedules = [
            item
            for item in all_schedules
            if s in (item.get("service") or "").lower()
            or s in (item.get("location") or "").lower()
        ]
    if day:
        all_schedules = [
            item for item in all_schedules if (item.get("day") or "").lower() == day.lower()
        ]
    if type:
        all_schedules = [
            item for item in all_schedules if (item.get("type") or "").lower() == type.lower()
        ]
    if status and status != "all":
        want_active = status.lower() == "active"
        all_schedules = [
            item for item in all_schedules if bool(item.get("isActive", True)) == want_active
        ]
    total = len(all_schedules)
    offset = (page - 1) * limit
    paginated = all_schedules[offset : offset + limit]
    return {
        "schedules": paginated,
        "serviceSchedules": paginated,
        "pagination": _pagination(total, page, limit),
    }


@router.get("/teams/{branch_id}/service-schedules")
@router.get("/branches/{branch_id}/service-schedules")
async def get_branch_service_schedules(
    branch_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1),
    search: str = Query(""),
    day: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.read"
    )
    schedules = _extract_branch_schedules(branch)
    if search:
        s = search.lower()
        schedules = [
            item
            for item in schedules
            if s in (item.get("service") or "").lower()
            or s in (item.get("location") or "").lower()
        ]
    if day:
        schedules = [
            item for item in schedules if (item.get("day") or "").lower() == day.lower()
        ]
    if type:
        schedules = [
            item for item in schedules if (item.get("type") or "").lower() == type.lower()
        ]
    if status and status != "all":
        want_active = status.lower() == "active"
        schedules = [
            item for item in schedules if bool(item.get("isActive", True)) == want_active
        ]
    total = len(schedules)
    offset = (page - 1) * limit
    paginated = schedules[offset : offset + limit]
    return {
        "schedules": paginated,
        "serviceSchedules": paginated,
        "pagination": _pagination(total, page, limit),
    }


@router.post("/teams/{branch_id}/service-schedules")
@router.post("/branches/{branch_id}/service-schedules")
async def create_branch_service_schedule(
    branch_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.update"
    )
    schedules = _extract_branch_schedules(branch)
    sid = str(uuid.uuid4())
    svc_name = (payload.get("service") or payload.get("name") or "Sunday Service").strip()
    facilitator = await _resolve_facilitator(session, payload.get("facilitator"), str(branch.church_id))
    new_item = {
        "id": sid,
        "_id": sid,
        "name": svc_name,
        "service": svc_name,
        "day": (payload.get("day") or "sunday").lower(),
        "time": payload.get("time") or "09:00",
        "type": (payload.get("type") or "worship").lower(),
        "attendance": int(float(payload.get("attendance") or 100)),
        "duration": int(float(payload.get("duration") or 90)),
        "location": payload.get("location") or branch.name or "Main Sanctuary",
        "facilitator": facilitator,
        "recurring": bool(payload.get("recurring", True)),
        "isActive": bool(payload.get("isActive", True)),
        "startDate": payload.get("startDate"),
        "endDate": payload.get("endDate"),
        "notes": payload.get("notes") or "",
        "createdAt": _iso(_now()),
        "updatedAt": _iso(_now()),
    }
    raw_list = [
        {k: v for k, v in s.items() if k != "branchId"}
        for s in schedules
    ]
    raw_list.insert(0, new_item)
    meta = dict(branch.metadata_json or {})
    meta["serviceSchedules"] = raw_list
    branch.metadata_json = meta
    flag_modified(branch, "metadata_json")
    await session.commit()
    formatted = _extract_branch_schedules(branch)[0]
    return {
        **formatted,
        "schedule": formatted,
        "message": "Service schedule has been created successfully.",
    }


@router.get("/teams/{branch_id}/service-schedules/{schedule_id}")
@router.get("/branches/{branch_id}/service-schedules/{schedule_id}")
async def get_branch_service_schedule_by_id(
    branch_id: str,
    schedule_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.read"
    )
    schedules = _extract_branch_schedules(branch)
    found = next(
        (s for s in schedules if s.get("id") == schedule_id or s.get("_id") == schedule_id),
        None,
    )
    if not found:
        raise HTTPException(status_code=404, detail="Service schedule not found")
    return {"schedule": found, "branch": {"id": str(branch.id), "name": branch.name}}


@router.put("/teams/{branch_id}/service-schedules/{schedule_id}")
@router.put("/branches/{branch_id}/service-schedules/{schedule_id}")
async def update_branch_service_schedule(
    branch_id: str,
    schedule_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.update"
    )
    schedules = _extract_branch_schedules(branch)
    updated_item = None
    raw_list = []
    for s in schedules:
        item = {k: v for k, v in s.items() if k != "branchId"}
        if item.get("id") == schedule_id or item.get("_id") == schedule_id:
            if "service" in payload or "name" in payload:
                svc_name = (payload.get("service") or payload.get("name") or item["service"]).strip()
                item["service"] = svc_name
                item["name"] = svc_name
            for field in ("day", "time", "type", "location", "startDate", "endDate", "notes"):
                if field in payload and payload[field] is not None:
                    item[field] = payload[field]
            if "facilitator" in payload:
                item["facilitator"] = await _resolve_facilitator(
                    session, payload.get("facilitator"), str(branch.church_id)
                )
            if "attendance" in payload and payload["attendance"] is not None:
                item["attendance"] = int(float(payload["attendance"]))
            if "duration" in payload and payload["duration"] is not None:
                item["duration"] = int(float(payload["duration"]))
            if "recurring" in payload and payload["recurring"] is not None:
                item["recurring"] = bool(payload["recurring"])
            if "isActive" in payload and payload["isActive"] is not None:
                item["isActive"] = bool(payload["isActive"])
            item["updatedAt"] = _iso(_now())
            updated_item = item
        raw_list.append(item)
    if not updated_item:
        raise HTTPException(status_code=404, detail="Service schedule not found")
    meta = dict(branch.metadata_json or {})
    meta["serviceSchedules"] = raw_list
    branch.metadata_json = meta
    flag_modified(branch, "metadata_json")
    await session.commit()
    refreshed = next(
        s for s in _extract_branch_schedules(branch) if s["id"] == schedule_id
    )
    return {
        "schedule": refreshed,
        "branch": {"id": str(branch.id), "name": branch.name},
        "message": "Service schedule has been updated successfully.",
    }


@router.delete("/teams/{branch_id}/service-schedules/{schedule_id}")
@router.delete("/branches/{branch_id}/service-schedules/{schedule_id}")
async def delete_branch_service_schedule(
    branch_id: str,
    schedule_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    force: bool = Query(False),
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.update"
    )
    schedules = _extract_branch_schedules(branch)
    raw_list = [
        {k: v for k, v in s.items() if k != "branchId"}
        for s in schedules
        if s.get("id") != schedule_id and s.get("_id") != schedule_id
    ]
    meta = dict(branch.metadata_json or {})
    meta["serviceSchedules"] = raw_list
    branch.metadata_json = meta
    flag_modified(branch, "metadata_json")
    await session.commit()
    return {"message": "Service schedule has been deleted successfully."}


@router.get("/activities")
async def get_all_branch_activities(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1),
    search: str = Query(""),
    type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    branchId: Optional[str] = Query(None),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    conds = [Branch.church_id == actor.church_id, Branch.deleted_at.is_(None)]
    if branchId:
        conds.append(Branch.id == branchId)
    branches = (
        await session.execute(select(Branch).where(*conds).order_by(Branch.name))
    ).scalars().all()
    activities: List[Dict[str, Any]] = []
    for b in branches:
        activities.extend(_extract_branch_activities(b))
    if search:
        s = search.lower()
        activities = [
            a for a in activities if s in (a.get("activity") or "").lower()
        ]
    if type:
        activities = [
            a for a in activities if (a.get("type") or "").lower() == type.lower()
        ]
    if status:
        activities = [
            a for a in activities if (a.get("status") or "").lower() == status.lower()
        ]
    total = len(activities)
    offset = (page - 1) * limit
    paginated = activities[offset : offset + limit]
    return {
        "activities": paginated,
        "pagination": _pagination(total, page, limit),
    }


@router.get("/teams/{branch_id}/activities")
@router.get("/branches/{branch_id}/activities")
async def get_branch_activities(
    branch_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1),
    search: str = Query(""),
    type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
):
    branch, _ = await _branch_actor(session, request, current_user, branch_id, "branch.read")
    activities = _extract_branch_activities(branch)
    if search:
        s = search.lower()
        activities = [
            a for a in activities if s in (a.get("activity") or "").lower()
        ]
    if type:
        activities = [
            a for a in activities if (a.get("type") or "").lower() == type.lower()
        ]
    if status:
        activities = [
            a for a in activities if (a.get("status") or "").lower() == status.lower()
        ]
    total = len(activities)
    offset = (page - 1) * limit
    paginated = activities[offset : offset + limit]
    return {
        "activities": paginated,
        "pagination": _pagination(total, page, limit),
    }


@router.post("/teams/{branch_id}/activities")
@router.post("/branches/{branch_id}/activities")
async def create_branch_activity(
    branch_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.update"
    )
    activities = _extract_branch_activities(branch)
    aid = str(uuid.uuid4())
    facilitator = await _resolve_facilitator(session, payload.get("facilitator"), str(branch.church_id))
    new_act = {
        "id": aid,
        "_id": aid,
        "churchId": str(branch.church_id),
        "branchId": str(branch.id),
        "activity": (payload.get("activity") or payload.get("name") or "Branch Activity").strip(),
        "date": payload.get("date") or _iso(_now()),
        "participants": int(float(payload.get("participants") or 0)),
        "type": (payload.get("type") or "event").lower(),
        "status": (payload.get("status") or "planned").lower(),
        "startTime": payload.get("startTime") or "09:00",
        "endTime": payload.get("endTime") or "11:00",
        "location": payload.get("location") or branch.name or "Main Hall",
        "facilitator": facilitator,
        "budget": float(payload.get("budget") or 0),
        "description": payload.get("description") or "",
        "createdAt": _iso(_now()),
        "updatedAt": _iso(_now()),
    }
    activities.insert(0, new_act)
    meta = dict(branch.metadata_json or {})
    meta["activities"] = activities
    branch.metadata_json = meta
    flag_modified(branch, "metadata_json")
    await session.commit()
    return {
        **new_act,
        "activity": new_act["activity"],
        "data": new_act,
        "message": "Activity has been created successfully.",
    }


@router.get("/teams/{branch_id}/activities/{activity_id}")
@router.get("/branches/{branch_id}/activities/{activity_id}")
async def get_branch_activity_by_id(
    branch_id: str,
    activity_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.read"
    )
    activities = _extract_branch_activities(branch)
    found = next(
        (a for a in activities if a.get("id") == activity_id or a.get("_id") == activity_id),
        None,
    )
    if not found:
        raise HTTPException(status_code=404, detail="Activity not found")
    return {"activity": found, "branch": {"id": str(branch.id), "name": branch.name}}


@router.put("/teams/{branch_id}/activities/{activity_id}")
@router.put("/branches/{branch_id}/activities/{activity_id}")
async def update_branch_activity(
    branch_id: str,
    activity_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.update"
    )
    activities = _extract_branch_activities(branch)
    updated_act = None
    for a in activities:
        if a.get("id") == activity_id or a.get("_id") == activity_id:
            for field in ("activity", "date", "type", "status", "startTime", "endTime", "location", "description"):
                if field in payload and payload[field] is not None:
                    a[field] = payload[field]
            if "facilitator" in payload:
                a["facilitator"] = await _resolve_facilitator(
                    session, payload.get("facilitator"), str(branch.church_id)
                )
            if "participants" in payload and payload["participants"] is not None:
                a["participants"] = int(float(payload["participants"]))
            if "budget" in payload and payload["budget"] is not None:
                a["budget"] = float(payload["budget"])
            a["updatedAt"] = _iso(_now())
            updated_act = a
    if not updated_act:
        raise HTTPException(status_code=404, detail="Activity not found")
    meta = dict(branch.metadata_json or {})
    meta["activities"] = activities
    branch.metadata_json = meta
    flag_modified(branch, "metadata_json")
    await session.commit()
    return {
        "activity": updated_act,
        "branch": {"id": str(branch.id), "name": branch.name},
        "message": "Activity has been updated successfully.",
    }


@router.delete("/teams/{branch_id}/activities/{activity_id}")
@router.delete("/branches/{branch_id}/activities/{activity_id}")
async def delete_branch_activity(
    branch_id: str,
    activity_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    force: bool = Query(False),
    cancel: bool = Query(False),
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.update"
    )
    activities = _extract_branch_activities(branch)
    if cancel:
        for a in activities:
            if a.get("id") == activity_id or a.get("_id") == activity_id:
                a["status"] = "cancelled"
                a["updatedAt"] = _iso(_now())
    else:
        activities = [
            a for a in activities if a.get("id") != activity_id and a.get("_id") != activity_id
        ]
    meta = dict(branch.metadata_json or {})
    meta["activities"] = activities
    branch.metadata_json = meta
    flag_modified(branch, "metadata_json")
    await session.commit()
    return {"message": "Activity has been deleted successfully."}


async def _find_branch_for_schedule(
    session: DbSession, church_id: str, schedule_id: str
) -> Tuple[Branch, Dict[str, Any]]:
    branches = (
        await session.execute(
            select(Branch).where(
                Branch.church_id == church_id, Branch.deleted_at.is_(None)
            )
        )
    ).scalars().all()
    for b in branches:
        for s in _extract_branch_schedules(b):
            if s.get("id") == schedule_id or s.get("_id") == schedule_id:
                return b, s
    raise HTTPException(status_code=404, detail="Service schedule not found")


async def _find_branch_for_activity(
    session: DbSession, church_id: str, activity_id: str
) -> Tuple[Branch, Dict[str, Any]]:
    branches = (
        await session.execute(
            select(Branch).where(
                Branch.church_id == church_id, Branch.deleted_at.is_(None)
            )
        )
    ).scalars().all()
    for b in branches:
        for a in _extract_branch_activities(b):
            if a.get("id") == activity_id or a.get("_id") == activity_id:
                return b, a
    raise HTTPException(status_code=404, detail="Activity not found")


@router.get("/service-schedules/{schedule_id}")
async def get_church_service_schedule_by_id(
    schedule_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    branch, found = await _find_branch_for_schedule(session, actor.church_id, schedule_id)
    return {"schedule": found, "branch": {"id": str(branch.id), "name": branch.name}}


@router.put("/service-schedules/{schedule_id}")
async def update_church_service_schedule_by_id(
    schedule_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    branch, _ = await _find_branch_for_schedule(session, actor.church_id, schedule_id)
    return await update_branch_service_schedule(
        str(branch.id), schedule_id, payload, request, current_user, session
    )


@router.delete("/service-schedules/{schedule_id}")
async def delete_church_service_schedule_by_id(
    schedule_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    force: bool = Query(False),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    branch, _ = await _find_branch_for_schedule(session, actor.church_id, schedule_id)
    return await delete_branch_service_schedule(
        str(branch.id), schedule_id, request, current_user, session, force
    )


@router.get("/activities/{activity_id}")
async def get_church_activity_by_id(
    activity_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    branch, found = await _find_branch_for_activity(session, actor.church_id, activity_id)
    return {"activity": found, "branch": {"id": str(branch.id), "name": branch.name}}


@router.put("/activities/{activity_id}")
async def update_church_activity_by_id(
    activity_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    branch, _ = await _find_branch_for_activity(session, actor.church_id, activity_id)
    return await update_branch_activity(
        str(branch.id), activity_id, payload, request, current_user, session
    )


@router.delete("/activities/{activity_id}")
async def delete_church_activity_by_id(
    activity_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    force: bool = Query(False),
    cancel: bool = Query(False),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    branch, _ = await _find_branch_for_activity(session, actor.church_id, activity_id)
    return await delete_branch_activity(
        str(branch.id), activity_id, request, current_user, session, force, cancel
    )


# ============================================================================
# CHURCH SETTINGS, ADMINS, ROLES & PERMISSIONS
# ============================================================================
LEADERSHIP_ROLE_NAMES = {
    "OWNER",
    "ADMIN",
    "PASTOR",
    "BISHOP",
    "STAFF",
    "TREASURER",
}

ROLE_DISPLAY_LABELS = {
    "OWNER": "Church Owner",
    "ADMIN": "Church Administrator",
    "PASTOR": "Pastor",
    "BISHOP": "Bishop",
    "STAFF": "Church Staff",
    "TREASURER": "Treasurer",
    "VOLUNTEER": "Volunteer",
    "MEMBER": "Member",
    "VISITOR": "Visitor",
}


def _format_role_display_name(name: str) -> str:
    upper = (name or "").strip().upper()
    if upper in ROLE_DISPLAY_LABELS:
        return ROLE_DISPLAY_LABELS[upper]
    return " ".join(w.capitalize() for w in (name or "").replace("_", " ").split())


def _permission_category(perm_name: str) -> str:
    prefix = (perm_name or "").split(".")[0].lower()
    mapping = {
        "member": "Members",
        "branch": "Branches",
        "role": "Roles & Access",
        "event": "Events",
        "group": "Groups",
        "offering": "Finance",
        "fund": "Finance",
        "audit": "System & Audit",
        "content": "Content & Media",
        "communication": "Communications",
    }
    return mapping.get(prefix, "General")


async def _get_effective_church_roles(
    session: DbSession, church_id: str
) -> Tuple[List[Role], Dict[str, Role], Dict[str, Optional[Role]]]:
    """
    Returns:
      - effective_roles: list of Role rows active for this church (church override beats system role)
      - by_upper_name: dict of UPPER_NAME -> effective Role
      - system_by_upper_name: dict of UPPER_NAME -> system Role (church_id IS NULL)
    """
    all_roles = (
        await session.execute(
            select(Role).where(
                or_(Role.church_id == church_id, Role.church_id.is_(None))
            )
        )
    ).scalars().all()
    system_by_upper: Dict[str, Optional[Role]] = {}
    by_upper: Dict[str, Role] = {}
    for r in all_roles:
        if r.church_id is None:
            system_by_upper[r.name.upper()] = r
    # Sort so church-specific roles come first and win over system roles of the same name
    for r in sorted(all_roles, key=lambda item: (item.church_id is None, item.name.upper())):
        by_upper.setdefault(r.name.upper(), r)

    # Order: built-in roles in ROLE_DESCRIPTIONS order first, then custom roles alphabetically
    ordered_keys = [
        k for k in ROLE_DESCRIPTIONS.keys() if k in by_upper
    ] + sorted(k for k in by_upper.keys() if k not in ROLE_DESCRIPTIONS)
    return [by_upper[k] for k in ordered_keys], by_upper, system_by_upper


async def _permissions_by_role_ids(
    session: DbSession, role_ids: List[str]
) -> Dict[str, List[Permission]]:
    out: Dict[str, List[Permission]] = {rid: [] for rid in role_ids}
    if not role_ids:
        return out
    rows = (
        await session.execute(
            select(role_permission.c.role_id, Permission)
            .join(Permission, Permission.id == role_permission.c.permission_id)
            .where(role_permission.c.role_id.in_(role_ids))
            .order_by(Permission.name)
        )
    ).all()
    for rid, perm in rows:
        out.setdefault(str(rid), []).append(perm)
    return out


def _serialize_church_settings(church: Church, addr: Optional[Address]) -> Dict[str, Any]:
    meta = dict(church.metadata_json or {})
    settings_meta = dict(meta.get("settings") or {})
    addr_parts = [
        p
        for p in [
            (addr.street if addr else "") or "",
            (addr.city if addr else "") or "",
            (addr.country if addr else "") or "",
        ]
        if p
    ]
    formatted_addr = ", ".join(addr_parts)
    return {
        "churchId": str(church.id),
        "churchName": church.name or "",
        "slug": church.slug or "",
        "logo": settings_meta.get("logo") or church.logo or "",
        "denomination": church.denomination or "",
        "description": church.description or "",
        "address": settings_meta.get("address") or formatted_addr,
        "addressDetails": {
            "street": (addr.street if addr else "") or "",
            "city": (addr.city if addr else "") or "",
            "state": (addr.state if addr else "") or "",
            "zipCode": (addr.zip_code if addr else "") or "",
            "country": (addr.country if addr else "Kenya") or "Kenya",
        },
        "phone": church.phone_number or "",
        "email": church.email or "",
        "website": church.website or "",
        "timezone": settings_meta.get("timezone") or "Africa/Nairobi",
        "currency": settings_meta.get("currency") or "KES",
        "dateFormat": settings_meta.get("dateFormat") or "DD/MM/YYYY",
        "emailNotifications": bool(settings_meta.get("emailNotifications", True)),
        "smsNotifications": bool(settings_meta.get("smsNotifications", True)),
        "autoBackup": bool(settings_meta.get("autoBackup", True)),
        "maintenanceMode": bool(settings_meta.get("maintenanceMode", False)),
        "publicRegistration": bool(settings_meta.get("publicRegistration", True)),
        "requireApproval": bool(settings_meta.get("requireApproval", True)),
        "lastBackupAt": settings_meta.get("lastBackupAt"),
        "updatedAt": _iso(church.updated_at),
    }


@router.get("/settings")
async def get_church_settings(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church = await session.get(Church, actor.church_id)
    if not church or church.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Church not found")
    addr = await session.get(Address, church.address_id) if church.address_id else None
    data = _serialize_church_settings(church, addr)
    return {**data, "settings": data}


@router.put("/settings")
@router.patch("/settings")
async def update_church_settings(
    payload: Dict[str, Any],
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    # Require branch.update or role.assign
    if actor.member is not None:
        await require(session, actor, "branch.update")
    church = await session.get(Church, actor.church_id)
    if not church or church.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Church not found")

    if "churchName" in payload or "name" in payload:
        new_name = (payload.get("churchName") or payload.get("name") or "").strip()
        if new_name:
            church.name = new_name
    if "phone" in payload or "phoneNumber" in payload:
        church.phone_number = (
            payload.get("phone")
            if "phone" in payload
            else payload.get("phoneNumber")
        )
    if "email" in payload:
        church.email = (payload.get("email") or "").strip() or None
    if "website" in payload:
        church.website = (payload.get("website") or "").strip() or None
    if "logo" in payload:
        logo_val = (payload.get("logo") or "").strip() or None
        if logo_val and len(logo_val) <= 500:
            church.logo = logo_val
        elif not logo_val:
            church.logo = None
    if "denomination" in payload:
        church.denomination = (payload.get("denomination") or "").strip() or None
    if "description" in payload:
        church.description = (payload.get("description") or "").strip() or None

    addr = await session.get(Address, church.address_id) if church.address_id else None
    if "addressDetails" in payload and isinstance(payload["addressDetails"], dict):
        ad = payload["addressDetails"]
        if addr is None:
            addr = Address(country=ad.get("country") or "Kenya")
            session.add(addr)
            await session.flush()
            church.address_id = addr.id
        for key, attr in (
            ("street", "street"),
            ("city", "city"),
            ("state", "state"),
            ("zipCode", "zip_code"),
            ("country", "country"),
        ):
            if key in ad and ad[key] is not None:
                setattr(addr, attr, ad[key])
    elif "address" in payload and isinstance(payload["address"], str):
        raw_addr = payload["address"].strip()
        if addr is None:
            addr = Address(street=raw_addr, country="Kenya")
            session.add(addr)
            await session.flush()
            church.address_id = addr.id
        else:
            addr.street = raw_addr

    meta = dict(church.metadata_json or {})
    settings_meta = dict(meta.get("settings") or {})
    for key in (
        "logo",
        "address",
        "timezone",
        "currency",
        "dateFormat",
        "emailNotifications",
        "smsNotifications",
        "autoBackup",
        "maintenanceMode",
        "publicRegistration",
        "requireApproval",
    ):
        if key in payload and payload[key] is not None:
            settings_meta[key] = payload[key]
    meta["settings"] = settings_meta
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "UPDATE_CHURCH_SETTINGS",
        "CHURCH",
        church.id,
        actor_user_id=current_user.id,
        church_id=church.id,
    )
    data = _serialize_church_settings(church, addr)
    return {
        **data,
        "settings": data,
        "message": "Church settings updated successfully",
    }


@router.post("/settings/backup")
async def create_church_backup(
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    if actor.member is not None:
        await require(session, actor, "branch.update")
    church = await session.get(Church, actor.church_id)
    if not church or church.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Church not found")
    addr = await session.get(Address, church.address_id) if church.address_id else None

    branches = (
        await session.execute(
            select(Branch).where(
                Branch.church_id == church.id, Branch.deleted_at.is_(None)
            )
        )
    ).scalars().all()
    members = (
        await session.execute(
            select(Member).where(
                Member.church_id == church.id, Member.deleted_at.is_(None)
            )
        )
    ).scalars().all()
    roles_by_member = await role_names_for_members(session, [m.id for m in members])

    now_iso = _iso(_now())
    meta = dict(church.metadata_json or {})
    settings_meta = dict(meta.get("settings") or {})
    settings_meta["lastBackupAt"] = now_iso
    meta["settings"] = settings_meta
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    queue_audit(
        background_tasks,
        request,
        "BACKUP_CHURCH_DATA",
        "CHURCH",
        church.id,
        actor_user_id=current_user.id,
        church_id=church.id,
    )

    backup_payload = {
        "exportedAt": now_iso,
        "church": _serialize_church_settings(church, addr),
        "summary": {
            "totalBranches": len(branches),
            "totalMembers": len(members),
        },
        "branches": [
            {
                "id": str(b.id),
                "name": b.name,
                "email": b.email,
                "phoneNumber": b.phone_number,
                "capacity": b.capacity,
                "isActive": b.is_active,
            }
            for b in branches
        ],
        "members": [
            {
                "id": str(m.id),
                "membershipNumber": m.membership_number,
                "firstName": m.first_name,
                "lastName": m.last_name,
                "email": m.email,
                "phoneNumber": m.phone_number,
                "status": m.status.value,
                "memberType": m.member_type.value,
                "roles": roles_by_member.get(m.id, []),
                "branchId": str(m.branch_id),
                "joinedAt": _iso(m.joined_at),
            }
            for m in members
        ],
    }
    return {
        "success": True,
        "message": "Church data backup generated successfully",
        "backup": backup_payload,
    }


@router.get("/admins")
async def list_church_admins(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
    search: str = Query(""),
):
    actor = await authorize(
        session, current_user, church_id_from(request, organizationId), "member.read"
    )
    church_id = actor.church_id
    effective_roles, by_upper, _ = await _get_effective_church_roles(session, church_id)
    perms_by_role = await _permissions_by_role_ids(
        session, [str(r.id) for r in effective_roles]
    )

    # Which role names count as administrative / leadership roles?
    # Built-in leadership roles + any role (custom or overridden) that has >= 1 permission
    admin_role_upper: set[str] = set(LEADERSHIP_ROLE_NAMES)
    for r in effective_roles:
        r_perms = perms_by_role.get(str(r.id), [])
        if r.church_id == church_id or len(r_perms) > 0:
            if r.name.upper() not in ("MEMBER", "VISITOR"):
                admin_role_upper.add(r.name.upper())

    conds = [
        Member.church_id == church_id,
        Member.deleted_at.is_(None),
        or_(
            Member.id.in_(
                select(MemberRoleAssignment.member_id)
                .join(Role, Role.id == MemberRoleAssignment.role_id)
                .where(func.upper(Role.name).in_(admin_role_upper))
            ),
            Member.id.in_(select(AdminDetails.member_id)),
        ),
    ]
    if search:
        conds.append(_search_filter(search))

    rows = (
        await session.execute(
            select(Member, User, Branch, AdminDetails)
            .outerjoin(User, Member.user_id == User.id)
            .outerjoin(Branch, Member.branch_id == Branch.id)
            .outerjoin(AdminDetails, AdminDetails.member_id == Member.id)
            .where(*conds)
            .order_by(Member.created_at.desc(), Member.id)
        )
    ).all()

    roles_by_member = await role_names_for_members(
        session, [m.id for m, _, _, _ in rows]
    )
    total_seeded_perms = len(PERMISSIONS)

    admins_out: List[Dict[str, Any]] = []
    for member, user, branch, admin_d in rows:
        m_roles = roles_by_member.get(member.id, [])
        # Pick primary leadership role first
        primary_role = next(
            (r for r in m_roles if r.upper() in ADMIN_ROLE_NAMES),
            next(
                (r for r in m_roles if r.upper() in admin_role_upper),
                m_roles[0] if m_roles else "ADMIN",
            ),
        )
        # Aggregate permissions across all roles held by this member
        perm_names_set: set[str] = set()
        perm_labels: List[str] = []
        has_full_access = any(r.upper() in ADMIN_ROLE_NAMES for r in m_roles)
        for rname in m_roles:
            eff_role = by_upper.get(rname.upper())
            if eff_role:
                for p in perms_by_role.get(str(eff_role.id), []):
                    if p.name not in perm_names_set:
                        perm_names_set.add(p.name)
                        perm_labels.append(p.description or p.name)
        if has_full_access or (
            total_seeded_perms > 0 and len(perm_names_set) >= total_seeded_perms
        ):
            display_perms = ["All Permissions"]
        elif perm_labels:
            display_perms = perm_labels
        else:
            display_perms = [
                PERMISSIONS.get(code, code)
                for code in DEFAULT_ROLE_PERMISSIONS.get(primary_role.upper(), [])
            ] or ["Basic Access"]

        is_active = member.status == MembershipStatus.ACTIVE and (
            user is None or user.status == AccountStatus.ACTIVE
        )
        admins_out.append(
            {
                "id": str(member.id),
                "memberId": str(member.id),
                "userId": str(user.id) if user else None,
                "name": member.full_name,
                "firstName": member.first_name,
                "lastName": member.last_name,
                "email": (user.email if user else member.email) or "",
                "phoneNumber": (
                    member.phone_number or (user.phone_number if user else "")
                )
                or "",
                "gender": member.gender.value.lower() if member.gender else "male",
                "avatar": member.photo_url
                or (user.profile_picture_url if user else None),
                "role": primary_role.upper(),
                "roleLabel": _format_role_display_name(primary_role),
                "roles": m_roles or [primary_role.upper()],
                "position": member.position
                or _format_role_display_name(primary_role),
                "branchId": str(member.branch_id),
                "branchName": branch.name if branch else "",
                "accessLevel": (admin_d.access_level if admin_d else "BRANCH")
                or "BRANCH",
                "status": "Active" if is_active else "Inactive",
                "memberStatus": member.status.value,
                "accountStatus": user.status.value if user else "NO_LOGIN",
                "hasLogin": user is not None,
                "lastLogin": _iso(user.last_login) if user and user.last_login else None,
                "permissions": display_perms,
                "permissionCodes": sorted(perm_names_set),
                "createdAt": _iso(member.created_at),
            }
        )

    return {"admins": admins_out, "users": admins_out, "total": len(admins_out)}


@router.post("/admins")
async def create_or_promote_church_admin(
    payload: Dict[str, Any],
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session,
        current_user,
        church_id_from(request, organizationId or payload.get("organizationId")),
    )
    church_id = actor.church_id
    await require(session, actor, "role.assign")

    # Ensure permissions and system roles exist
    await seed_permissions(session)

    # Resolve branch: use payload.branchId / primaryBranchId / branchIds[0] or first branch in church
    branch_id = (
        payload.get("branchId")
        or payload.get("primaryBranchId")
        or (
            payload.get("branchIds")[0]
            if isinstance(payload.get("branchIds"), list) and payload.get("branchIds")
            else None
        )
    )
    if branch_id:
        branch = await _branch_in_church(session, branch_id, church_id)
    else:
        branch = (
            await session.execute(
                select(Branch)
                .where(Branch.church_id == church_id, Branch.deleted_at.is_(None))
                .order_by(Branch.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not branch:
            raise HTTPException(
                status_code=400,
                detail="Please create at least one branch before adding an administrator",
            )

    requested_role = (payload.get("role") or "ADMIN").strip().upper()
    role_names_to_assign = [requested_role]
    if payload.get("isMember", True) and "MEMBER" not in role_names_to_assign:
        role_names_to_assign.append("MEMBER")
    if payload.get("isStaff") and "STAFF" not in role_names_to_assign:
        role_names_to_assign.append("STAFF")
    if payload.get("isVolunteer") and "VOLUNTEER" not in role_names_to_assign:
        role_names_to_assign.append("VOLUNTEER")

    roles = await resolve_roles(session, church_id, role_names_to_assign)
    password = (payload.get("password") or "User@123").strip()
    access_level = (payload.get("accessLevel") or "BRANCH").strip().upper()

    # Check if promoting an existing member by memberId or matching email in this church
    existing_member: Optional[Member] = None
    if payload.get("memberId"):
        existing_member, _, _ = await _member_in_church(
            session, str(payload["memberId"]), church_id
        )
    elif payload.get("email"):
        email_clean = str(payload["email"]).strip().lower()
        existing_member = (
            await session.execute(
                select(Member).where(
                    Member.church_id == church_id,
                    func.lower(Member.email) == email_clean,
                    Member.deleted_at.is_(None),
                )
            )
        ).scalars().first()

    if existing_member:
        member = existing_member
        user = await session.get(User, member.user_id) if member.user_id else None
        email_clean = (
            str(payload.get("email") or member.email or "").strip().lower()
        )
        if not user and email_clean:
            user = (
                await session.execute(
                    select(User).where(func.lower(User.email) == email_clean)
                )
            ).scalar_one_or_none()
            if not user:
                user = User(
                    email=email_clean,
                    display_name=member.full_name,
                    phone_number=member.phone_number,
                    hashed_password=get_password_hash(password),
                    status=AccountStatus.ACTIVE,
                )
                session.add(user)
                await session.flush()
            member.user_id = user.id
        if payload.get("firstName"):
            member.first_name = str(payload["firstName"]).strip()
        if payload.get("lastName"):
            member.last_name = str(payload["lastName"]).strip()
        if payload.get("phoneNumber"):
            member.phone_number = str(payload["phoneNumber"]).strip()
        if branch_id:
            member.branch_id = branch.id
        member.position = _format_role_display_name(requested_role)
        await set_member_roles(session, member.id, roles, replace=False)
        await _upsert_detail(
            session,
            AdminDetails,
            member.id,
            {"access_level": access_level},
            {"access_level": access_level},
        )
        await session.commit()
        return {
            "success": True,
            "message": "Administrator role assigned successfully",
            "id": str(member.id),
        }

    # Creating a brand-new member + user account
    await require(session, actor, "member.create", branch.id)
    email = str(payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    first_name = str(payload.get("firstName") or "").strip()
    last_name = str(payload.get("lastName") or "").strip()
    if not first_name or not last_name:
        raise HTTPException(
            status_code=400, detail="First name and last name are required"
        )
    phone_number = str(payload.get("phoneNumber") or "").strip() or None
    gender_raw = str(payload.get("gender") or "MALE").strip().upper()
    gender = Gender.FEMALE if gender_raw == "FEMALE" else Gender.MALE

    user = (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if user:
        prior = (
            await session.execute(
                select(Member).where(
                    Member.church_id == church_id, Member.user_id == user.id
                )
            )
        ).scalars().first()
        if prior and prior.deleted_at is None:
            raise HTTPException(
                status_code=409,
                detail="A member with this email already exists in this church",
            )
    else:
        if phone_number:
            phone_conflict = (
                await session.execute(
                    select(User.id).where(User.phone_number == phone_number)
                )
            ).first()
            if phone_conflict:
                raise HTTPException(
                    status_code=409,
                    detail="A user with this phone number already exists",
                )
        user = User(
            email=email,
            display_name=f"{first_name} {last_name}".strip(),
            phone_number=phone_number,
            hashed_password=get_password_hash(password),
            status=AccountStatus.ACTIVE,
        )
        session.add(user)
        await session.flush()

    position = _format_role_display_name(requested_role)
    number = await generate_unique_membership_number(
        session=session,
        church_id=church_id,
        roles=[r.name for r in roles],
        position=position,
        member_type=MemberType.MEMBER,
    )
    member = Member(
        church_id=church_id,
        branch_id=branch.id,
        user_id=user.id,
        member_type=MemberType.MEMBER,
        first_name=first_name,
        last_name=last_name,
        gender=gender,
        phone_number=phone_number,
        email=email,
        position=position,
        membership_number=number,
        status=MembershipStatus.ACTIVE,
    )
    session.add(member)
    await session.flush()
    await set_member_roles(session, member.id, roles, replace=True)

    if isinstance(payload.get("address"), dict):
        ad = payload["address"]
        addr_input = AddressInput(
            street=ad.get("street") or "",
            city=ad.get("city") or "",
            state=ad.get("state"),
            zip_code=ad.get("zipCode") or ad.get("zip_code"),
            country=ad.get("country") or "Kenya",
        )
        await _apply_address(session, member, addr_input)

    await _upsert_detail(
        session, MemberDetails, member.id, {}, {"membership_date": _now()}
    )
    await _upsert_detail(
        session,
        AdminDetails,
        member.id,
        {"access_level": access_level},
        {"access_level": access_level},
    )
    if any("STAFF" in r.name.upper() for r in roles):
        await _upsert_detail(
            session,
            StaffDetails,
            member.id,
            {},
            {"job_title": position, "department": "Administration", "start_date": _now()},
        )
    if any("VOLUNTEER" in r.name.upper() for r in roles):
        await _upsert_detail(session, VolunteerDetails, member.id, {})

    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "CREATE_CHURCH_ADMIN",
        "MEMBER",
        member.id,
        actor_user_id=current_user.id,
        church_id=church_id,
        metadata_json={"role": requested_role, "accessLevel": access_level},
    )
    return {
        "success": True,
        "message": "Administrator created successfully",
        "id": str(member.id),
        "userId": str(user.id),
    }


@router.put("/admins/{member_id}")
@router.patch("/admins/{member_id}")
async def update_church_admin(
    member_id: str,
    payload: Dict[str, Any],
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church_id = actor.church_id
    member, user, _ = await _member_in_church(session, member_id, church_id)
    await require(session, actor, "member.update", member.branch_id)

    if "firstName" in payload and payload["firstName"]:
        member.first_name = str(payload["firstName"]).strip()
    if "lastName" in payload and payload["lastName"]:
        member.last_name = str(payload["lastName"]).strip()
    if "email" in payload and payload["email"]:
        new_email = str(payload["email"]).strip().lower()
        member.email = new_email
        if user:
            other_memberships = (
                await session.execute(
                    select(func.count(Member.id)).where(
                        Member.user_id == user.id,
                        Member.church_id != church_id,
                        Member.deleted_at.is_(None),
                    )
                )
            ).scalar() or 0
            if other_memberships == 0:
                user.email = new_email
    if "phoneNumber" in payload:
        member.phone_number = str(payload["phoneNumber"] or "").strip() or None
    if "branchId" in payload and payload["branchId"]:
        new_branch = await _branch_in_church(session, str(payload["branchId"]), church_id)
        member.branch_id = new_branch.id

    if "status" in payload and payload["status"]:
        st_raw = str(payload["status"]).strip().upper()
        if st_raw in ("ACTIVE",):
            member.status = MembershipStatus.ACTIVE
            if user:
                user.status = AccountStatus.ACTIVE
        elif st_raw in ("INACTIVE", "SUSPENDED"):
            member.status = MembershipStatus.INACTIVE
            if user:
                user.status = (
                    AccountStatus.SUSPENDED
                    if st_raw == "SUSPENDED"
                    else AccountStatus.INACTIVE
                )

    if "accessLevel" in payload and payload["accessLevel"]:
        await _upsert_detail(
            session,
            AdminDetails,
            member.id,
            {"access_level": str(payload["accessLevel"]).strip().upper()},
            {"access_level": "BRANCH"},
        )

    if "role" in payload or "roles" in payload:
        await require(session, actor, "role.assign")
        raw_roles = (
            payload.get("roles")
            if isinstance(payload.get("roles"), list)
            else [payload.get("role")]
        )
        wanted_names = [str(r).strip().upper() for r in raw_roles if r]
        if wanted_names:
            if "MEMBER" not in wanted_names:
                wanted_names.append("MEMBER")
            resolved = await resolve_roles(session, church_id, wanted_names)
            await set_member_roles(session, member.id, resolved, replace=True)
            member.position = _format_role_display_name(wanted_names[0])

    if user:
        user.display_name = member.full_name

    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "UPDATE_CHURCH_ADMIN",
        "MEMBER",
        member.id,
        actor_user_id=current_user.id,
        church_id=church_id,
    )
    return {
        "success": True,
        "message": "Administrator updated successfully",
        "id": str(member.id),
    }


@router.post("/admins/{member_id}/reset-password")
async def reset_church_admin_password(
    member_id: str,
    payload: Dict[str, Any],
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church_id = actor.church_id
    await require(session, actor, "role.assign")
    member, user, _ = await _member_in_church(session, member_id, church_id)

    new_password = str(payload.get("password") or "").strip()
    if len(new_password) < 6:
        raise HTTPException(
            status_code=400,
            detail="New password must be at least 6 characters long",
        )

    if user is None:
        email = (member.email or "").strip().lower()
        if not email:
            raise HTTPException(
                status_code=400,
                detail="This member does not have an email address to create a login account",
            )
        user = (
            await session.execute(
                select(User).where(func.lower(User.email) == email)
            )
        ).scalar_one_or_none()
        if user is None:
            user = User(
                email=email,
                display_name=member.full_name,
                phone_number=member.phone_number,
                hashed_password=get_password_hash(new_password),
                status=AccountStatus.ACTIVE,
            )
            session.add(user)
            await session.flush()
        else:
            user.hashed_password = get_password_hash(new_password)
        member.user_id = user.id
    else:
        user.hashed_password = get_password_hash(new_password)
        if user.status != AccountStatus.ACTIVE:
            user.status = AccountStatus.ACTIVE

    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "RESET_ADMIN_PASSWORD",
        "MEMBER",
        member.id,
        actor_user_id=current_user.id,
        church_id=church_id,
    )
    return {
        "success": True,
        "message": f"Password for {member.full_name} has been reset successfully",
    }


@router.delete("/admins/{member_id}")
async def delete_or_demote_church_admin(
    member_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    mode: str = Query(
        "demote",
        description="'demote' revokes admin roles & keeps member; 'remove' soft-deletes member from church",
    ),
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church_id = actor.church_id
    await require(session, actor, "role.assign")
    member, _, _ = await _member_in_church(session, member_id, church_id)

    if actor.member and member.id == actor.member.id:
        raise HTTPException(
            status_code=400,
            detail="You cannot remove or demote your own administrator account",
        )

    await session.execute(
        delete(AdminDetails).where(AdminDetails.member_id == member.id)
    )

    if mode.lower() == "remove":
        await require(session, actor, "member.delete", member.branch_id)
        await session.execute(
            delete(MemberRoleAssignment).where(
                MemberRoleAssignment.member_id == member.id
            )
        )
        member.soft_delete()
        action_msg = f"{member.full_name} has been removed from the church"
    else:
        member_role = await resolve_roles(session, church_id, ["MEMBER"])
        await set_member_roles(session, member.id, member_role, replace=True)
        member.position = "Member"
        action_msg = f"{member.full_name} has been demoted to regular Member"

    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "REMOVE_CHURCH_ADMIN",
        "MEMBER",
        member.id,
        actor_user_id=current_user.id,
        church_id=church_id,
        metadata_json={"mode": mode.lower()},
    )
    return {"success": True, "message": action_msg}


@router.get("/permissions")
async def list_church_permissions(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    await seed_permissions(session)
    rows = (
        await session.execute(select(Permission).order_by(Permission.name))
    ).scalars().all()
    items = [
        {
            "id": str(p.id),
            "name": p.name,
            "code": p.name,
            "description": p.description or PERMISSIONS.get(p.name, p.name),
            "label": p.description or PERMISSIONS.get(p.name, p.name),
            "category": _permission_category(p.name),
        }
        for p in rows
    ]
    return {"permissions": items}


@router.get("/roles")
async def list_church_roles(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church_id = actor.church_id
    effective_roles, _, _ = await _get_effective_church_roles(session, church_id)
    perms_by_role = await _permissions_by_role_ids(
        session, [str(r.id) for r in effective_roles]
    )

    # Fetch all active members in this church and their role assignments
    assignment_rows = (
        await session.execute(
            select(
                Role.name,
                Member.id,
                Member.first_name,
                Member.last_name,
                Member.email,
                Branch.name,
            )
            .join(MemberRoleAssignment, MemberRoleAssignment.role_id == Role.id)
            .join(Member, Member.id == MemberRoleAssignment.member_id)
            .outerjoin(Branch, Branch.id == Member.branch_id)
            .where(Member.church_id == church_id, Member.deleted_at.is_(None))
            .order_by(Member.first_name, Member.last_name)
        )
    ).all()

    members_by_role_upper: Dict[str, List[Dict[str, Any]]] = {}
    seen_by_role_upper: Dict[str, set[str]] = {}
    for rname, mid, fname, lname, memail, bname in assignment_rows:
        key = rname.upper()
        seen = seen_by_role_upper.setdefault(key, set())
        if str(mid) not in seen:
            seen.add(str(mid))
            members_by_role_upper.setdefault(key, []).append(
                {
                    "id": str(mid),
                    "name": f"{fname} {lname}".strip(),
                    "email": memail or "",
                    "branchName": bname or "",
                }
            )

    total_seeded_perms = len(PERMISSIONS)
    roles_out: List[Dict[str, Any]] = []
    for r in effective_roles:
        upper = r.name.upper()
        r_perms = perms_by_role.get(str(r.id), [])
        perm_codes = [p.name for p in r_perms]
        perm_labels = [p.description or p.name for p in r_perms]
        is_locked = upper in ADMIN_ROLE_NAMES
        if is_locked or (
            total_seeded_perms > 0 and len(perm_codes) >= total_seeded_perms
        ):
            display_perms = ["All Permissions"]
        else:
            display_perms = perm_labels

        assigned_list = members_by_role_upper.get(upper, [])
        roles_out.append(
            {
                "id": str(r.id),
                "name": r.name,
                "displayName": _format_role_display_name(r.name),
                "description": r.description
                or ROLE_DESCRIPTIONS.get(upper, "Custom church role"),
                "churchId": str(r.church_id) if r.church_id else None,
                "isSystem": bool(r.is_system and r.church_id is None),
                "isBuiltIn": upper in ROLE_DESCRIPTIONS,
                "isCustomized": bool(r.church_id == church_id and upper in ROLE_DESCRIPTIONS),
                "isLocked": is_locked,
                "userCount": len(assigned_list),
                "assignedMembers": assigned_list,
                "permissions": display_perms,
                "permissionCodes": perm_codes,
                "permissionLabels": perm_labels,
            }
        )

    return {"roles": roles_out}


async def _set_role_permissions(
    session: DbSession, role_id: str, permission_codes: List[str]
) -> None:
    await session.execute(
        delete(role_permission).where(role_permission.c.role_id == role_id)
    )
    clean_codes = {c.strip() for c in permission_codes if c and c.strip()}
    if not clean_codes:
        return
    perms = (
        await session.execute(
            select(Permission).where(
                or_(
                    Permission.name.in_(clean_codes),
                    Permission.description.in_(clean_codes),
                )
            )
        )
    ).scalars().all()
    for p in perms:
        await session.execute(
            insert(role_permission).values(role_id=role_id, permission_id=p.id)
        )


@router.post("/roles")
async def create_church_role(
    payload: Dict[str, Any],
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church_id = actor.church_id
    await require(session, actor, "role.assign")
    await seed_permissions(session)

    raw_name = str(payload.get("name") or "").strip()
    if not raw_name:
        raise HTTPException(status_code=400, detail="Role name is required")
    normalized_name = "_".join(raw_name.upper().split())
    description = str(payload.get("description") or "").strip() or None
    perm_codes = payload.get("permissions") or payload.get("permissionCodes") or []

    existing = (
        await session.execute(
            select(Role).where(
                Role.church_id == church_id,
                func.upper(Role.name) == normalized_name,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"A role named '{raw_name}' already exists in this church",
        )

    new_role = Role(
        name=normalized_name,
        description=description,
        church_id=church_id,
        is_system=False,
    )
    session.add(new_role)
    await session.flush()

    await _set_role_permissions(session, str(new_role.id), perm_codes)

    # If overriding a system role name, migrate this church's member assignments to the new role row
    sys_role = (
        await session.execute(
            select(Role).where(
                Role.church_id.is_(None),
                func.upper(Role.name) == normalized_name,
            )
        )
    ).scalar_one_or_none()
    if sys_role:
        church_member_ids = select(Member.id).where(Member.church_id == church_id)
        assignments = (
            await session.execute(
                select(MemberRoleAssignment).where(
                    MemberRoleAssignment.role_id == sys_role.id,
                    MemberRoleAssignment.member_id.in_(church_member_ids),
                )
            )
        ).scalars().all()
        for a in assignments:
            a.role_id = new_role.id

    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "CREATE_ROLE",
        "ROLE",
        new_role.id,
        actor_user_id=current_user.id,
        church_id=church_id,
        metadata_json={"name": normalized_name},
    )
    return {
        "success": True,
        "message": "Role created successfully",
        "id": str(new_role.id),
        "name": new_role.name,
    }


@router.put("/roles/{role_id}")
@router.patch("/roles/{role_id}")
async def update_church_role(
    role_id: str,
    payload: Dict[str, Any],
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church_id = actor.church_id
    await require(session, actor, "role.assign")
    await seed_permissions(session)

    target = await session.get(Role, role_id)
    if not target or (target.church_id is not None and target.church_id != church_id):
        raise HTTPException(status_code=404, detail="Role not found")

    upper = target.name.upper()
    perm_codes = (
        payload.get("permissionCodes")
        if "permissionCodes" in payload
        else payload.get("permissions")
    )
    if upper in ADMIN_ROLE_NAMES:
        # OWNER and ADMIN always retain all permissions
        perm_codes = list(PERMISSIONS.keys())

    new_desc = (
        str(payload.get("description")).strip()
        if "description" in payload and payload.get("description") is not None
        else target.description
    )

    if target.church_id is None:
        # Target is a global system role: create or update a church-scoped override role
        override = (
            await session.execute(
                select(Role).where(
                    Role.church_id == church_id,
                    func.upper(Role.name) == upper,
                )
            )
        ).scalar_one_or_none()
        if override is None:
            override = Role(
                name=target.name,
                description=new_desc,
                church_id=church_id,
                is_system=False,
            )
            session.add(override)
            await session.flush()
        else:
            override.description = new_desc

        if perm_codes is not None:
            await _set_role_permissions(session, str(override.id), perm_codes)

        # Migrate this church's MemberRoleAssignment rows from system role to church override role
        church_member_ids = select(Member.id).where(Member.church_id == church_id)
        assignments = (
            await session.execute(
                select(MemberRoleAssignment).where(
                    MemberRoleAssignment.role_id == target.id,
                    MemberRoleAssignment.member_id.in_(church_member_ids),
                )
            )
        ).scalars().all()
        for a in assignments:
            a.role_id = override.id
        active_role = override
    else:
        # Target is already church-scoped
        if "name" in payload and payload["name"] and upper not in ROLE_DESCRIPTIONS:
            normalized_name = "_".join(str(payload["name"]).strip().upper().split())
            if normalized_name:
                target.name = normalized_name
        target.description = new_desc
        if perm_codes is not None:
            await _set_role_permissions(session, str(target.id), perm_codes)
        active_role = target

    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "UPDATE_ROLE",
        "ROLE",
        active_role.id,
        actor_user_id=current_user.id,
        church_id=church_id,
        metadata_json={"name": active_role.name},
    )
    return {
        "success": True,
        "message": "Role updated successfully",
        "id": str(active_role.id),
        "name": active_role.name,
    }


@router.delete("/roles/{role_id}")
async def delete_church_role(
    role_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church_id = actor.church_id
    await require(session, actor, "role.assign")

    target = await session.get(Role, role_id)
    if not target or target.church_id != church_id:
        raise HTTPException(
            status_code=400,
            detail="Built-in system default roles cannot be deleted",
        )

    upper = target.name.upper()
    sys_role = (
        await session.execute(
            select(Role).where(
                Role.church_id.is_(None),
                func.upper(Role.name) == upper,
            )
        )
    ).scalar_one_or_none()

    if sys_role:
        # Revert church's members back to the system role
        assignments = (
            await session.execute(
                select(MemberRoleAssignment).where(
                    MemberRoleAssignment.role_id == target.id
                )
            )
        ).scalars().all()
        for a in assignments:
            a.role_id = sys_role.id
        await session.flush()
        await session.execute(
            delete(role_permission).where(role_permission.c.role_id == target.id)
        )
        await session.execute(delete(Role).where(Role.id == target.id))
        await session.commit()
        return {
            "success": True,
            "message": f"Role '{_format_role_display_name(target.name)}' reset to system defaults",
        }

    # Custom role deletion: ensure affected members still have at least MEMBER role
    affected_member_ids = (
        await session.execute(
            select(MemberRoleAssignment.member_id).where(
                MemberRoleAssignment.role_id == target.id
            )
        )
    ).scalars().all()
    await session.execute(
        delete(MemberRoleAssignment).where(
            MemberRoleAssignment.role_id == target.id
        )
    )
    await session.execute(
        delete(role_permission).where(role_permission.c.role_id == target.id)
    )
    await session.execute(delete(Role).where(Role.id == target.id))
    await session.flush()

    if affected_member_ids:
        member_role = await resolve_roles(session, church_id, ["MEMBER"])
        for mid in set(affected_member_ids):
            remaining = (
                await session.execute(
                    select(func.count(MemberRoleAssignment.id)).where(
                        MemberRoleAssignment.member_id == mid
                    )
                )
            ).scalar() or 0
            if remaining == 0:
                await set_member_roles(session, mid, member_role, replace=False)

    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "DELETE_ROLE",
        "ROLE",
        role_id,
        actor_user_id=current_user.id,
        church_id=church_id,
        metadata_json={"name": upper},
    )
    return {"success": True, "message": "Custom role deleted successfully"}


@router.post("/roles/{role_id}/assign")
async def assign_church_role_to_members(
    role_id: str,
    payload: Dict[str, Any],
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church_id = actor.church_id
    await require(session, actor, "role.assign")

    target = await session.get(Role, role_id)
    if not target or (target.church_id is not None and target.church_id != church_id):
        raise HTTPException(status_code=404, detail="Role not found")

    # Resolve the effective role for this church
    resolved = await resolve_roles(session, church_id, [target.name])
    eff_role = resolved[0]

    member_ids: List[str] = [
        str(m).strip()
        for m in (payload.get("memberIds") or [])
        if m and str(m).strip()
    ]
    if payload.get("memberId"):
        member_ids.append(str(payload["memberId"]).strip())
    await _check_members_in_church(session, member_ids, church_id)

    mode = str(payload.get("mode") or "set").lower()
    # Find all role IDs matching this role's name (system + church override)
    matching_role_ids = (
        await session.execute(
            select(Role.id).where(
                or_(Role.church_id == church_id, Role.church_id.is_(None)),
                func.upper(Role.name) == eff_role.name.upper(),
            )
        )
    ).scalars().all()

    church_member_ids_subq = select(Member.id).where(Member.church_id == church_id)

    if mode == "set":
        # Replace the set of members in this church who hold this role
        existing_assignments = (
            await session.execute(
                select(MemberRoleAssignment).where(
                    MemberRoleAssignment.role_id.in_(matching_role_ids),
                    MemberRoleAssignment.member_id.in_(church_member_ids_subq),
                )
            )
        ).scalars().all()
        currently_assigned = {str(a.member_id) for a in existing_assignments}
        wanted_set = set(member_ids)

        # Prevent actor from removing their own ADMIN/OWNER role
        if (
            eff_role.name.upper() in ADMIN_ROLE_NAMES
            and actor.member
            and str(actor.member.id) in (currently_assigned - wanted_set)
        ):
            raise HTTPException(
                status_code=400,
                detail="You cannot unassign your own administrator role",
            )

        to_remove = currently_assigned - wanted_set
        if to_remove:
            await session.execute(
                delete(MemberRoleAssignment).where(
                    MemberRoleAssignment.role_id.in_(matching_role_ids),
                    MemberRoleAssignment.member_id.in_(to_remove),
                )
            )
            await session.flush()
            # Ensure anyone who lost their last role still has MEMBER
            member_fallback = await resolve_roles(session, church_id, ["MEMBER"])
            for mid in to_remove:
                rem = (
                    await session.execute(
                        select(func.count(MemberRoleAssignment.id)).where(
                            MemberRoleAssignment.member_id == mid
                        )
                    )
                ).scalar() or 0
                if rem == 0:
                    await set_member_roles(session, mid, member_fallback, replace=False)

        for mid in wanted_set - currently_assigned:
            await set_member_roles(session, mid, [eff_role], replace=False)
            if eff_role.name.upper() in ADMIN_ROLE_NAMES:
                await _upsert_detail(
                    session, AdminDetails, mid, {}, {"access_level": "BRANCH"}
                )
    elif mode == "remove":
        if member_ids:
            await session.execute(
                delete(MemberRoleAssignment).where(
                    MemberRoleAssignment.role_id.in_(matching_role_ids),
                    MemberRoleAssignment.member_id.in_(member_ids),
                )
            )
    else:
        # mode == "add"
        for mid in set(member_ids):
            await set_member_roles(session, mid, [eff_role], replace=False)
            if eff_role.name.upper() in ADMIN_ROLE_NAMES:
                await _upsert_detail(
                    session, AdminDetails, mid, {}, {"access_level": "BRANCH"}
                )

    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "ASSIGN_ROLE",
        "ROLE",
        eff_role.id,
        actor_user_id=current_user.id,
        church_id=church_id,
        metadata_json={"role": eff_role.name, "mode": mode, "count": len(member_ids)},
    )
    return {
        "success": True,
        "message": f"Role '{_format_role_display_name(eff_role.name)}' assignments updated",
    }


def _relative_time_label(dt: Optional[datetime], now: datetime) -> str:
    if not dt:
        return "Recently"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff_sec = max(0, int((now - dt).total_seconds()))
    if diff_sec < 60:
        return "Just now"
    if diff_sec < 3600:
        return f"{diff_sec // 60}m ago"
    if diff_sec < 86400:
        return f"{diff_sec // 3600}h ago"
    days = diff_sec // 86400
    if days < 30:
        return f"{days}d ago"
    return dt.strftime("%b %d")


def _format_pct_change(curr: float, prev: float) -> Tuple[str, str]:
    if prev > 0:
        pct = round(((curr - prev) / prev) * 100)
        return (f"+{pct}%" if pct >= 0 else f"{pct}%"), ("up" if pct >= 0 else "down")
    if curr > 0:
        return "+100%", "up"
    return "0%", "up"


@router.get("/dashboard")
async def get_church_dashboard_overview(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church_id = actor.church_id
    church = await session.get(Church, church_id)
    now = _now()
    thirty_days_ago = now - timedelta(days=30)
    sixty_days_ago = now - timedelta(days=60)
    seven_days_ago = now - timedelta(days=7)
    fourteen_days_ago = now - timedelta(days=14)

    # 1. Members & New Members
    members = (
        await session.execute(
            select(Member)
            .where(Member.church_id == church_id, Member.deleted_at.is_(None))
            .order_by(Member.created_at.desc())
        )
    ).scalars().all()
    total_members = len(members)
    new_members_30d = 0
    new_members_prev_30d = 0
    for m in members:
        c_dt = m.created_at
        if c_dt:
            if c_dt.tzinfo is None:
                c_dt = c_dt.replace(tzinfo=timezone.utc)
            if c_dt >= thirty_days_ago:
                new_members_30d += 1
            elif c_dt >= sixty_days_ago:
                new_members_prev_30d += 1
    member_change, member_trend = _format_pct_change(
        float(new_members_30d), float(new_members_prev_30d)
    )

    # 2. Events & Attendance
    events = (
        await session.execute(
            select(Event)
            .where(Event.church_id == church_id)
            .order_by(Event.start_time.asc())
        )
    ).scalars().all()
    active_events = [
        ev for ev in events if ev.status == EventStatus.ACTIVE
    ]
    new_events_30d = sum(
        1
        for ev in active_events
        if ev.created_at
        and (
            ev.created_at.replace(tzinfo=timezone.utc)
            if ev.created_at.tzinfo is None
            else ev.created_at
        )
        >= thirty_days_ago
    )

    att_rows = (
        await session.execute(
            select(Attendance, Event)
            .join(Event, Event.id == Attendance.event_id)
            .where(Event.church_id == church_id)
            .order_by(Attendance.checked_in_at.desc())
        )
    ).all()
    att_by_event: Dict[str, int] = {}
    att_last_7d = 0
    att_prev_7d = 0
    # 4 weekly buckets: Week 1 (21-28d ago), Week 2 (14-21d ago), Week 3 (7-14d ago), Week 4 (0-7d ago)
    week_counts = [0, 0, 0, 0]
    for att, ev in att_rows:
        eid = str(ev.id)
        att_by_event[eid] = att_by_event.get(eid, 0) + 1
        dt = att.checked_in_at or ev.start_time or att.created_at
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= seven_days_ago:
                att_last_7d += 1
                week_counts[3] += 1
            elif dt >= fourteen_days_ago:
                att_prev_7d += 1
                week_counts[2] += 1
            elif dt >= now - timedelta(days=21):
                week_counts[1] += 1
            elif dt >= now - timedelta(days=28):
                week_counts[0] += 1

    weekly_attendance_val = att_last_7d if att_last_7d > 0 else len(att_rows)
    att_change, att_trend = _format_pct_change(
        float(att_last_7d), float(att_prev_7d)
    )

    # 3. Offerings & Giving
    offering_rows = (
        await session.execute(
            select(Offering, Fund)
            .outerjoin(Fund, Fund.id == Offering.fund_id)
            .where(
                Offering.church_id == church_id,
                Offering.deleted_at.is_(None),
            )
            .order_by(Offering.received_at.desc())
        )
    ).all()
    giving_30d = 0.0
    giving_prev_30d = 0.0
    total_giving_all = 0.0
    for o, _ in offering_rows:
        amt = float(o.amount or 0)
        total_giving_all += amt
        dt = o.received_at or o.created_at
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= thirty_days_ago:
                giving_30d += amt
            elif dt >= sixty_days_ago:
                giving_prev_30d += amt

    monthly_giving_val = round(
        giving_30d if giving_30d > 0 else total_giving_all, 2
    )
    giving_change, giving_trend = _format_pct_change(giving_30d, giving_prev_30d)

    # 4. Prayer Requests & Assets for Recent Activity & Ministry Health
    prayers = (
        await session.execute(
            select(PrayerRequest)
            .where(PrayerRequest.church_id == church_id)
            .order_by(PrayerRequest.created_at.desc())
        )
    ).scalars().all()
    active_prayers = sum(
        1 for p in prayers if p.status == PrayerStatus.ACTIVE
    )
    answered_prayers = sum(
        1 for p in prayers if p.status == PrayerStatus.ANSWERED
    )

    assets = (
        await session.execute(
            select(Asset)
            .where(Asset.church_id == church_id)
            .order_by(Asset.created_at.desc())
        )
    ).scalars().all()

    # 5. Volunteer & Ministry Engagement
    dept_member_ids = set(
        (
            await session.execute(
                select(DepartmentMember.member_id)
                .join(Department, Department.id == DepartmentMember.department_id)
                .where(Department.church_id == church_id)
            )
        )
        .scalars()
        .all()
    )
    group_member_ids = set(
        (
            await session.execute(
                select(GroupMember.member_id)
                .join(Group, Group.id == GroupMember.group_id)
                .where(Group.church_id == church_id)
            )
        )
        .scalars()
        .all()
    )
    vol_member_ids = set(
        (
            await session.execute(
                select(VolunteerDetails.member_id)
                .join(Member, Member.id == VolunteerDetails.member_id)
                .where(Member.church_id == church_id, Member.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    staff_role_member_ids = set(
        (
            await session.execute(
                select(MemberRoleAssignment.member_id)
                .join(Role, Role.id == MemberRoleAssignment.role_id)
                .join(Member, Member.id == MemberRoleAssignment.member_id)
                .where(
                    Member.church_id == church_id,
                    Member.deleted_at.is_(None),
                    func.upper(Role.name).in_(STAFF_AND_LEADERSHIP_ROLES),
                )
            )
        )
        .scalars()
        .all()
    )
    engaged_member_ids = (
        dept_member_ids | group_member_ids | vol_member_ids | staff_role_member_ids
    )
    volunteer_engagement_pct = (
        min(100, round((len(engaged_member_ids) / total_members) * 100))
        if total_members > 0
        else 0
    )

    # Giving Goal (from active pledges or church metadata or dynamic benchmark)
    pledge_sum = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(Pledge.amount), 0)).where(
                    Pledge.church_id == church_id,
                    Pledge.deleted_at.is_(None),
                )
            )
        ).scalar()
        or 0
    )
    meta = dict((church.metadata_json if church else None) or {})
    giving_goal = float(
        meta.get("giving_goal")
        or (pledge_sum if pledge_sum > 0 else max(50000.0, monthly_giving_val))
    )
    giving_goal_pct = (
        min(100, round((monthly_giving_val / giving_goal) * 100))
        if giving_goal > 0
        else 0
    )

    # 6. Recent Activity Feed (chronological across modules)
    feed: List[Dict[str, Any]] = []
    for m in members[:4]:
        dt = m.created_at
        feed.append(
            {
                "id": f"mem-{m.id}",
                "type": "member",
                "title": "New member registered",
                "description": f"{m.full_name} joined the church",
                "timestamp": _iso(dt) or "",
                "time": _relative_time_label(dt, now),
                "color": "bg-green-500",
            }
        )
    for ev in sorted(
        events,
        key=lambda x: x.created_at or x.start_time or now,
        reverse=True,
    )[:4]:
        dt = ev.created_at or ev.start_time
        feed.append(
            {
                "id": f"evt-{ev.id}",
                "type": "event",
                "title": "Event scheduled",
                "description": f"{ev.name} ({ev.location or 'Main Sanctuary'})",
                "timestamp": _iso(dt) or "",
                "time": _relative_time_label(dt, now),
                "color": "bg-blue-500",
            }
        )
    for o, f in offering_rows[:4]:
        dt = o.received_at or o.created_at
        fname = f.name if f else "General Offering"
        feed.append(
            {
                "id": f"off-{o.id}",
                "type": "finance",
                "title": "Offering recorded",
                "description": f"{fname}: KES {float(o.amount or 0):,.0f}",
                "timestamp": _iso(dt) or "",
                "time": _relative_time_label(dt, now),
                "color": "bg-purple-500",
            }
        )
    for pr in prayers[:3]:
        dt = pr.created_at
        feed.append(
            {
                "id": f"pr-{pr.id}",
                "type": "prayer",
                "title": "Prayer request submitted",
                "description": pr.title,
                "timestamp": _iso(dt) or "",
                "time": _relative_time_label(dt, now),
                "color": "bg-orange-500",
            }
        )
    for a in assets[:3]:
        dt = a.created_at
        feed.append(
            {
                "id": f"ast-{a.id}",
                "type": "asset",
                "title": "Church asset registered",
                "description": f"{a.name} ({a.condition.value.title()})",
                "timestamp": _iso(dt) or "",
                "time": _relative_time_label(dt, now),
                "color": "bg-red-500",
            }
        )
    feed.sort(key=lambda x: x.get("timestamp") or "", reverse=True)

    # 7. Upcoming Events list
    events_meta = dict(meta.get("events_meta") or {})
    upcoming_candidates = [
        ev
        for ev in active_events
        if ev.start_time
        and (
            ev.start_time.replace(tzinfo=timezone.utc)
            if ev.start_time.tzinfo is None
            else ev.start_time
        )
        >= (now - timedelta(days=1))
    ]
    if not upcoming_candidates:
        upcoming_candidates = active_events[:6]
    upcoming_out = []
    for ev in upcoming_candidates[:6]:
        em = dict(events_meta.get(str(ev.id)) or {})
        st = ev.start_time
        if st:
            st_utc = st.replace(tzinfo=timezone.utc) if st.tzinfo is None else st
            day_diff = (st_utc.date() - now.date()).days
            if day_diff == 0:
                date_label = "Today"
            elif day_diff == 1:
                date_label = "Tomorrow"
            elif 1 < day_diff < 7:
                date_label = st_utc.strftime("%A")
            else:
                date_label = st_utc.strftime("%b %d, %Y")
            time_label = st_utc.strftime("%I:%M %p").lstrip("0")
        else:
            date_label = "Scheduled"
            time_label = "9:00 AM"
        att_cnt = att_by_event.get(str(ev.id), 0)
        exp_att = int(em.get("expectedAttendees") or max(att_cnt, 50))
        upcoming_out.append(
            {
                "id": str(ev.id),
                "title": ev.name,
                "location": ev.location or "Main Sanctuary",
                "time": time_label,
                "date": date_label,
                "attendees": exp_att,
                "actualAttendees": att_cnt,
                "category": (em.get("type") or "service").lower(),
            }
        )

    # 8. Attendance Trends (Week 1 - Week 4)
    target_cap = max(1, total_members, max(week_counts) if any(week_counts) else 1)
    attendance_trends = [
        {
            "week": f"Week {i + 1}",
            "attendees": cnt,
            "percentage": min(100, round((cnt / target_cap) * 100)) if cnt > 0 else 0,
        }
        for i, cnt in enumerate(week_counts)
    ]

    # 9. Ministry Health
    new_members_target = max(10, round(total_members * 0.2))
    new_members_pct = min(
        100, round((new_members_30d / max(1, new_members_target)) * 100)
    )
    prayer_pct = (
        min(
            100,
            round(
                ((answered_prayers if answered_prayers > 0 else active_prayers) / len(prayers))
                * 100
            ),
        )
        if prayers
        else 0
    )

    return {
        "churchName": church.name if church else "",
        "stats": {
            "totalMembers": {
                "value": total_members,
                "change": member_change,
                "trend": member_trend,
                "newThisMonth": new_members_30d,
            },
            "weeklyAttendance": {
                "value": weekly_attendance_val,
                "change": att_change,
                "trend": att_trend,
            },
            "monthlyGiving": {
                "value": monthly_giving_val,
                "totalGiving": round(total_giving_all, 2),
                "change": giving_change,
                "trend": giving_trend,
            },
            "activeEvents": {
                "value": len(active_events),
                "change": f"+{new_events_30d}",
                "trend": "up",
            },
        },
        "recentActivities": feed[:8],
        "upcomingEvents": upcoming_out,
        "attendanceTrends": attendance_trends,
        "ministryHealth": {
            "newMembers": {
                "count": new_members_30d,
                "target": new_members_target,
                "label": f"{new_members_30d} this month",
                "percentage": new_members_pct,
            },
            "volunteerEngagement": {
                "engagedCount": len(engaged_member_ids),
                "totalMembers": total_members,
                "label": f"{volunteer_engagement_pct}%",
                "percentage": volunteer_engagement_pct,
            },
            "givingGoal": {
                "amount": monthly_giving_val,
                "goal": giving_goal,
                "label": f"KES {monthly_giving_val:,.0f} / KES {giving_goal:,.0f}",
                "percentage": giving_goal_pct,
            },
            "prayerRequests": {
                "active": active_prayers,
                "answered": answered_prayers,
                "total": len(prayers),
                "label": f"{active_prayers} active",
                "percentage": prayer_pct,
            },
        },
    }


def _parse_iso_dt(val: Any) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


async def _collect_church_notifications(
    session: DbSession,
    church_id: uuid.UUID,
    church: Optional[Church],
    user_id: uuid.UUID,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    now = _now()
    meta = dict((church.metadata_json if church else None) or {})
    notif_state_all = dict(meta.get("notification_state") or {})
    user_state = dict(notif_state_all.get(str(user_id)) or {})
    read_ids = set(user_state.get("read_ids") or [])
    dismissed_ids = set(user_state.get("dismissed_ids") or [])
    mark_all_before_str = user_state.get("mark_all_read_before")
    mark_all_before = _parse_iso_dt(mark_all_before_str) if mark_all_before_str else None

    raw_items: List[Dict[str, Any]] = []

    # 1. Prayer Requests
    prayers = (
        await session.execute(
            select(PrayerRequest, Member)
            .outerjoin(Member, Member.id == PrayerRequest.member_id)
            .where(PrayerRequest.church_id == church_id)
            .order_by(PrayerRequest.created_at.desc())
            .limit(10)
        )
    ).all()
    prayer_meta_map = dict(meta.get("prayer_meta") or {})
    for pr, mem in prayers:
        nid = f"notif-prayer-{pr.id}"
        pm = dict(prayer_meta_map.get(str(pr.id)) or {})
        priority = str(pm.get("priority") or "").lower()
        is_urgent = (
            priority in ("urgent", "high", "critical")
            or "urgent" in (pr.title or "").lower()
            or "emergency" in (pr.title or "").lower()
            or pr.status == PrayerStatus.ACTIVE
        )
        requester = (
            mem.full_name
            if mem
            else (pm.get("requesterName") or "A church member")
        )
        dt = pr.created_at or now
        raw_items.append(
            {
                "id": nid,
                "type": "urgent" if is_urgent else "info",
                "title": f"Prayer Request: {pr.title}",
                "message": f"{requester}: {pr.content or pr.title}",
                "time": _relative_time_label(dt, now),
                "timestamp": _iso(dt) or "",
                "_dt": dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt,
                "category": "Prayer",
                "actionUrl": "/church/prayer",
            }
        )

    # 2. Recent Member Registrations
    members = (
        await session.execute(
            select(Member)
            .where(Member.church_id == church_id, Member.deleted_at.is_(None))
            .order_by(Member.created_at.desc())
            .limit(8)
        )
    ).scalars().all()
    for m in members:
        nid = f"notif-member-{m.id}"
        dt = m.created_at or now
        raw_items.append(
            {
                "id": nid,
                "type": "info",
                "title": "New Member Registration",
                "message": f"{m.full_name} has completed membership registration",
                "time": _relative_time_label(dt, now),
                "timestamp": _iso(dt) or "",
                "_dt": dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt,
                "category": "Members",
                "actionUrl": f"/church/users/{m.id}",
            }
        )

    # 3. Offerings & Donations
    offerings = (
        await session.execute(
            select(Offering, Fund, Member)
            .outerjoin(Fund, Fund.id == Offering.fund_id)
            .outerjoin(Member, Member.id == Offering.member_id)
            .where(
                Offering.church_id == church_id,
                Offering.deleted_at.is_(None),
            )
            .order_by(Offering.received_at.desc())
            .limit(8)
        )
    ).all()
    for o, f, m in offerings:
        nid = f"notif-offering-{o.id}"
        dt = o.received_at or o.created_at or now
        donor = m.full_name if m else "Anonymous donor"
        fund_name = f.name if f else "General Offering"
        raw_items.append(
            {
                "id": nid,
                "type": "success",
                "title": "Contribution Received",
                "message": f"{donor} contributed KES {float(o.amount or 0):,.0f} towards {fund_name}",
                "time": _relative_time_label(dt, now),
                "timestamp": _iso(dt) or "",
                "_dt": dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt,
                "category": "Finance",
                "actionUrl": "/church/contributions",
            }
        )

    # 4. Events
    events = (
        await session.execute(
            select(Event)
            .where(Event.church_id == church_id, Event.status == EventStatus.ACTIVE)
            .order_by(Event.start_time.desc())
            .limit(8)
        )
    ).scalars().all()
    att_counts = dict(
        (
            await session.execute(
                select(Attendance.event_id, func.count(Attendance.id))
                .join(Event, Event.id == Attendance.event_id)
                .where(Event.church_id == church_id)
                .group_by(Attendance.event_id)
            )
        ).all()
    )
    for ev in events:
        nid = f"notif-event-{ev.id}"
        dt = ev.created_at or ev.start_time or now
        att_c = int(att_counts.get(ev.id, 0) or 0)
        st = ev.start_time
        st_str = (
            st.strftime("%b %d at %I:%M %p").lstrip("0")
            if st
            else "Scheduled"
        )
        raw_items.append(
            {
                "id": nid,
                "type": "warning",
                "title": f"Event Reminder: {ev.name}",
                "message": f"{ev.name} ({st_str}) at {ev.location or 'Main Sanctuary'} — {att_c} attendees recorded",
                "time": _relative_time_label(dt, now),
                "timestamp": _iso(dt) or "",
                "_dt": dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt,
                "category": "Events",
                "actionUrl": "/church/events",
            }
        )

    # 5. Volunteer / Department enrollments
    dept_enrollments = (
        await session.execute(
            select(DepartmentMember, Department, Member)
            .join(Department, Department.id == DepartmentMember.department_id)
            .join(Member, Member.id == DepartmentMember.member_id)
            .where(Department.church_id == church_id, Member.deleted_at.is_(None))
            .order_by(DepartmentMember.created_at.desc())
            .limit(6)
        )
    ).all()
    for dm, dept, mem in dept_enrollments:
        nid = f"notif-vol-{dm.id}"
        dt = dm.created_at or now
        role_label = str(_plain(dm.role) or "Volunteer").replace("_", " ").title()
        raw_items.append(
            {
                "id": nid,
                "type": "info",
                "title": "Ministry Volunteer Assignment",
                "message": f"{mem.full_name} joined {dept.name} ({role_label})",
                "time": _relative_time_label(dt, now),
                "timestamp": _iso(dt) or "",
                "_dt": dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt,
                "category": "Volunteers",
                "actionUrl": "/church/volunteers",
            }
        )

    # 6. Custom notifications in church metadata
    for cn in list(meta.get("custom_notifications") or []):
        if not isinstance(cn, dict):
            continue
        nid = str(cn.get("id") or "")
        if not nid:
            continue
        dt = _parse_iso_dt(cn.get("timestamp")) or now
        raw_items.append(
            {
                "id": nid,
                "type": str(cn.get("type") or "info"),
                "title": str(cn.get("title") or "System Notification"),
                "message": str(cn.get("message") or ""),
                "time": _relative_time_label(dt, now),
                "timestamp": _iso(dt) or "",
                "_dt": dt,
                "category": str(cn.get("category") or "System"),
                "actionUrl": str(cn.get("actionUrl") or "/church/notifications"),
            }
        )

    # Filter out dismissed notifications & sort newest-first
    visible_items: List[Dict[str, Any]] = []
    for item in raw_items:
        nid = item["id"]
        if nid in dismissed_ids:
            continue
        item_dt: datetime = item.pop("_dt", now)
        is_read = (nid in read_ids) or bool(
            mark_all_before and item_dt <= mark_all_before
        )
        item["read"] = is_read
        visible_items.append(item)

    visible_items.sort(key=lambda x: x.get("timestamp") or "", reverse=True)

    today_date = now.date()
    today_count = 0
    for n in visible_items:
        ts_dt = _parse_iso_dt(n.get("timestamp"))
        if ts_dt:
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            if (now - ts_dt).days < 1 or ts_dt.date() == today_date:
                today_count += 1

    stats = {
        "total": len(visible_items),
        "unread": sum(1 for n in visible_items if not n["read"]),
        "urgent": sum(1 for n in visible_items if n["type"] == "urgent"),
        "today": today_count if today_count > 0 else len(visible_items),
    }
    return visible_items, stats


@router.get("/notifications")
async def get_church_notifications(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    filter: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church = await session.get(Church, actor.church_id)
    notifications, stats = await _collect_church_notifications(
        session, actor.church_id, church, current_user.id
    )

    filtered = notifications
    if filter and filter != "all":
        f_low = filter.lower()
        if f_low == "unread":
            filtered = [n for n in filtered if not n["read"]]
        elif f_low == "read":
            filtered = [n for n in filtered if n["read"]]
        else:
            filtered = [n for n in filtered if n["type"].lower() == f_low]

    if category and category.lower() != "all":
        c_low = category.lower()
        filtered = [n for n in filtered if n["category"].lower() == c_low]

    if search and search.strip():
        q = search.strip().lower()
        filtered = [
            n
            for n in filtered
            if q in n["title"].lower()
            or q in n["message"].lower()
            or q in n["category"].lower()
        ]

    return {
        "notifications": filtered,
        "stats": stats,
    }


@router.post("/notifications/mark-all-read")
async def mark_all_church_notifications_read(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church = await session.get(Church, actor.church_id)
    if not church:
        raise HTTPException(status_code=404, detail="Church not found")

    notifications, _ = await _collect_church_notifications(
        session, actor.church_id, church, current_user.id
    )
    meta = dict(church.metadata_json or {})
    notif_state_all = dict(meta.get("notification_state") or {})
    uid_str = str(current_user.id)
    user_state = dict(notif_state_all.get(uid_str) or {})
    read_ids = set(user_state.get("read_ids") or [])
    for n in notifications:
        read_ids.add(n["id"])
    user_state["read_ids"] = list(read_ids)
    user_state["mark_all_read_before"] = _iso(_now())
    notif_state_all[uid_str] = user_state
    meta["notification_state"] = notif_state_all
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "markedCount": len(notifications),
        "unreadCount": 0,
    }


@router.post("/notifications/{notification_id}/read")
async def mark_church_notification_read(
    notification_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    payload: Optional[Dict[str, Any]] = Body(default=None),
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church = await session.get(Church, actor.church_id)
    if not church:
        raise HTTPException(status_code=404, detail="Church not found")

    mark_read = True
    if payload and "read" in payload:
        mark_read = bool(payload["read"])

    meta = dict(church.metadata_json or {})
    notif_state_all = dict(meta.get("notification_state") or {})
    uid_str = str(current_user.id)
    user_state = dict(notif_state_all.get(uid_str) or {})
    read_ids = set(user_state.get("read_ids") or [])
    if mark_read:
        read_ids.add(notification_id)
    else:
        read_ids.discard(notification_id)
    user_state["read_ids"] = list(read_ids)
    notif_state_all[uid_str] = user_state
    meta["notification_state"] = notif_state_all
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, stats = await _collect_church_notifications(
        session, actor.church_id, church, current_user.id
    )
    return {
        "id": notification_id,
        "read": mark_read,
        "unreadCount": stats["unread"],
    }


@router.delete("/notifications/{notification_id}")
async def dismiss_church_notification(
    notification_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church = await session.get(Church, actor.church_id)
    if not church:
        raise HTTPException(status_code=404, detail="Church not found")

    meta = dict(church.metadata_json or {})
    notif_state_all = dict(meta.get("notification_state") or {})
    uid_str = str(current_user.id)
    user_state = dict(notif_state_all.get(uid_str) or {})
    dismissed_ids = set(user_state.get("dismissed_ids") or [])
    dismissed_ids.add(notification_id)
    user_state["dismissed_ids"] = list(dismissed_ids)
    notif_state_all[uid_str] = user_state
    meta["notification_state"] = notif_state_all
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    _, stats = await _collect_church_notifications(
        session, actor.church_id, church, current_user.id
    )
    return {
        "id": notification_id,
        "dismissed": True,
        "unreadCount": stats["unread"],
    }


@router.get("/profile")
async def get_admin_self_profile(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church = await session.get(Church, actor.church_id)
    branch = (
        await session.get(Branch, actor.member.branch_id)
        if actor.member and actor.member.branch_id
        else None
    )
    m = actor.member
    meta = dict((church.metadata_json if church else None) or {})
    admin_profiles = dict(meta.get("admin_profiles") or {})
    prof_meta = dict(admin_profiles.get(str(current_user.id)) or {})
    first_name = m.first_name if m else (current_user.display_name or "").split(" ")[0]
    last_name = (
        m.last_name
        if m
        else " ".join((current_user.display_name or "").split(" ")[1:])
    )
    full_name = f"{first_name} {last_name}".strip() or (current_user.display_name or "Admin")
    return {
        "id": str(current_user.id),
        "memberId": str(m.id) if m else "",
        "firstName": first_name,
        "lastName": last_name,
        "fullName": full_name,
        "email": current_user.email,
        "phoneNumber": (m.phone_number if m and m.phone_number else current_user.phone_number) or "",
        "profilePictureUrl": (
            (m.photo_url if m and m.photo_url else None)
            or current_user.profile_picture_url
            or ""
        ),
        "position": (m.position if m and m.position else "Church Administrator"),
        "bio": str(prof_meta.get("bio") or ""),
        "role": actor.roles[0] if actor.roles else "ADMIN",
        "roles": list(actor.roles),
        "churchId": str(actor.church_id),
        "churchName": church.name if church else "",
        "branchId": str(branch.id) if branch else "",
        "branchName": branch.name if branch else "Headquarters",
    }


@router.put("/profile")
async def update_admin_self_profile(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    payload: Dict[str, Any] = Body(...),
    organizationId: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, organizationId)
    )
    church = await session.get(Church, actor.church_id)
    m = actor.member

    first_name = str(payload.get("firstName") or "").strip()
    last_name = str(payload.get("lastName") or "").strip()
    phone_number = str(payload.get("phoneNumber") or "").strip()
    profile_pic = payload.get("profilePictureUrl")
    position = payload.get("position")
    bio = payload.get("bio")
    new_password = payload.get("newPassword")
    current_password = payload.get("currentPassword")

    if new_password:
        if current_password and not verify_password(
            current_password, current_user.hashed_password
        ):
            raise HTTPException(
                status_code=400, detail="Current password is incorrect"
            )
        if len(str(new_password)) < 6:
            raise HTTPException(
                status_code=400,
                detail="New password must be at least 6 characters",
            )
        current_user.hashed_password = get_password_hash(str(new_password))

    if first_name or last_name:
        full_name = f"{first_name} {last_name}".strip()
        current_user.display_name = full_name
        if m:
            if first_name:
                m.first_name = first_name
            if last_name:
                m.last_name = last_name

    if phone_number is not None:
        current_user.phone_number = phone_number or None
        if m:
            m.phone_number = phone_number or None

    if profile_pic is not None:
        current_user.profile_picture_url = profile_pic or None
        if m:
            m.photo_url = profile_pic or None

    if m and position is not None:
        m.position = str(position).strip() or None

    if church and bio is not None:
        meta = dict(church.metadata_json or {})
        admin_profiles = dict(meta.get("admin_profiles") or {})
        prof_meta = dict(admin_profiles.get(str(current_user.id)) or {})
        prof_meta["bio"] = str(bio).strip()
        admin_profiles[str(current_user.id)] = prof_meta
        meta["admin_profiles"] = admin_profiles
        church.metadata_json = meta
        flag_modified(church, "metadata_json")

    await session.commit()
    return await get_admin_self_profile(
        request=request,
        current_user=current_user,
        session=session,
        organizationId=organizationId,
    )




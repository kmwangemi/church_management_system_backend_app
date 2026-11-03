import enum
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from app.core.deps import (
    Actor,
    CurrentUser,
    DbSession,
    authorize,
    church_id_from,
    require,
    resolve_actor,
)
from app.core.security import get_password_hash
from app.core.seed import ADMIN_ROLE_NAMES
from app.enums import AccountStatus, MemberType, MembershipStatus
from app.models.auth import MemberRoleAssignment, Role
from app.models.church import Branch
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
from app.models.group import Department, DepartmentMember
from app.models.member import Guardian, Household, Member
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
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from sqlalchemy import delete, func, or_, select
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
    row = (
        await session.execute(select(model).where(model.member_id == member_id))
    ).scalar_one_or_none()
    data = {k: _plain(v) for k, v in data.items()}
    if row is None:
        row = model(member_id=member_id, **{**(defaults or {}), **data})
        session.add(row)
    else:
        for k, v in data.items():
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
        session.add(
            EmergencyContact(
                member_id=member_id,
                full_name=c.full_name,
                phone_number=c.phone_number,
                relationship_type=c.relationship_type,
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
    return {
        "members": items,
        "users": items,
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
    base = [Member.church_id == actor.church_id, Member.deleted_at.is_(None)]
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
    total = sum(by_status.values())
    active = by_status.get(MembershipStatus.ACTIVE, 0)
    suspended = by_status.get(MembershipStatus.SUSPENDED, 0)
    return {
        "totalMembers": total,
        "activeMembers": active,
        "inactiveMembers": total - active - suspended,
        "suspendedMembers": suspended,
        "admins": admins,
        "visitors": by_type.get(MemberType.VISITOR, 0),
        "children": by_type.get(MemberType.CHILD, 0),
    }


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
    def one(model):
        return select(model).where(model.member_id == member.id)
    md = (await session.execute(one(MemberDetails))).scalar_one_or_none()
    vol = (await session.execute(one(VolunteerDetails))).scalar_one_or_none()
    staff = (await session.execute(one(StaffDetails))).scalar_one_or_none()
    pastor = (await session.execute(one(PastorDetails))).scalar_one_or_none()
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
    row = format_member_row(member, user, branch, roles)
    created = _iso(member.created_at) or ""
    first = contacts[0] if contacts else None
    return {
        "id": str(member.id),
        "membershipNumber": mem_code,
        "memberId": mem_code or str(member.id),
        "userId": str(user.id) if user else None,
        "hasLogin": user is not None,
        "memberType": member.member_type.value,
        "role": roles or ["MEMBER"],
        "roles": roles or ["MEMBER"],
        "position": member.position or "Member",
        "status": member.status.value,
        "organizationId": str(member.church_id),
        "createdAt": created,
        "user": {
            **row["user"],
            "dateOfBirth": (
                member.date_of_birth.isoformat() if member.date_of_birth else None
            ),
            "maritalStatus": (
                md.marital_status.value if md and md.marital_status else None
            ),
            "occupation": (md.occupation if md else None) or "",
            "skills": [],
            "notes": "",
        },
        "member": {
            "id": str(member.id),
            "_id": str(member.id),
            "membershipNumber": mem_code,
            "memberId": mem_code or str(member.id),
            "role": roles or ["MEMBER"],
            "roles": roles or ["MEMBER"],
            "position": member.position or "Member",
            "churchId": str(member.church_id),
            "organizationId": str(member.church_id),
            "branchId": str(member.branch_id),
            "teamId": str(member.branch_id),
            "householdId": member.household_id,
            "householdRole": (
                member.household_role.value if member.household_role else None
            ),
            "createdAt": created,
            "joinedDate": member.joined_at.isoformat() if member.joined_at else created,
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
            "joinedDate": (
                _iso(md.membership_date) if md and md.membership_date else created
            ),
            "baptismDate": _iso(md.baptism_date) if md else None,
            "departmentIds": [],
            "groupIds": [],
        },
        "volunteerDetails": {
            "volunteerId": mem_code or str(member.id),
            "hoursContributed": vol.hours_contributed if vol else 0,
            "roles": [],
        },
        "staffDetails": {
            "staffId": mem_code or str(member.id),
            "department": staff.department if staff else "",
            "jobTitle": staff.job_title if staff else "",
            "employmentType": "FULL_TIME",
            "startDate": _iso(staff.start_date) if staff else None,
        },
        "pastorDetails": {
            "pastorId": mem_code or str(member.id),
            "ordinationDate": _iso(pastor.ordination_date) if pastor else None,
            "sermonCount": pastor.sermon_count if pastor else 0,
            "biography": (pastor.biography if pastor else "") or "",
        },
        "adminDetails": {
            "adminId": mem_code or str(member.id),
            "accessLevel": (admin_d.access_level if admin_d else "BRANCH"),
            "assignedBranches": [str(member.branch_id)],
        },
        "visitorDetails": {
            "visitDate": _iso(visitor.visit_date) if visitor else None,
            "howDidYouHear": visitor.how_did_you_hear if visitor else None,
            "followUpStatus": visitor.follow_up_status if visitor else None,
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
        if member.id == (actor.member.id if actor.member else None):
            raise HTTPException(
                status_code=400, detail="You cannot change your own roles"
            )
        roles = await resolve_roles(
            session,
            actor.church_id,
            payload.roles or default_role_names(member.member_type),
        )
        await set_member_roles(session, member.id, roles, replace=True)
        changed.append("roles")
    if payload.address:
        await _apply_address(session, member, payload.address)
        changed.append("address")
    if payload.emergencyContacts is not None:
        await _replace_emergency_contacts(session, member.id, payload.emergencyContacts)
        changed.append("emergencyContacts")
    detail_models = [
        ("adminDetails", AdminDetails, {}),
        ("staffDetails", StaffDetails, {}),
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
            if data.get("invited_by_member_id"):
                await _check_members_in_church(
                    session, [data["invited_by_member_id"]], actor.church_id
                )
            await _upsert_detail(session, model, member.id, data, defaults)
            changed.append(key)
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
    await _check_members_in_church(session, [pastor_member_id], church_id)
    details = await _upsert_detail(session, PastorDetails, pastor_member_id, {})
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
        "organizationId": str(b.church_id),
        "churchId": str(b.church_id),
        "memberCount": member_count,
        "members": member_count,
        "capacity": b.capacity or 500,
        "email": b.email or "",
        "phone": b.phone_number or "",
        "phoneNumber": b.phone_number or "",
        "isActive": b.is_active,
        "isDeleted": b.is_deleted,
        "establishedDate": _iso(b.created_at) or "",
        "createdAt": _iso(b.created_at) or "",
        "updatedAt": _iso(b.updated_at) or "",
        "_count": {"teammembers": member_count, "members": member_count},
        "pastorId": {
            "id": pastor.get("id", ""),
            "_id": pastor.get("id", ""),
            "firstName": p_first,
            "lastName": p_last,
            "name": pastor_name,
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
):
    actor = await authorize(
        session, current_user, church_id_from(request, organizationId), "branch.read"
    )
    conds = [Branch.church_id == actor.church_id, Branch.deleted_at.is_(None)]
    if search:
        conds.append(Branch.name.ilike(f"%{search.strip()}%"))
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
    counts = await _member_counts(session, actor.church_id)
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
    return {
        "teams": items,
        "branches": items,
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
    branch = Branch(
        name=(payload.name or payload.branchName).strip(),
        church_id=actor.church_id,
        email=payload.email,
        phone_number=payload.phoneNumber or payload.phone,
        capacity=payload.capacity if payload.capacity is not None else 500,
        is_active=True,
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
    if payload.status is not None:
        branch.is_active = payload.status.strip().lower() == "active"
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
    """Departments are church-wide; they are shown under each branch."""
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
    items = [
        {
            "id": str(d.id),
            "_id": str(d.id),
            "name": d.name,
            "description": d.description or "",
            "isActive": d.is_active,
            "branchId": str(branch.id),
            "churchId": str(branch.church_id),
            "createdAt": _iso(d.created_at) or "",
            "updatedAt": _iso(d.updated_at) or "",
            "memberCount": counts.get(d.id, 0),
            "_count": {"members": counts.get(d.id, 0)},
        }
        for d in departments
    ]
    return {
        "departments": items,
        "pagination": {
            "total": len(items),
            "page": page,
            "limit": limit,
            "totalPages": 1,
            "hasMore": False,
        },
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
):
    branch, _ = await _branch_actor(
        session, request, current_user, branch_id, "branch.read"
    )
    services = (branch.metadata_json or {}).get(
        "services", ["Sunday 9:00 AM", "Sunday 11:00 AM"]
    )
    schedules = [
        {
            "id": f"{branch.id}-{i}",
            "_id": f"{branch.id}-{i}",
            "name": svc if isinstance(svc, str) else svc.get("name", "Sunday Service"),
            "day": "Sunday",
            "time": "09:00 AM" if i == 0 else "11:00 AM",
            "type": "Regular Service",
            "branchId": str(branch.id),
            "isActive": True,
        }
        for i, svc in enumerate(services)
    ]
    return {
        "schedules": schedules,
        "serviceSchedules": schedules,
        "pagination": {
            "total": len(schedules),
            "page": page,
            "limit": limit,
            "totalPages": 1,
            "hasMore": False,
        },
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
):
    await _branch_actor(session, request, current_user, branch_id, "branch.read")
    return {
        "activities": [],
        "pagination": {
            "total": 0,
            "page": page,
            "limit": limit,
            "totalPages": 0,
            "hasMore": False,
        },
    }

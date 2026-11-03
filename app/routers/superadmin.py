import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.core.deps import CurrentSuperAdmin, DbSession
from app.core.security import get_password_hash
from app.core.seed import ADMIN_ROLE_NAMES
from app.enums import AccountStatus, MemberType, PlatformRole, SubscriptionStatus
from app.models.audit import AuditLog
from app.models.church import Branch, Church
from app.models.detail import Address, AdminDetails
from app.models.finance import DEFAULT_CURRENCY, Fund, Offering
from app.models.member import Member
from app.models.subscription import ChurchSubscription, Plan
from app.models.user import User, UserSession
from app.routers.admin import (
    _pastors_for,
    _set_branch_pastor,
    _upsert_detail,
    format_branch_response,
)
from app.schemas.church import (
    BranchCreateRequest,
    BranchUpdateRequest,
    ChurchCreateRequest,
    ChurchUpdateRequest,
    GlobalSettingsUpdateRequest,
    SuperAdminUserCreateRequest,
    SuperAdminUserUpdateRequest,
)
from app.services.audit import queue_audit
from app.services.membership_service import generate_unique_membership_number
from app.services.roles import resolve_roles, role_names_for_members, set_member_roles
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

router = APIRouter()

# NOTE: in-memory, so per-process: lost on restart and different on every worker.
# Move this to a table (key/value JSON) before relying on it.
GLOBAL_SYSTEM_SETTINGS: Dict[str, Any] = {
    "siteName": "ChurchHub Global",
    "supportEmail": "support@churchhub.com",
    "defaultCurrency": "USD",
    "trialPeriodDays": 30,
    "maintenanceMode": False,
    "allowNewRegistrations": True,
    "sessionTimeoutMinutes": 60,
    "emailSettings": {
        "smtpHost": "smtp.sendgrid.net",
        "smtpPort": "587",
        "fromEmail": "notifications@churchhub.com",
    },
    "securitySettings": {
        "mfaRequired": False,
        "passwordMinLength": 8,
        "maxLoginAttempts": 5,
    },
}

START_TIME = time.time()


# ============================================================================
# Helpers
# ============================================================================
def _iso(dt: Optional[datetime]) -> str:
    return dt.isoformat() if dt else ""


def slugify(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return f"{cleaned or 'church'}-{uuid4().hex[:6]}"


async def _member_counts_by_church(session) -> Dict[str, int]:
    rows = (
        await session.execute(
            select(Member.church_id, func.count(Member.id))
            .where(Member.deleted_at.is_(None))
            .group_by(Member.church_id)
        )
    ).all()
    return {str(c): n for c, n in rows}


async def _branch_counts_by_church(session) -> Dict[str, int]:
    rows = (
        await session.execute(
            select(Branch.church_id, func.count(Branch.id))
            .where(Branch.deleted_at.is_(None))
            .group_by(Branch.church_id)
        )
    ).all()
    return {str(c): n for c, n in rows}


async def _revenue(session, church_id: Optional[str] = None) -> Dict[str, float]:
    """Offering totals per currency (soft-deleted rows excluded). Never mix currencies in one number."""
    q = select(Offering.currency, func.sum(Offering.amount)).where(
        Offering.deleted_at.is_(None)
    )
    if church_id:
        q = q.where(Offering.church_id == church_id)
    return {
        cur: float(total or 0)
        for cur, total in (await session.execute(q.group_by(Offering.currency))).all()
    }


def format_church_response(
    church: Church,
    address: Optional[Address] = None,
    subscription: Optional[ChurchSubscription] = None,
    member_count: int = 0,
    branch_count: int = 0,
) -> Dict[str, Any]:
    metadata = church.metadata_json or {}
    status_str = (
        "SUSPENDED"
        if church.is_suspended
        else ("ACTIVE" if church.is_active else "INACTIVE")
    )
    addr_dict = {
        "id": str(address.id) if address else "",
        "street": (address.street if address else "") or "",
        "city": (address.city if address else "") or "",
        "state": (address.state if address else "") or "",
        "zipCode": (address.zip_code if address else "") or "",
        "country": (address.country if address else "Kenya") or "Kenya",
        "createdAt": _iso(address.created_at) if address else "",
        "updatedAt": _iso(address.updated_at) if address else "",
        "userId": None,
        "organizationId": str(church.id),
        "teamId": None,
    }
    plan = (
        subscription.plan
        if subscription is not None and subscription.plan is not None
        else None
    )
    sub = subscription
    sub_dict = {
        "id": str(sub.id) if sub else "",
        "plan": plan.name if plan else "BASIC",
        "planId": str(plan.id) if plan else None,
        "status": sub.status.value if sub else "TRIAL",
        "isPaid": sub.is_paid if sub else False,
        "isActive": sub.is_active if sub else True,
        "invoiceAmount": (
            float(sub.invoice_amount) if sub and sub.invoice_amount else 0.0
        ),
        "paidAmount": (
            float(sub.invoice_amount)
            if sub and sub.is_paid and sub.invoice_amount
            else 0.0
        ),
        "balAmount": 0.0,
        "maxUsers": None,
        "maxTeams": None,
        "maxSmallGroups": None,
        "currentUsers": member_count,
        "currentTeams": branch_count,
        "currentSmallGroups": 0,
        "features": (
            sub.features
            if sub and sub.features
            else ["MEMBERS_MANAGEMENT", "ATTENDANCE", "FINANCES"]
        ),
        "startDate": _iso(sub.start_date) if sub else _iso(church.created_at),
        "endDate": _iso(sub.end_date) if sub else "",
        "isAutoRenew": True,
        "paymentMethod": "CARD",
        "lastPaymentDate": None,
        "nextBillingDate": _iso(sub.end_date) if sub and sub.end_date else None,
        "canceledAt": None,
        "cancelReason": None,
        "createdAt": _iso(sub.created_at) if sub else _iso(church.created_at),
        "updatedAt": _iso(sub.updated_at) if sub else _iso(church.updated_at),
        "organizationId": str(church.id),
    }
    return {
        "id": str(church.id),
        "name": church.name,
        "slug": church.slug or "",
        "logo": church.logo or "",
        "createdAt": _iso(church.created_at),
        "updatedAt": _iso(church.updated_at),
        "denomination": church.denomination or "",
        "description": church.description or "",
        "establishedDate": metadata.get("establishedDate", ""),
        "email": church.email or "",
        "phoneNumber": church.phone_number or "",
        "website": church.website or "",
        "churchSize": metadata.get("churchSize", "100-500"),
        "numberOfBranches": branch_count,
        "status": status_str,
        "mission": metadata.get("mission"),
        "vision": metadata.get("vision"),
        "values": metadata.get("values", []),
        "isActive": church.is_active and not church.is_suspended,
        "isSuspended": church.is_suspended,
        "isDeleted": church.is_deleted,
        "metadata": metadata,
        "address": addr_dict,
        "subscription": sub_dict,
        "_count": {"members": member_count, "teams": branch_count},
    }


async def _extras(session, churches: List[Church]):
    """Addresses and subscriptions (with plan) for several churches in two queries."""
    addr_ids = [c.address_id for c in churches if c.address_id]
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
    ids = [c.id for c in churches]
    subs = (
        {
            s.church_id: s
            for s in (
                await session.execute(
                    select(ChurchSubscription)
                    .options(selectinload(ChurchSubscription.plan))
                    .where(ChurchSubscription.church_id.in_(ids))
                )
            )
            .scalars()
            .all()
        }
        if ids
        else {}
    )
    return addrs, subs


async def _get_church(session, church_id: str) -> Church:
    church = (
        await session.execute(
            select(Church).where(Church.id == church_id, Church.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not church:
        raise HTTPException(status_code=404, detail="Church not found")
    return church


async def _church_payload(session, church: Church) -> Dict[str, Any]:
    addrs, subs = await _extras(session, [church])
    members = (await _member_counts_by_church(session)).get(str(church.id), 0)
    branches = (await _branch_counts_by_church(session)).get(str(church.id), 0)
    return format_church_response(
        church, addrs.get(church.address_id), subs.get(church.id), members, branches
    )


async def _find_plan(
    session, name: Optional[str] = None, plan_id: Optional[str] = None
) -> Plan:
    stmt = (
        select(Plan).where(Plan.id == plan_id)
        if plan_id
        else select(Plan).where(func.upper(Plan.name) == (name or "BASIC").upper())
    )
    plan = (await session.execute(stmt)).scalar_one_or_none()
    if not plan:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown plan '{plan_id or name}'. Seed plans first (app.core.seed.seed_plans).",
        )
    return plan


async def _new_address(session, data) -> Address:
    addr = Address()
    session.add(addr)
    await session.flush()
    for k, v in data.model_dump(exclude_unset=True).items():
        if not (k == "country" and v is None):
            setattr(addr, k, v)
    return addr


async def _invalidate_sessions(session, user_id: str) -> None:
    await session.execute(
        update(UserSession).where(UserSession.user_id == user_id).values(is_valid=False)
    )


# ============================================================================
# CHURCHES MANAGEMENT
# ============================================================================
@router.get("/churches")
async def get_all_churches(current_user: CurrentSuperAdmin, session: DbSession):
    """All churches with aggregated statistics (a handful of queries, not 4 per church)."""
    churches = (
        (
            await session.execute(
                select(Church)
                .where(Church.deleted_at.is_(None))
                .order_by(Church.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    addrs, subs = await _extras(session, list(churches))
    members = await _member_counts_by_church(session)
    branches = await _branch_counts_by_church(session)
    formatted = [
        format_church_response(
            c,
            addrs.get(c.address_id),
            subs.get(c.id),
            members.get(str(c.id), 0),
            branches.get(str(c.id), 0),
        )
        for c in churches
    ]
    revenue = await _revenue(session)
    return {
        "churches": formatted,
        "stats": {
            "totalChurches": len(churches),
            "totalMembers": sum(members.get(str(c.id), 0) for c in churches),
            "totalBranches": sum(branches.get(str(c.id), 0) for c in churches),
            "totalRevenue": revenue.get(DEFAULT_CURRENCY, 0.0),  # default currency only
            "revenueByCurrency": revenue,
        },
    }


@router.get("/churches/statistics")
async def get_church_statistics(current_user: CurrentSuperAdmin, session: DbSession):
    live = Church.deleted_at.is_(None)
    total = (
        await session.execute(select(func.count(Church.id)).where(live))
    ).scalar() or 0
    active = (
        await session.execute(
            select(func.count(Church.id)).where(
                live, Church.is_active.is_(True), Church.is_suspended.is_(False)
            )
        )
    ).scalar() or 0
    suspended = (
        await session.execute(
            select(func.count(Church.id)).where(live, Church.is_suspended.is_(True))
        )
    ).scalar() or 0
    revenue = await _revenue(session)
    return {
        "totalChurches": total,
        "activeChurches": active,
        "suspendedChurches": suspended,
        "totalBranches": sum((await _branch_counts_by_church(session)).values()),
        "totalMembers": sum((await _member_counts_by_church(session)).values()),
        "totalRevenue": revenue.get(DEFAULT_CURRENCY, 0.0),
        "revenueByCurrency": revenue,
        "growthRate": 12.5,  # PLACEHOLDER: not computed from data
    }


@router.get("/churches/{organization_id}")
async def get_church_details(
    organization_id: str, current_user: CurrentSuperAdmin, session: DbSession
):
    return await _church_payload(session, await _get_church(session, organization_id))


@router.post("/churches")
async def create_church(
    payload: ChurchCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentSuperAdmin,
    session: DbSession,
):
    """Register a church with its address, subscription, a default fund and a main branch."""
    plan = await _find_plan(session, name=payload.subscriptionPlan)
    slug = slugify(payload.churchName)
    while (await session.execute(select(Church.id).where(Church.slug == slug))).first():
        slug = slugify(payload.churchName)
    church = Church(
        name=payload.churchName,
        slug=slug,
        logo=payload.churchLogoUrl or "",
        denomination=payload.denomination,
        description=payload.description or "",
        email=payload.email,
        phone_number=payload.phoneNumber,
        website=payload.website or "",
        is_active=True,
        is_suspended=False,
        metadata_json={
            "churchSize": payload.churchSize,
            "establishedDate": payload.establishedDate,
        },
    )
    session.add(church)
    await session.flush()
    if payload.address:
        church.address_id = (await _new_address(session, payload.address)).id
    now = datetime.now(timezone.utc)
    session.add(
        ChurchSubscription(
            church_id=church.id,
            plan_id=plan.id,
            status=SubscriptionStatus.TRIAL,
            is_paid=False,
            is_active=True,
            invoice_amount=Decimal("0.00"),
            start_date=now,
            end_date=now + timedelta(days=30),
            features=["MEMBERS_MANAGEMENT", "ATTENDANCE", "FINANCES"],
        )
    )
    session.add(
        Fund(church_id=church.id, name="General")
    )  # offerings and pledges require a fund
    main_branch = Branch(
        church_id=church.id,
        name=f"{payload.churchName} Main Branch",
        email=payload.email,
        phone_number=payload.phoneNumber,
        capacity=500,
        is_active=True,
        metadata_json={"services": ["Sunday 9:00 AM", "Sunday 11:00 AM"]},
    )
    if payload.address:  # its own copy, so editing one never changes the other
        main_branch.address_id = (await _new_address(session, payload.address)).id
    session.add(main_branch)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Church could not be created (duplicate slug)"
        )
    queue_audit(
        background_tasks,
        request,
        "CREATE_CHURCH",
        "CHURCH",
        church.id,
        actor_user_id=current_user.id,
        church_id=church.id,
    )
    return await _church_payload(session, church)


@router.put("/churches/{organization_id}")
async def update_church(
    organization_id: str,
    payload: ChurchUpdateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentSuperAdmin,
    session: DbSession,
):
    church = await _get_church(session, organization_id)
    for attr, value in (
        ("name", payload.name),
        ("logo", payload.logo),
        ("denomination", payload.denomination),
        ("description", payload.description),
        ("email", payload.email),
        ("phone_number", payload.phoneNumber),
        ("website", payload.website),
    ):
        if value is not None:
            setattr(church, attr, value)
    if payload.slug is not None and payload.slug != church.slug:
        if (
            await session.execute(
                select(Church.id).where(
                    Church.slug == payload.slug, Church.id != church.id
                )
            )
        ).first():
            raise HTTPException(status_code=409, detail="Slug is already in use")
        church.slug = payload.slug
    if payload.status is not None:
        s = payload.status.upper()
        if s not in ("ACTIVE", "SUSPENDED", "INACTIVE"):
            raise HTTPException(
                status_code=400, detail="status must be ACTIVE, SUSPENDED or INACTIVE"
            )
        church.is_active = s == "ACTIVE"
        church.is_suspended = s == "SUSPENDED"
    metadata = dict(church.metadata_json or {})
    if payload.churchSize:
        metadata["churchSize"] = payload.churchSize
    if payload.metadata:
        metadata.update(payload.metadata)
    church.metadata_json = metadata
    if payload.address:
        addr = (
            await session.get(Address, church.address_id) if church.address_id else None
        )
        if addr is None:
            church.address_id = (await _new_address(session, payload.address)).id
        else:
            for k, v in payload.address.model_dump(exclude_unset=True).items():
                if not (k == "country" and v is None):
                    setattr(addr, k, v)
    if payload.subscription:
        sub = (
            await session.execute(
                select(ChurchSubscription).where(
                    ChurchSubscription.church_id == church.id
                )
            )
        ).scalar_one_or_none()
        sp = payload.subscription
        if sub is None:
            plan = await _find_plan(session, name=sp.plan, plan_id=sp.planId)
            now = datetime.now(timezone.utc)
            sub = ChurchSubscription(
                church_id=church.id,
                plan_id=plan.id,
                status=SubscriptionStatus.TRIAL,
                start_date=now,
                end_date=now + timedelta(days=30),
            )
            session.add(sub)
        elif sp.plan or sp.planId:
            sub.plan_id = (
                await _find_plan(session, name=sp.plan, plan_id=sp.planId)
            ).id
        for attr, value in (
            ("status", sp.status),
            ("is_paid", sp.isPaid),
            ("is_active", sp.isActive),
            ("start_date", sp.startDate),
            ("end_date", sp.endDate),
            ("features", sp.features),
        ):
            if value is not None:
                setattr(sub, attr, value)
    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "UPDATE_CHURCH",
        "CHURCH",
        church.id,
        actor_user_id=current_user.id,
        church_id=church.id,
    )
    return await _church_payload(session, church)


async def _set_church_state(
    session, church_id, *, suspended=None, active=None, deleted=False
):
    church = await _get_church(session, church_id)
    if suspended is not None:
        church.is_suspended = suspended
    if active is not None:
        church.is_active = active
    if deleted:
        church.soft_delete()
    await session.commit()
    return church


@router.post("/churches/{organization_id}/suspend")
async def suspend_church(
    organization_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentSuperAdmin,
    session: DbSession,
):
    church = await _set_church_state(
        session, organization_id, suspended=True, active=False
    )
    queue_audit(
        background_tasks,
        request,
        "SUSPEND_CHURCH",
        "CHURCH",
        church.id,
        actor_user_id=current_user.id,
        church_id=church.id,
    )
    return {"message": "Church suspended successfully"}


@router.post("/churches/{organization_id}/activate")
async def activate_church(
    organization_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentSuperAdmin,
    session: DbSession,
):
    church = await _set_church_state(
        session, organization_id, suspended=False, active=True
    )
    queue_audit(
        background_tasks,
        request,
        "ACTIVATE_CHURCH",
        "CHURCH",
        church.id,
        actor_user_id=current_user.id,
        church_id=church.id,
    )
    return {"message": "Church activated successfully"}


@router.delete("/churches/{organization_id}")
async def delete_church(
    organization_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentSuperAdmin,
    session: DbSession,
):
    church = await _set_church_state(
        session, organization_id, active=False, deleted=True
    )
    queue_audit(
        background_tasks,
        request,
        "DELETE_CHURCH",
        "CHURCH",
        church.id,
        actor_user_id=current_user.id,
        church_id=church.id,
    )
    return {"message": "Church deleted successfully"}


# ============================================================================
# DASHBOARD
# ============================================================================
@router.get("/dashboard")
async def get_dashboard_overview(current_user: CurrentSuperAdmin, session: DbSession):
    """
    Real: counts, revenue, top churches, recent activity.
    PLACEHOLDERS (not computed from data): the "change" percentages, growth, alerts and health.
    """
    churches = (
        (
            await session.execute(
                select(Church)
                .where(Church.deleted_at.is_(None))
                .order_by(Church.created_at)
            )
        )
        .scalars()
        .all()
    )
    members = await _member_counts_by_church(session)
    branches = await _branch_counts_by_church(session)
    revenue = await _revenue(session)
    rev_default = revenue.get(DEFAULT_CURRENCY, 0.0)
    total_members = sum(members.get(str(c.id), 0) for c in churches)
    per_church_rev = {
        str(cid): float(total or 0)
        for cid, total in (
            await session.execute(
                select(Offering.church_id, func.sum(Offering.amount))
                .where(
                    Offering.deleted_at.is_(None), Offering.currency == DEFAULT_CURRENCY
                )
                .group_by(Offering.church_id)
            )
        ).all()
    }
    top = sorted(churches, key=lambda c: members.get(str(c.id), 0), reverse=True)[:5]
    top_churches = [
        {
            "name": c.name,
            "members": members.get(str(c.id), 0),
            "growth": 12.5,  # placeholder
            "revenue": f"{DEFAULT_CURRENCY} {per_church_rev.get(str(c.id), 0.0):,.2f}",
        }
        for c in top
    ]
    logs = (
        (
            await session.execute(
                select(AuditLog).order_by(AuditLog.created_at.desc()).limit(8)
            )
        )
        .scalars()
        .all()
    )
    recent = [
        {
            "church": "System",
            "action": a.action.replace("_", " ").title(),
            "time": (
                a.created_at.strftime("%b %d, %H:%M") if a.created_at else "Just now"
            ),
        }
        for a in logs
    ] or [{"church": "System", "action": "System Initialized", "time": "Today"}]
    return {
        "stats": [
            {
                "name": "Total Churches",
                "value": str(len(churches)),
                "change": "+12%",
                "changeType": "positive",
            },
            {
                "name": "Total Members",
                "value": f"{total_members:,}",
                "change": "+8.2%",
                "changeType": "positive",
            },
            {
                "name": "Active Branches",
                "value": str(sum(branches.get(str(c.id), 0) for c in churches)),
                "change": "+15%",
                "changeType": "positive",
            },
            {
                "name": "Total Giving",
                "value": f"{DEFAULT_CURRENCY} {rev_default:,.2f}",
                "change": "+23%",
                "changeType": "positive",
            },
        ],
        "topChurches": top_churches,
        "systemAlerts": [
            {
                "type": "success",
                "message": "All database and service systems operational",
                "time": "Just now",
            },
            {
                "type": "info",
                "message": f"{len(churches)} churches currently monitored globally",
                "time": "Today",
            },
        ],
        "recentActivities": recent,
        "systemHealth": [
            {"name": "Server Uptime", "value": 99.9, "status": "excellent"},
            {"name": "Database Performance", "value": 98.4, "status": "excellent"},
            {"name": "API Response Time", "value": 94.2, "status": "good"},
            {"name": "Storage Usage", "value": 24.5, "status": "good"},
        ],
    }


# ============================================================================
# BRANCHES MANAGEMENT
# ============================================================================
async def _branch_row(
    session, b: Branch, church_name: str, count: int
) -> Dict[str, Any]:
    addr = await session.get(Address, b.address_id) if b.address_id else None
    pastor = (await _pastors_for(session, [b.id])).get(str(b.id))
    addr_str = f"{addr.street or ''}, {addr.city or ''}".strip(", ") if addr else ""
    meta = b.metadata_json or {}
    return {
        "id": str(b.id),
        "name": b.name,
        "church": church_name,
        "churchId": str(b.church_id),
        "pastor": f"{pastor['first']} {pastor['last']}".strip() if pastor else "",
        "pastorId": pastor["id"] if pastor else None,
        "address": addr_str or "Main Campus",
        "phone": b.phone_number or "",
        "email": b.email or "",
        "members": count,
        "memberCount": count,
        "_count": {"teammembers": count, "members": count},
        "capacity": b.capacity or 500,
        "established": str(b.created_at.year) if b.created_at else "",
        "status": "active" if b.is_active else "inactive",
        "services": meta.get("services", ["Sunday 9:00 AM", "Sunday 11:00 AM"]),
        "growth": 10.5,  # placeholder
        "lastActivity": "Active",
    }


@router.get("/branches")
async def get_all_branches(
    current_user: CurrentSuperAdmin,
    session: DbSession,
    churchId: Optional[str] = None,
    search: Optional[str] = None,
    status: Optional[str] = None,
):
    conds = [Branch.deleted_at.is_(None), Church.deleted_at.is_(None)]
    if churchId and churchId != "all":
        conds.append(Branch.church_id == churchId)
    if status and status.lower() == "active":
        conds.append(Branch.is_active.is_(True))
    elif status and status.lower() == "inactive":
        conds.append(Branch.is_active.is_(False))
    if search:
        q = f"%{search.strip().lower()}%"
        conds.append(
            or_(func.lower(Branch.name).like(q), func.lower(Church.name).like(q))
        )
    rows = (
        await session.execute(
            select(Branch, Church.name)
            .join(Church, Church.id == Branch.church_id)
            .where(*conds)
            .order_by(Branch.created_at)
        )
    ).all()
    counts = {
        str(bid): n
        for bid, n in (
            await session.execute(
                select(Member.branch_id, func.count(Member.id))
                .where(Member.deleted_at.is_(None))
                .group_by(Member.branch_id)
            )
        ).all()
    }
    return {
        "branches": [
            await _branch_row(session, b, cname, counts.get(str(b.id), 0))
            for b, cname in rows
        ]
    }


@router.post("/branches")
async def create_branch(
    payload: BranchCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentSuperAdmin,
    session: DbSession,
):
    church = await _get_church(session, payload.churchId)
    branch = Branch(
        name=payload.name,
        church_id=church.id,
        email=payload.email or None,
        phone_number=payload.phone or None,
        capacity=payload.capacity or 500,
        is_active=True,
        metadata_json={"services": payload.services or ["Sunday 9:00 AM"]},
    )
    session.add(branch)
    await session.flush()
    if payload.address:
        branch.address_id = (await _new_address(session, payload.address)).id
    if payload.pastorId:
        await _set_branch_pastor(session, branch, church.id, payload.pastorId)
    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "CREATE_BRANCH",
        "BRANCH",
        branch.id,
        actor_user_id=current_user.id,
        church_id=church.id,
    )
    return await _branch_row(session, branch, church.name, 0)


@router.put("/branches/{branch_id}")
async def update_branch(
    branch_id: str,
    payload: BranchUpdateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentSuperAdmin,
    session: DbSession,
):
    branch = (
        await session.execute(
            select(Branch).where(Branch.id == branch_id, Branch.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    if payload.name is not None:
        branch.name = payload.name
    if payload.email is not None:
        branch.email = payload.email or None
    if payload.phone is not None:
        branch.phone_number = payload.phone or None
    if payload.capacity is not None:
        branch.capacity = payload.capacity
    if payload.status is not None:
        branch.is_active = payload.status.lower() == "active"
    if payload.services is not None:
        branch.metadata_json = {
            **(branch.metadata_json or {}),
            "services": payload.services,
        }
    if payload.address:
        addr = (
            await session.get(Address, branch.address_id) if branch.address_id else None
        )
        if addr is None:
            branch.address_id = (await _new_address(session, payload.address)).id
        else:
            for k, v in payload.address.model_dump(exclude_unset=True).items():
                if not (k == "country" and v is None):
                    setattr(addr, k, v)
    if "pastorId" in payload.model_fields_set:  # "" clears
        await _set_branch_pastor(session, branch, branch.church_id, payload.pastorId)
    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "UPDATE_BRANCH",
        "BRANCH",
        branch.id,
        actor_user_id=current_user.id,
        church_id=branch.church_id,
    )
    return {"message": "Branch updated successfully"}


@router.delete("/branches/{branch_id}")
async def delete_branch(
    branch_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentSuperAdmin,
    session: DbSession,
):
    branch = (
        await session.execute(
            select(Branch).where(Branch.id == branch_id, Branch.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
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
        church_id=branch.church_id,
    )
    return {"message": "Branch deleted successfully"}


# ============================================================================
# USERS MANAGEMENT (logins). Names, churches and roles come from Member profiles.
# ============================================================================
def _user_name(user: User, member: Optional[Member]) -> str:
    return (
        (member.full_name if member else None)
        or user.display_name
        or user.email.split("@")[0]
    )


@router.get("/users")
async def get_all_users(
    current_user: CurrentSuperAdmin,
    session: DbSession,
    search: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    churchId: Optional[str] = None,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    conds = [User.deleted_at.is_(None)]
    if status and status != "all":
        try:
            conds.append(User.status == AccountStatus[status.upper()])
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Unknown status '{status}'")
    if churchId and churchId != "all":
        conds.append(
            User.id.in_(
                select(Member.user_id).where(
                    Member.church_id == churchId,
                    Member.deleted_at.is_(None),
                    Member.user_id.is_not(None),
                )
            )
        )
    if search:
        q = f"%{search.strip().lower()}%"
        conds.append(
            or_(
                func.lower(User.email).like(q),
                func.lower(func.coalesce(User.display_name, "")).like(q),
                User.id.in_(
                    select(Member.user_id).where(
                        Member.user_id.is_not(None),
                        or_(
                            func.lower(Member.first_name).like(q),
                            func.lower(Member.last_name).like(q),
                        ),
                    )
                ),
            )
        )
    users = (
        (
            await session.execute(
                select(User)
                .where(*conds)
                .order_by(User.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    latest: Dict[str, tuple] = {}
    if users:
        rows = (
            await session.execute(
                select(Member, Church)
                .join(Church, Church.id == Member.church_id)
                .where(
                    Member.user_id.in_([u.id for u in users]),
                    Member.deleted_at.is_(None),
                    Church.deleted_at.is_(None),
                )
                .order_by(Member.created_at.desc(), Member.id)
            )
        ).all()
        for m, ch in rows:
            latest.setdefault(m.user_id, (m, ch))
    roles_by_member = await role_names_for_members(
        session, [m.id for m, _ in latest.values()]
    )
    result = []
    for u in users:
        m, ch = latest.get(u.id, (None, None))
        names = roles_by_member.get(m.id, []) if m else []
        role_str = (
            "SUPER_ADMIN"
            if u.platform_role == PlatformRole.SUPER_ADMIN
            else (names[0] if names else "MEMBER")
        )
        item = {
            "id": str(u.id),
            "name": _user_name(u, m),
            "email": u.email,
            "phone": u.phone_number or "",
            "role": role_str,
            "platformRole": u.platform_role.value,
            "church": ch.name if ch else "Global / Platform",
            "churchId": str(ch.id) if ch else None,
            "status": u.status.value.lower(),
            "lastLogin": (
                u.last_login.strftime("%b %d, %H:%M") if u.last_login else "Never"
            ),
            "joinDate": u.created_at.strftime("%Y-%m-%d") if u.created_at else "",
            "permissions": (
                ["manage_members", "manage_events", "manage_finance"]
                if "ADMIN" in role_str
                else ["view_events"]
            ),
            "avatar": u.profile_picture_url or "/placeholder.svg?height=40&width=40",
        }
        if role and role != "all" and role.lower() not in role_str.lower():
            continue  # note: filters this page only
        result.append(item)
    return {"users": result}


@router.post("/users")
async def create_user(
    payload: SuperAdminUserCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentSuperAdmin,
    session: DbSession,
):
    """
    Platform super-admin: a login only. Anyone else: a login plus a Member profile
    (church + branch) holding the church role (default ADMIN).
    """
    email = payload.email.strip().lower()
    if (
        await session.execute(select(User.id).where(func.lower(User.email) == email))
    ).first():
        raise HTTPException(
            status_code=400, detail="User with this email already exists"
        )
    if (
        payload.phoneNumber
        and (
            await session.execute(
                select(User.id).where(User.phone_number == payload.phoneNumber)
            )
        ).first()
    ):
        raise HTTPException(
            status_code=400, detail="User with this phone number already exists"
        )
    if not payload.password:
        # Invitation e-mails are not implemented yet, so a password must be supplied.
        raise HTTPException(status_code=400, detail="password is required")
    church = branch = None
    roles: list = []
    if payload.platformRole != PlatformRole.SUPER_ADMIN:
        church = await _get_church(session, payload.churchId)
        branch = (
            await session.execute(
                select(Branch).where(
                    Branch.id == payload.branchId,
                    Branch.church_id == church.id,
                    Branch.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not branch:
            raise HTTPException(
                status_code=404, detail="Branch not found in this church"
            )
        roles = await resolve_roles(session, church.id, [payload.role or "ADMIN"])
    user = User(
        email=email,
        display_name=f"{payload.firstName} {payload.lastName}".strip(),
        phone_number=payload.phoneNumber,
        hashed_password=get_password_hash(payload.password),
        platform_role=payload.platformRole,
        status=AccountStatus.ACTIVE,
    )
    session.add(user)
    await session.flush()
    mem_number = None
    if church:
        role_names = [r.name for r in roles]
        mem_number = await generate_unique_membership_number(
            session=session,
            church_id=church.id,
            church=church,
            roles=role_names,
            position="admin",
            member_type=MemberType.MEMBER,
        )
        member = Member(
            church_id=church.id,
            branch_id=branch.id,
            user_id=user.id,
            first_name=payload.firstName.strip(),
            last_name=payload.lastName.strip(),
            email=email,
            phone_number=payload.phoneNumber,
            position="admin",
            membership_number=mem_number,
        )
        session.add(member)
        await session.flush()
        await set_member_roles(session, member.id, roles, replace=False)
        if any(n.upper() in ADMIN_ROLE_NAMES for n in role_names):
            await _upsert_detail(
                session, AdminDetails, member.id, {}, {"access_level": "NATIONAL"}
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
        "CREATE_USER",
        "USER",
        user.id,
        actor_user_id=current_user.id,
        church_id=church.id if church else None,
        metadata_json={"platformRole": payload.platformRole.value},
    )
    return {
        "id": str(user.id),
        "name": user.display_name,
        "email": user.email,
        "role": (
            "SUPER_ADMIN"
            if payload.platformRole == PlatformRole.SUPER_ADMIN
            else (payload.role or "ADMIN")
        ),
        "platformRole": payload.platformRole.value,
        "membershipNumber": mem_number,
        "memberId": mem_number or str(user.id),
        "status": "active",
    }


def _not_self(current: User, target_id: str, what: str) -> None:
    if current.id == target_id:
        raise HTTPException(
            status_code=400, detail=f"You cannot {what} your own account"
        )


@router.put("/users/{user_id}")
async def update_user(
    user_id: str,
    payload: SuperAdminUserUpdateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentSuperAdmin,
    session: DbSession,
):
    """
    Login fields (email, phone, status, platform role) change the User. Names and the church role
    change the Member profile of ONE church: `churchId` selects which (required if the user has several).
    """
    user = (
        await session.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.email is not None and payload.email.lower() != user.email.lower():
        if (
            await session.execute(
                select(User.id).where(
                    func.lower(User.email) == payload.email.lower(), User.id != user.id
                )
            )
        ).first():
            raise HTTPException(status_code=409, detail="Email is already in use")
        user.email = payload.email.lower()
    if payload.phoneNumber is not None and payload.phoneNumber != user.phone_number:
        if (
            await session.execute(
                select(User.id).where(
                    User.phone_number == payload.phoneNumber, User.id != user.id
                )
            )
        ).first():
            raise HTTPException(
                status_code=409, detail="Phone number is already in use"
            )
        user.phone_number = payload.phoneNumber
    if payload.status is not None and payload.status != user.status:
        _not_self(current_user, user.id, "change the status of")
        user.status = payload.status
        if payload.status != AccountStatus.ACTIVE:
            await _invalidate_sessions(session, user.id)
    if payload.platformRole is not None and payload.platformRole != user.platform_role:
        _not_self(current_user, user.id, "change the platform role of")
        user.platform_role = payload.platformRole
    wants_member_change = any(
        v is not None
        for v in (payload.firstName, payload.lastName, payload.role, payload.branchId)
    )
    if wants_member_change:
        q = select(Member).where(Member.user_id == user.id, Member.deleted_at.is_(None))
        if payload.churchId:
            q = q.where(Member.church_id == payload.churchId)
        members = (await session.execute(q)).scalars().all()
        if not members:
            raise HTTPException(
                status_code=404, detail="No church membership found for this user"
            )
        if len(members) > 1:
            raise HTTPException(
                status_code=400,
                detail="User belongs to several churches: send churchId",
            )
        member = members[0]
        if payload.firstName:
            member.first_name = payload.firstName.strip()
        if payload.lastName:
            member.last_name = payload.lastName.strip()
        if payload.firstName or payload.lastName:
            user.display_name = member.full_name
        if payload.branchId and payload.branchId != member.branch_id:
            b = (
                await session.execute(
                    select(Branch).where(
                        Branch.id == payload.branchId,
                        Branch.church_id == member.church_id,
                        Branch.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if not b:
                raise HTTPException(
                    status_code=404, detail="Branch not found in this church"
                )
            member.branch_id = b.id
        if payload.role:
            await set_member_roles(
                session,
                member.id,
                await resolve_roles(session, member.church_id, [payload.role]),
                replace=True,
            )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Conflict: duplicate email or phone"
        )
    queue_audit(
        background_tasks,
        request,
        "UPDATE_USER",
        "USER",
        user.id,
        actor_user_id=current_user.id,
    )
    return {"message": "User updated successfully"}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentSuperAdmin,
    session: DbSession,
):
    """Soft-delete a login and end all its sessions. Church membership records are kept."""
    _not_self(current_user, user_id, "delete")
    user = (
        await session.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.soft_delete()
    await _invalidate_sessions(session, user.id)
    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "DELETE_USER",
        "USER",
        user.id,
        actor_user_id=current_user.id,
    )
    return {"message": "User deleted successfully"}


@router.post("/users/{user_id}/status")
async def toggle_user_status(
    user_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentSuperAdmin,
    session: DbSession,
):
    """Toggle ACTIVE <-> SUSPENDED. Suspending also ends the user's sessions."""
    _not_self(current_user, user_id, "suspend")
    user = (
        await session.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.status == AccountStatus.ACTIVE:
        user.status = AccountStatus.SUSPENDED
        await _invalidate_sessions(session, user.id)
    else:
        user.status = AccountStatus.ACTIVE
    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "TOGGLE_USER_STATUS",
        "USER",
        user.id,
        actor_user_id=current_user.id,
        metadata_json={"status": user.status.value},
    )
    return {"status": user.status.value}


# ============================================================================
# ANALYTICS & METRICS
# ============================================================================
@router.get("/analytics")
async def get_analytics(
    current_user: CurrentSuperAdmin,
    session: DbSession,
    timeRange: Optional[str] = Query("7days"),
):
    """
    REAL: metrics, church distribution, top churches by members, regional counts.
    PLACEHOLDER (synthetic, derived from totals, NOT historical data): membershipGrowthData,
    revenueData, attendanceData, planDistribution and every "growth" value. Replace with
    group-by-month queries on Member.created_at / Offering.received_at / Attendance before trusting them.
    """
    churches = (
        (await session.execute(select(Church).where(Church.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    members_by_church = await _member_counts_by_church(session)
    members_count = sum(members_by_church.get(str(c.id), 0) for c in churches)
    churches_count = len(churches)
    branches_count = sum((await _branch_counts_by_church(session)).values())
    revenue_by_cur = await _revenue(session)
    revenue_sum = revenue_by_cur.get(DEFAULT_CURRENCY, 0.0)
    avg_church_size = round(members_count / churches_count) if churches_count else 0
    metrics = {
        "totalGrowthRate": 12.4,
        "avgChurchSize": avg_church_size,
        "revenuePerMember": (
            round(revenue_sum / members_count, 2) if members_count else 0.0
        ),
        "totalRevenue": revenue_sum,
        "revenueByCurrency": revenue_by_cur,
        "totalChurches": churches_count,
        "totalMembers": members_count,
        "totalBranches": branches_count,
        "globalReach": max(1, churches_count),
        "currency": DEFAULT_CURRENCY,
    }
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul"]
    base_mems = max(10, members_count - 60)
    growth_data = [
        {
            "month": m,
            "members": base_mems + i * 10,
            "newMembers": 10 + i * 2,
            "churches": max(1, churches_count),
        }
        for i, m in enumerate(months)
    ]
    base_rev = max(50000.0, revenue_sum if revenue_sum > 0 else 150000.0)
    shape = [
        (0.70, 0.55, 0.15),
        (0.75, 0.58, 0.17),
        (0.82, 0.64, 0.18),
        (0.88, 0.68, 0.20),
        (0.92, 0.71, 0.21),
        (0.96, 0.74, 0.22),
        (1.0, 0.76, 0.24),
    ]
    revenue_data = [
        {
            "month": m,
            "revenue": round(base_rev * t),
            "offerings": round(base_rev * o),
            "tithes": round(base_rev * ti),
        }
        for m, (t, o, ti) in zip(months, shape)
    ]
    palette = [
        "#8884d8",
        "#82ca9d",
        "#ffc658",
        "#ff7300",
        "#0088fe",
        "#00c49f",
        "#e91e63",
    ]
    denoms = (
        await session.execute(
            select(Church.denomination, func.count(Church.id))
            .where(Church.deleted_at.is_(None))
            .group_by(Church.denomination)
        )
    ).all()
    church_distribution = [
        {
            "name": d or "Non-denominational",
            "value": n,
            "color": palette[i % len(palette)],
        }
        for i, (d, n) in enumerate(denoms)
    ] or [
        {
            "name": "Christian / General",
            "value": max(1, churches_count),
            "color": palette[0],
        }
    ]
    total_capacity = (
        await session.execute(
            select(func.sum(Branch.capacity)).where(Branch.deleted_at.is_(None))
        )
    ).scalar() or max(500, churches_count * 500)
    avg_att = max(members_count, int(total_capacity * 0.7))
    day_shape = [
        ("Sunday", 0.95),
        ("Monday", 0.18),
        ("Tuesday", 0.24),
        ("Wednesday", 0.58),
        ("Thursday", 0.20),
        ("Friday", 0.38),
        ("Saturday", 0.32),
    ]
    attendance_data = [
        {"day": d, "attendance": int(avg_att * f), "capacity": total_capacity}
        for d, f in day_shape
    ]
    per_church_rev = {
        str(cid): float(total or 0)
        for cid, total in (
            await session.execute(
                select(Offering.church_id, func.sum(Offering.amount))
                .where(
                    Offering.deleted_at.is_(None), Offering.currency == DEFAULT_CURRENCY
                )
                .group_by(Offering.church_id)
            )
        ).all()
    }
    top = sorted(
        churches, key=lambda c: members_by_church.get(str(c.id), 0), reverse=True
    )[:5]
    top_perf = [
        {
            "name": c.name,
            "growth": 14.5,
            "members": members_by_church.get(str(c.id), 0),
            "revenue": per_church_rev.get(str(c.id), 0.0),
            "revenueFormatted": f"{DEFAULT_CURRENCY} {per_church_rev.get(str(c.id), 0.0):,.2f}",
        }
        for c in top
    ]
    countries = (
        await session.execute(
            select(Address.country, func.count(Church.id))
            .join(Church, Church.address_id == Address.id)
            .where(Church.deleted_at.is_(None))
            .group_by(Address.country)
        )
    ).all()
    regional = [
        {
            "region": c or "Kenya",
            "churches": n,
            "members": n * max(50, avg_church_size),
            "growth": 15.2,
        }
        for c, n in countries
    ] or [
        {
            "region": "Kenya",
            "churches": churches_count,
            "members": members_count,
            "growth": 12.4,
        }
    ]
    return {
        "metrics": metrics,
        "membershipGrowthData": growth_data,
        "revenueData": revenue_data,
        "planDistribution": [
            {"name": "Basic", "value": 45, "color": "#8884d8"},
            {"name": "Ministry", "value": 35, "color": "#82ca9d"},
            {"name": "Cathedral", "value": 15, "color": "#ffc658"},
            {"name": "Custom", "value": 5, "color": "#ff8042"},
        ],
        "churchDistributionData": church_distribution,
        "attendanceData": attendance_data,
        "topPerformingChurches": top_perf,
        "regionalData": regional,
    }


# ============================================================================
# SYSTEM HEALTH
# ============================================================================
@router.get("/system-health")
async def get_system_health(current_user: CurrentSuperAdmin, session: DbSession):
    """dbLatencyMs and uptime are measured; the percentage 'value's are placeholders."""
    t0 = time.time()
    await session.execute(select(func.now()))
    db_latency_ms = round((time.time() - t0) * 1000, 2)
    uptime_hours = round((time.time() - START_TIME) / 3600, 2)
    return {
        "status": "healthy",
        "dbLatencyMs": db_latency_ms,
        "uptimeHours": uptime_hours,
        "metrics": [
            {
                "name": "Server Uptime",
                "value": 99.98,
                "status": "excellent",
                "description": f"Running smoothly for {uptime_hours}h",
                "target": 99.9,
            },
            {
                "name": "Database Performance",
                "value": 96.5,
                "status": "good" if db_latency_ms < 200 else "warning",
                "description": f"PostgreSQL latency: {db_latency_ms}ms",
                "target": 95.0,
            },
            {
                "name": "API Response Time",
                "value": 92.4,
                "status": "good",
                "description": f"Average latency: {db_latency_ms + 15}ms",
                "target": 90.0,
            },
            {
                "name": "Network Latency",
                "value": 95.1,
                "status": "good",
                "description": "Average roundtrip: ~30ms",
                "target": 90.0,
            },
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# GLOBAL SETTINGS
# ============================================================================
@router.get("/settings")
async def get_global_settings(current_user: CurrentSuperAdmin):
    return GLOBAL_SYSTEM_SETTINGS


@router.put("/settings")
async def update_global_settings(
    payload: GlobalSettingsUpdateRequest, current_user: CurrentSuperAdmin
):
    for key in (
        "siteName",
        "supportEmail",
        "defaultCurrency",
        "trialPeriodDays",
        "maintenanceMode",
        "allowNewRegistrations",
        "sessionTimeoutMinutes",
    ):
        value = getattr(payload, key)
        if value is not None:
            GLOBAL_SYSTEM_SETTINGS[key] = value
    if payload.emailSettings is not None:
        GLOBAL_SYSTEM_SETTINGS["emailSettings"].update(payload.emailSettings)
    if payload.securitySettings is not None:
        GLOBAL_SYSTEM_SETTINGS["securitySettings"].update(payload.securitySettings)
    return {
        "message": "Settings saved successfully",
        "settings": GLOBAL_SYSTEM_SETTINGS,
    }

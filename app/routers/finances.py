from datetime import datetime, timezone
from decimal import Decimal
import math
from typing import Any, Dict, List, Optional

from app.core.deps import (
    CurrentUser,
    DbSession,
    church_id_from,
    resolve_actor,
)
from app.enums import (
    OfferingType,
    PaymentMethod,
    PaymentSchedule,
    PledgeStatus,
)
from app.models.church import Branch
from app.models.finance import Expense, Fund, Offering, Pledge
from app.models.member import Member
from app.models.user import User
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


async def _get_or_create_default_fund(session: DbSession, church_id: str) -> str:
    f = (
        await session.execute(
            select(Fund.id).where(Fund.church_id == church_id, Fund.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if f:
        return str(f)
    fund = Fund(name="General Fund", church_id=church_id, is_active=True)
    session.add(fund)
    await session.commit()
    await session.refresh(fund)
    return str(fund.id)


def _format_offering(offering: Offering, m: Optional[Member] = None, u: Optional[User] = None) -> Dict[str, Any]:
    member_name = f"{u.first_name} {u.last_name}".strip() if u else "Anonymous"
    return {
        "_id": str(offering.id),
        "id": str(offering.id),
        "amount": float(offering.amount),
        "currency": offering.currency or "KES",
        "date": _iso(offering.received_at),
        "received_at": _iso(offering.received_at),
        "type": offering.type.value if hasattr(offering.type, "value") else str(offering.type),
        "method": offering.payment_method.value if hasattr(offering.payment_method, "value") else str(offering.payment_method),
        "paymentMethod": offering.payment_method.value if hasattr(offering.payment_method, "value") else str(offering.payment_method),
        "member": member_name,
        "memberId": str(m.id) if m else None,
        "fundId": str(offering.fund_id),
        "pledgeId": str(offering.pledge_id) if offering.pledge_id else None,
        "status": "Completed",
        "createdAt": _iso(offering.created_at),
    }


def _format_pledge(pledge: Pledge, m: Optional[Member] = None, u: Optional[User] = None) -> Dict[str, Any]:
    member_name = f"{u.first_name} {u.last_name}".strip() if u else "Member"
    return {
        "_id": str(pledge.id),
        "id": str(pledge.id),
        "member": member_name,
        "memberId": str(m.id) if m else str(pledge.member_id),
        "amount": float(pledge.amount),
        "goal": float(pledge.amount),
        "current": 0,
        "paid": 0,
        "remaining": float(pledge.amount),
        "currency": pledge.currency or "KES",
        "purpose": pledge.purpose or "General Purpose",
        "name": pledge.purpose or "Pledge",
        "status": pledge.status.value.capitalize() if hasattr(pledge.status, "value") else str(pledge.status),
        "dueDate": _iso(pledge.end_date) if pledge.end_date else _iso(pledge.start_date),
        "deadline": _iso(pledge.end_date) if pledge.end_date else _iso(pledge.start_date),
        "frequency": "Monthly",
        "createdAt": _iso(pledge.created_at),
    }


class OfferingPayload(BaseModel):
    memberId: Optional[str] = None
    member_id: Optional[str] = None
    type: Optional[str] = "OFFERING"
    amount: Any = 0
    method: Optional[str] = "CASH"
    payment_method: Optional[str] = None
    date: Optional[str] = None
    received_at: Optional[datetime] = None
    fundId: Optional[str] = None
    fund_id: Optional[str] = None
    pledgeId: Optional[str] = None
    pledge_id: Optional[str] = None
    branchId: Optional[str] = None
    branch_id: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    churchId: Optional[str] = None
    church_id: Optional[str] = None


class PledgePayload(BaseModel):
    memberId: Optional[str] = None
    member_id: Optional[str] = None
    amount: Any = 0
    purpose: Optional[str] = None
    dueDate: Optional[str] = None
    end_date: Optional[datetime] = None
    startDate: Optional[str] = None
    start_date: Optional[datetime] = None
    paymentSchedule: Optional[str] = "MONTHLY"
    notes: Optional[str] = None
    branchId: Optional[str] = None
    branch_id: Optional[str] = None
    churchId: Optional[str] = None
    church_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Funds
# ---------------------------------------------------------------------------
@router.get("/funds")
async def get_funds(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
    include_inactive: bool = Query(False),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Fund.church_id == actor.church_id]
    if not include_inactive:
        conds.append(Fund.is_active.is_(True))
    funds = (
        (await session.execute(select(Fund).where(*conds).order_by(Fund.name)))
        .scalars()
        .all()
    )
    return [
        {
            "_id": str(f.id),
            "id": str(f.id),
            "name": f.name,
            "isActive": f.is_active,
            "createdAt": _iso(f.created_at),
        }
        for f in funds
    ]


@router.post("/funds")
async def create_fund(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any],
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Fund name is required")
    fund = Fund(
        church_id=actor.church_id,
        name=name,
        is_active=payload.get("isActive", True),
    )
    session.add(fund)
    await session.commit()
    await session.refresh(fund)
    return {
        "_id": str(fund.id),
        "id": str(fund.id),
        "name": fund.name,
        "isActive": fund.is_active,
    }


# ---------------------------------------------------------------------------
# Offerings
# ---------------------------------------------------------------------------
@router.get("/offerings")
async def get_offerings(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
    branch_id: Optional[str] = Query(None),
    member_id: Optional[str] = Query(None),
    fund_id: Optional[str] = Query(None),
    search: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    offset: Optional[int] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Offering.church_id == actor.church_id, Offering.deleted_at.is_(None)]
    if branch_id:
        conds.append(Offering.branch_id == branch_id)
    if member_id:
        conds.append(Offering.member_id == member_id)
    if fund_id:
        conds.append(Offering.fund_id == fund_id)

    total = (
        await session.execute(select(func.count(Offering.id)).where(*conds))
    ).scalar() or 0

    if offset is None:
        offset = (page - 1) * limit

    rows = (
        (
            await session.execute(
                select(Offering, Member, User)
                .outerjoin(Member, Member.id == Offering.member_id)
                .outerjoin(User, User.id == Member.user_id)
                .where(*conds)
                .order_by(Offering.received_at.desc(), Offering.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .all()
    )

    formatted = [_format_offering(off, m, u) for off, m, u in rows]
    pages = math.ceil(total / limit) if limit else 1
    response.headers["X-Total-Count"] = str(total)

    return {
        "offerings": formatted,
        "data": formatted,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": pages,
            "hasNext": page < pages,
            "hasPrev": page > 1,
        },
    }


@router.post("/offerings")
async def create_offering(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: OfferingPayload,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, payload.churchId or payload.church_id or church_id)
    )
    church_id = actor.church_id

    fund_id = payload.fundId or payload.fund_id
    if not fund_id:
        fund_id = await _get_or_create_default_fund(session, church_id)

    mem_id = payload.memberId or payload.member_id
    if mem_id:
        mem_exists = (
            await session.execute(
                select(Member.id).where(Member.id == mem_id, Member.church_id == church_id)
            )
        ).scalar_one_or_none()
        if not mem_exists:
            mem_id = None

    try:
        amt = Decimal(str(payload.amount))
    except Exception:
        amt = Decimal("0.00")

    raw_type = (payload.type or "OFFERING").upper()
    off_type = OfferingType[raw_type] if raw_type in OfferingType.__members__ else OfferingType.OFFERING

    raw_method = (payload.method or payload.payment_method or "CASH").upper().replace("-", "_").replace(" ", "_")
    if "MPESA" in raw_method or "M_PESA" in raw_method:
        pay_method = PaymentMethod.MOBILE_MONEY
    elif raw_method in PaymentMethod.__members__:
        pay_method = PaymentMethod[raw_method]
    else:
        pay_method = PaymentMethod.CASH

    received_dt = payload.received_at
    if not received_dt and payload.date:
        try:
            received_dt = datetime.strptime(payload.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            received_dt = _now()
    if not received_dt:
        received_dt = _now()

    offering = Offering(
        amount=amt,
        currency="KES",
        received_at=received_dt,
        type=off_type,
        payment_method=pay_method,
        church_id=church_id,
        branch_id=payload.branchId or payload.branch_id,
        member_id=mem_id,
        fund_id=fund_id,
        pledge_id=payload.pledgeId or payload.pledge_id,
    )
    session.add(offering)
    await session.commit()
    await session.refresh(offering)

    queue_audit(
        background_tasks,
        request,
        "RECORD_OFFERING",
        "OFFERING",
        offering.id,
        actor_user_id=current_user.id,
        church_id=church_id,
    )

    formatted = _format_offering(offering)
    return {
        "status": "success",
        "message": "Offering recorded successfully",
        "offering": formatted,
        "data": formatted,
    }


@router.delete("/offerings/{offering_id}")
async def delete_offering(
    offering_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    off = (
        await session.execute(
            select(Offering).where(
                Offering.id == offering_id, Offering.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not off:
        raise HTTPException(status_code=404, detail="Offering not found")

    await session.delete(off)
    await session.commit()
    return {"status": "success", "message": "Offering deleted successfully"}


# ---------------------------------------------------------------------------
# Pledges
# ---------------------------------------------------------------------------
@router.get("/pledges")
async def get_pledges(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
    branch_id: Optional[str] = Query(None),
    member_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    offset: Optional[int] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Pledge.church_id == actor.church_id, Pledge.deleted_at.is_(None)]
    if branch_id:
        conds.append(Pledge.branch_id == branch_id)
    if member_id:
        conds.append(Pledge.member_id == member_id)

    total = (
        await session.execute(select(func.count(Pledge.id)).where(*conds))
    ).scalar() or 0

    if offset is None:
        offset = (page - 1) * limit

    rows = (
        (
            await session.execute(
                select(Pledge, Member, User)
                .outerjoin(Member, Member.id == Pledge.member_id)
                .outerjoin(User, User.id == Member.user_id)
                .where(*conds)
                .order_by(Pledge.created_at.desc(), Pledge.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .all()
    )

    formatted = [_format_pledge(pl, m, u) for pl, m, u in rows]
    pages = math.ceil(total / limit) if limit else 1
    response.headers["X-Total-Count"] = str(total)

    return {
        "pledges": formatted,
        "data": formatted,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": pages,
            "hasNext": page < pages,
            "hasPrev": page > 1,
        },
    }


@router.post("/pledges")
async def create_pledge(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: PledgePayload,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, payload.churchId or payload.church_id or church_id)
    )
    church_id = actor.church_id

    fund_id = await _get_or_create_default_fund(session, church_id)

    mem_id = payload.memberId or payload.member_id
    if not mem_id:
        # fallback to first member of church or actor
        mem_id = (
            await session.execute(
                select(Member.id).where(Member.church_id == church_id).limit(1)
            )
        ).scalar_one_or_none()
    if not mem_id:
        raise HTTPException(status_code=400, detail="Member is required for pledge")

    try:
        amt = Decimal(str(payload.amount))
    except Exception:
        amt = Decimal("0.00")

    end_dt = payload.end_date
    if not end_dt and payload.dueDate:
        try:
            end_dt = datetime.strptime(payload.dueDate, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            end_dt = None

    pledge = Pledge(
        amount=amt,
        currency="KES",
        purpose=payload.purpose or "General Pledge",
        status=PledgeStatus.ACTIVE,
        start_date=_now(),
        end_date=end_dt,
        church_id=church_id,
        branch_id=payload.branchId or payload.branch_id,
        member_id=mem_id,
        fund_id=fund_id,
    )
    session.add(pledge)
    await session.commit()
    await session.refresh(pledge)

    queue_audit(
        background_tasks,
        request,
        "CREATE_PLEDGE",
        "PLEDGE",
        pledge.id,
        actor_user_id=current_user.id,
        church_id=church_id,
    )

    formatted = _format_pledge(pledge)
    return {
        "status": "success",
        "message": "Pledge created successfully",
        "pledge": formatted,
        "data": formatted,
    }


@router.delete("/pledges/{pledge_id}")
async def delete_pledge(
    pledge_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    pledge = (
        await session.execute(
            select(Pledge).where(
                Pledge.id == pledge_id, Pledge.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not pledge:
        raise HTTPException(status_code=404, detail="Pledge not found")

    await session.delete(pledge)
    await session.commit()
    return {"status": "success", "message": "Pledge deleted successfully"}


# ---------------------------------------------------------------------------
# Contributions and Financial Summary
# ---------------------------------------------------------------------------
@router.get("/summary")
async def get_financial_summary(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    total_offerings = (
        await session.execute(
            select(func.coalesce(func.sum(Offering.amount), 0)).where(
                Offering.church_id == actor.church_id, Offering.deleted_at.is_(None)
            )
        )
    ).scalar() or 0

    total_pledges = (
        await session.execute(
            select(func.coalesce(func.sum(Pledge.amount), 0)).where(
                Pledge.church_id == actor.church_id, Pledge.deleted_at.is_(None)
            )
        )
    ).scalar() or 0

    total_expenses = (
        await session.execute(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.church_id == actor.church_id, Expense.deleted_at.is_(None)
            )
        )
    ).scalar() or 0

    return {
        "totalOfferings": float(total_offerings),
        "totalTithes": float(total_offerings) * 0.6,
        "totalPledges": float(total_pledges),
        "monthlyGoal": 50000,
        "expenses": float(total_expenses),
        "netIncome": float(total_offerings) - float(total_expenses),
    }

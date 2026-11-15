from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
from typing import Any, Dict, List, Optional
import uuid

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
from app.models.church import Branch, Church
from app.models.finance import Expense, Fund, Offering, Pledge
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


async def _get_or_create_default_fund(session: DbSession, church_id: str) -> str:
    f = (
        await session.execute(
            select(Fund.id)
            .where(Fund.church_id == church_id, Fund.is_active.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    if f:
        return str(f)
    fund = Fund(name="General Fund", church_id=church_id, is_active=True)
    session.add(fund)
    await session.commit()
    await session.refresh(fund)
    return str(fund.id)


_OFFERING_TYPE_LABELS = {
    "TITHE": "Tithe",
    "OFFERING": "Offering",
    "SPECIAL_OFFERING": "Special Offering",
    "SPECIAL_GIVING": "Special Offering",
    "BUILDING_FUND": "Building Fund",
    "MISSION": "Mission Fund",
    "MISSIONS": "Mission Fund",
    "THANKSGIVING": "Thanksgiving",
    "PARTNERSHIP": "Partnership",
    "DONATION": "Donation",
    "OTHER": "Other",
}

_PAYMENT_METHOD_LABELS = {
    "CASH": "Cash",
    "M_PESA": "M-Pesa",
    "BANK_TRANSFER": "Bank Transfer",
    "CARD": "Card",
    "CHEQUE": "Check",
    "ONLINE": "Online",
    "OTHER": "Other",
}

_PLEDGE_PURPOSE_LABELS = {
    "building-fund": "Building Fund",
    "mission-trip": "Mission Trip",
    "youth-program": "Youth Program",
    "equipment": "Equipment Purchase",
    "outreach": "Community Outreach",
    "education": "Education Fund",
    "other": "Other",
}


def _format_offering(
    offering: Offering,
    m: Optional[Member] = None,
    u: Optional[User] = None,
    meta_entry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    extra = dict(meta_entry or {})
    member_name = (
        f"{m.first_name or ''} {m.last_name or ''}".strip()
        if m
        else (
            u.display_name
            if u and u.display_name
            else (extra.get("memberName") or "Anonymous")
        )
    ) or "Anonymous"

    raw_t = (
        offering.type.value
        if hasattr(offering.type, "value")
        else str(offering.type)
    )
    raw_m = (
        offering.payment_method.value
        if hasattr(offering.payment_method, "value")
        else str(offering.payment_method)
    )
    type_label = extra.get("typeLabel") or _OFFERING_TYPE_LABELS.get(raw_t, raw_t.replace("_", " ").title())
    method_label = extra.get("methodLabel") or _PAYMENT_METHOD_LABELS.get(raw_m, raw_m.replace("_", " ").title())
    ref_code = extra.get("reference") or f"TX-{str(offering.id)[:8].upper()}"

    return {
        "_id": str(offering.id),
        "id": str(offering.id),
        "amount": float(offering.amount),
        "currency": offering.currency or "KES",
        "date": _iso(offering.received_at),
        "received_at": _iso(offering.received_at),
        "type": type_label,
        "rawType": raw_t,
        "method": method_label,
        "paymentMethod": method_label,
        "member": member_name,
        "memberId": str(m.id) if m else (str(offering.member_id) if offering.member_id else None),
        "fundId": str(offering.fund_id),
        "pledgeId": str(offering.pledge_id) if offering.pledge_id else None,
        "reference": ref_code,
        "notes": extra.get("notes") or "",
        "status": extra.get("status") or "Completed",
        "createdAt": _iso(offering.created_at),
    }


def _format_pledge(
    pledge: Pledge,
    m: Optional[Member] = None,
    u: Optional[User] = None,
    paid_amount: float = 0.0,
    meta_entry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    extra = dict(meta_entry or {})
    member_name = (
        f"{m.first_name or ''} {m.last_name or ''}".strip()
        if m
        else (u.display_name if u and u.display_name else "Member")
    ) or "Member"

    total_amt = float(pledge.amount)
    paid_val = round(float(paid_amount or 0.0), 2)
    remaining_val = max(0.0, round(total_amt - paid_val, 2))

    raw_purpose = pledge.purpose or "General Purpose"
    purpose_label = _PLEDGE_PURPOSE_LABELS.get(raw_purpose.lower(), raw_purpose)

    status_str = (
        pledge.status.value.capitalize()
        if hasattr(pledge.status, "value")
        else str(pledge.status).capitalize()
    )
    if total_amt > 0 and remaining_val <= 0:
        status_str = "Completed"

    return {
        "_id": str(pledge.id),
        "id": str(pledge.id),
        "member": member_name,
        "memberId": str(m.id) if m else str(pledge.member_id),
        "amount": total_amt,
        "goal": total_amt,
        "current": paid_val,
        "paid": paid_val,
        "remaining": remaining_val,
        "currency": pledge.currency or "KES",
        "purpose": purpose_label,
        "name": purpose_label,
        "status": status_str,
        "dueDate": _iso(pledge.end_date) if pledge.end_date else _iso(pledge.start_date),
        "deadline": _iso(pledge.end_date) if pledge.end_date else _iso(pledge.start_date),
        "frequency": extra.get("paymentSchedule") or "Monthly",
        "paymentSchedule": extra.get("paymentSchedule") or "Monthly",
        "notes": extra.get("notes") or "",
        "createdAt": _iso(pledge.created_at),
    }


class OfferingPayload(BaseModel):
    memberId: Optional[str] = None
    member_id: Optional[str] = None
    type: Optional[str] = None
    amount: Any = None
    method: Optional[str] = None
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
    status: Optional[str] = None
    churchId: Optional[str] = None
    church_id: Optional[str] = None


class PledgePayload(BaseModel):
    memberId: Optional[str] = None
    member_id: Optional[str] = None
    amount: Any = None
    purpose: Optional[str] = None
    dueDate: Optional[str] = None
    end_date: Optional[datetime] = None
    startDate: Optional[str] = None
    start_date: Optional[datetime] = None
    paymentSchedule: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    branchId: Optional[str] = None
    branch_id: Optional[str] = None
    churchId: Optional[str] = None
    church_id: Optional[str] = None


class PledgePaymentPayload(BaseModel):
    amount: Any = 0
    method: Optional[str] = "M-Pesa"
    date: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None


class ExpensePayload(BaseModel):
    amount: Any = 0
    category: str
    description: Optional[str] = ""
    date: Optional[str] = None
    branchId: Optional[str] = None


class FinanceGoalPayload(BaseModel):
    monthlyGoal: Any = 50000


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
    period: Optional[str] = Query(None),
    search: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    offset: Optional[int] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church = await _get_church(session, actor.church_id)
    offering_meta_map: Dict[str, Any] = dict(
        (church.metadata_json or {}).get("offering_meta") or {}
    )

    conds = [Offering.church_id == actor.church_id, Offering.deleted_at.is_(None)]
    if branch_id:
        conds.append(Offering.branch_id == branch_id)
    if member_id:
        conds.append(Offering.member_id == member_id)
    if fund_id:
        conds.append(Offering.fund_id == fund_id)
    if period and period.lower() != "all":
        now_dt = _now()
        p_low = period.lower()
        if p_low == "week":
            conds.append(Offering.received_at >= now_dt - timedelta(days=7))
        elif p_low == "month":
            conds.append(Offering.received_at >= now_dt - timedelta(days=31))
        elif p_low == "quarter":
            conds.append(Offering.received_at >= now_dt - timedelta(days=92))
        elif p_low == "year":
            conds.append(Offering.received_at >= now_dt - timedelta(days=366))

    rows = (
        (
            await session.execute(
                select(Offering, Member, User)
                .outerjoin(Member, Member.id == Offering.member_id)
                .outerjoin(User, User.id == Member.user_id)
                .where(*conds)
                .order_by(Offering.received_at.desc(), Offering.id)
            )
        )
        .all()
    )

    all_formatted = [
        _format_offering(off, m, u, offering_meta_map.get(str(off.id)))
        for off, m, u in rows
    ]

    if search:
        s = search.strip().lower()
        all_formatted = [
            item
            for item in all_formatted
            if s in (item.get("member") or "").lower()
            or s in (item.get("type") or "").lower()
            or s in (item.get("method") or "").lower()
            or s in (item.get("reference") or "").lower()
        ]

    total = len(all_formatted)
    if offset is None:
        offset = (page - 1) * limit
    formatted = all_formatted[offset : offset + limit]
    pages = max(1, math.ceil(total / limit)) if limit else 1
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

    raw_mem_id = payload.memberId or payload.member_id
    mem_obj = None
    mem_id = None
    if raw_mem_id:
        mem_obj = (
            await session.execute(
                select(Member).where(
                    or_(Member.id == raw_mem_id, Member.user_id == raw_mem_id),
                    Member.church_id == church_id,
                    Member.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if mem_obj:
            mem_id = str(mem_obj.id)

    try:
        amt = Decimal(str(payload.amount))
    except Exception:
        amt = Decimal("0.00")

    raw_type = (payload.type or "OFFERING").upper().replace("-", "_").replace(" ", "_")
    off_type = OfferingType[raw_type] if raw_type in OfferingType.__members__ else OfferingType.OFFERING

    raw_method = (payload.method or payload.payment_method or "CASH").upper().replace("-", "_").replace(" ", "_")
    if "MPESA" in raw_method or "M_PESA" in raw_method or "MOBILE" in raw_method:
        pay_method = PaymentMethod.M_PESA
    elif "CHECK" in raw_method or "CHEQUE" in raw_method:
        pay_method = PaymentMethod.CHEQUE
    elif raw_method in PaymentMethod.__members__:
        pay_method = PaymentMethod[raw_method]
    else:
        pay_method = PaymentMethod.CASH

    received_dt = payload.received_at
    if not received_dt and payload.date:
        try:
            received_dt = datetime.strptime(payload.date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            received_dt = _now()
    if not received_dt:
        received_dt = _now()

    pledge_id = payload.pledgeId or payload.pledge_id

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
        pledge_id=pledge_id,
    )
    session.add(offering)
    await session.flush()

    church = await _get_church(session, church_id)
    meta = dict(church.metadata_json or {})
    offering_meta_map = dict(meta.get("offering_meta") or {})
    ref_code = (
        payload.reference.strip()
        if payload.reference and payload.reference.strip()
        else f"TX-{str(offering.id)[:8].upper()}"
    )
    meta_entry = {
        "reference": ref_code,
        "notes": (payload.notes or "").strip(),
        "status": payload.status or "Completed",
    }
    offering_meta_map[str(offering.id)] = meta_entry
    meta["offering_meta"] = offering_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

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

    formatted = _format_offering(offering, mem_obj, None, meta_entry)
    return {
        "status": "success",
        "message": "Offering recorded successfully",
        "offering": formatted,
        "data": formatted,
    }


@router.put("/offerings/{offering_id}")
@router.patch("/offerings/{offering_id}")
async def update_offering(
    offering_id: str,
    payload: OfferingPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    off = (
        await session.execute(
            select(Offering).where(
                Offering.id == offering_id,
                Offering.church_id == actor.church_id,
                Offering.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not off:
        raise HTTPException(status_code=404, detail="Offering not found")

    if payload.amount is not None and str(payload.amount) != "":
        try:
            off.amount = Decimal(str(payload.amount))
        except Exception:
            pass
    if payload.type:
        raw_t = payload.type.upper().replace("-", "_").replace(" ", "_")
        if raw_t in OfferingType.__members__:
            off.type = OfferingType[raw_t]
    if payload.method or payload.payment_method:
        raw_m = (payload.method or payload.payment_method or "").upper().replace("-", "_").replace(" ", "_")
        if "MPESA" in raw_m or "M_PESA" in raw_m:
            off.payment_method = PaymentMethod.M_PESA
        elif raw_m in PaymentMethod.__members__:
            off.payment_method = PaymentMethod[raw_m]
    if payload.date:
        try:
            off.received_at = datetime.strptime(payload.date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            pass

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    offering_meta_map = dict(meta.get("offering_meta") or {})
    entry = dict(offering_meta_map.get(str(off.id)) or {})
    if payload.reference is not None:
        entry["reference"] = payload.reference.strip() or f"TX-{str(off.id)[:8].upper()}"
    if payload.notes is not None:
        entry["notes"] = payload.notes.strip()
    if payload.status is not None:
        entry["status"] = payload.status
    offering_meta_map[str(off.id)] = entry
    meta["offering_meta"] = offering_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    await session.commit()
    await session.refresh(off)

    mem_obj = None
    if off.member_id:
        mem_obj = (
            await session.execute(select(Member).where(Member.id == off.member_id))
        ).scalar_one_or_none()

    formatted = _format_offering(off, mem_obj, None, entry)
    return {
        "status": "success",
        "message": "Offering updated successfully",
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

    off.soft_delete()
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
    search: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    offset: Optional[int] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church = await _get_church(session, actor.church_id)
    pledge_meta_map: Dict[str, Any] = dict(
        (church.metadata_json or {}).get("pledge_meta") or {}
    )

    conds = [Pledge.church_id == actor.church_id, Pledge.deleted_at.is_(None)]
    if branch_id:
        conds.append(Pledge.branch_id == branch_id)
    if member_id:
        conds.append(Pledge.member_id == member_id)

    rows = (
        (
            await session.execute(
                select(Pledge, Member, User)
                .outerjoin(Member, Member.id == Pledge.member_id)
                .outerjoin(User, User.id == Member.user_id)
                .where(*conds)
                .order_by(Pledge.created_at.desc(), Pledge.id)
            )
        )
        .all()
    )

    # Calculate paid amounts per pledge from non-deleted Offerings linked to pledge_id
    paid_rows = (
        await session.execute(
            select(Offering.pledge_id, func.coalesce(func.sum(Offering.amount), 0))
            .where(
                Offering.church_id == actor.church_id,
                Offering.deleted_at.is_(None),
                Offering.pledge_id.is_not(None),
            )
            .group_by(Offering.pledge_id)
        )
    ).all()
    paid_by_pledge = {str(pid): float(total_paid or 0) for pid, total_paid in paid_rows}

    all_formatted = [
        _format_pledge(
            pl,
            m,
            u,
            paid_by_pledge.get(str(pl.id), 0.0),
            pledge_meta_map.get(str(pl.id)),
        )
        for pl, m, u in rows
    ]

    if search:
        s = search.strip().lower()
        all_formatted = [
            item
            for item in all_formatted
            if s in (item.get("member") or "").lower()
            or s in (item.get("purpose") or "").lower()
            or s in (item.get("status") or "").lower()
        ]

    total = len(all_formatted)
    if offset is None:
        offset = (page - 1) * limit
    formatted = all_formatted[offset : offset + limit]
    pages = max(1, math.ceil(total / limit)) if limit else 1
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

    raw_mem_id = payload.memberId or payload.member_id
    mem_obj = None
    mem_id = None
    if raw_mem_id:
        mem_obj = (
            await session.execute(
                select(Member).where(
                    or_(Member.id == raw_mem_id, Member.user_id == raw_mem_id),
                    Member.church_id == church_id,
                    Member.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if mem_obj:
            mem_id = str(mem_obj.id)
    if not mem_id and actor.member:
        mem_obj = actor.member
        mem_id = str(actor.member.id)
    if not mem_id:
        mem_obj = (
            await session.execute(
                select(Member).where(
                    Member.church_id == church_id,
                    Member.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if mem_obj:
            mem_id = str(mem_obj.id)
    if not mem_id:
        raise HTTPException(status_code=400, detail="Member is required for pledge")

    try:
        amt = Decimal(str(payload.amount))
    except Exception:
        amt = Decimal("0.00")

    end_dt = payload.end_date
    if not end_dt and payload.dueDate:
        try:
            end_dt = datetime.strptime(payload.dueDate[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            end_dt = None

    raw_purpose = (payload.purpose or "General Pledge").strip()
    purpose_label = _PLEDGE_PURPOSE_LABELS.get(raw_purpose.lower(), raw_purpose)

    pledge = Pledge(
        amount=amt,
        currency="KES",
        purpose=purpose_label,
        status=PledgeStatus.ACTIVE,
        start_date=_now(),
        end_date=end_dt,
        church_id=church_id,
        branch_id=payload.branchId or payload.branch_id,
        member_id=mem_id,
        fund_id=fund_id,
    )
    session.add(pledge)
    await session.flush()

    church = await _get_church(session, church_id)
    meta = dict(church.metadata_json or {})
    pledge_meta_map = dict(meta.get("pledge_meta") or {})
    meta_entry = {
        "paymentSchedule": (payload.paymentSchedule or "Monthly").replace("-", " ").title(),
        "notes": (payload.notes or "").strip(),
    }
    pledge_meta_map[str(pledge.id)] = meta_entry
    meta["pledge_meta"] = pledge_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

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

    formatted = _format_pledge(pledge, mem_obj, None, 0.0, meta_entry)
    return {
        "status": "success",
        "message": "Pledge created successfully",
        "pledge": formatted,
        "data": formatted,
    }


@router.post("/pledges/{pledge_id}/payments")
async def record_pledge_payment(
    pledge_id: str,
    payload: PledgePaymentPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    pledge = (
        await session.execute(
            select(Pledge).where(
                Pledge.id == pledge_id,
                Pledge.church_id == actor.church_id,
                Pledge.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not pledge:
        raise HTTPException(status_code=404, detail="Pledge not found")

    try:
        pay_amt = Decimal(str(payload.amount))
    except Exception:
        pay_amt = Decimal("0.00")
    if pay_amt <= 0:
        raise HTTPException(status_code=400, detail="Payment amount must be greater than 0")

    raw_m = (payload.method or "M_PESA").upper().replace("-", "_").replace(" ", "_")
    if "MPESA" in raw_m or "M_PESA" in raw_m:
        pay_method = PaymentMethod.M_PESA
    elif raw_m in PaymentMethod.__members__:
        pay_method = PaymentMethod[raw_m]
    else:
        pay_method = PaymentMethod.CASH

    pay_dt = _now()
    if payload.date:
        try:
            pay_dt = datetime.strptime(payload.date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            pay_dt = _now()

    offering = Offering(
        amount=pay_amt,
        currency="KES",
        received_at=pay_dt,
        type=OfferingType.BUILDING_FUND
        if "building" in (pledge.purpose or "").lower()
        else OfferingType.SPECIAL_OFFERING,
        payment_method=pay_method,
        church_id=actor.church_id,
        branch_id=pledge.branch_id,
        member_id=pledge.member_id,
        fund_id=pledge.fund_id,
        pledge_id=pledge.id,
    )
    session.add(offering)
    await session.flush()

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    offering_meta_map = dict(meta.get("offering_meta") or {})
    ref_code = (
        payload.reference.strip()
        if payload.reference and payload.reference.strip()
        else f"PLG-{str(offering.id)[:8].upper()}"
    )
    offering_meta_map[str(offering.id)] = {
        "reference": ref_code,
        "notes": payload.notes or f"Pledge payment: {pledge.purpose}",
        "typeLabel": f"Pledge ({pledge.purpose})",
        "status": "Completed",
    }
    meta["offering_meta"] = offering_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    total_paid = (
        await session.execute(
            select(func.coalesce(func.sum(Offering.amount), 0)).where(
                Offering.pledge_id == pledge.id,
                Offering.deleted_at.is_(None),
            )
        )
    ).scalar() or Decimal("0.00")

    if total_paid >= pledge.amount:
        pledge.status = PledgeStatus.COMPLETED

    await session.commit()
    await session.refresh(pledge)

    mem_obj = (
        await session.execute(select(Member).where(Member.id == pledge.member_id))
    ).scalar_one_or_none()
    pledge_meta_map = dict(meta.get("pledge_meta") or {})
    formatted = _format_pledge(
        pledge,
        mem_obj,
        None,
        float(total_paid),
        pledge_meta_map.get(str(pledge.id)),
    )

    return {
        "status": "success",
        "message": "Pledge payment recorded successfully",
        "pledge": formatted,
        "data": formatted,
    }


@router.put("/pledges/{pledge_id}")
@router.patch("/pledges/{pledge_id}")
async def update_pledge(
    pledge_id: str,
    payload: PledgePayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    pledge = (
        await session.execute(
            select(Pledge).where(
                Pledge.id == pledge_id,
                Pledge.church_id == actor.church_id,
                Pledge.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not pledge:
        raise HTTPException(status_code=404, detail="Pledge not found")

    if payload.amount is not None and str(payload.amount) != "":
        try:
            pledge.amount = Decimal(str(payload.amount))
        except Exception:
            pass
    if payload.purpose is not None and payload.purpose.strip():
        raw_p = payload.purpose.strip()
        pledge.purpose = _PLEDGE_PURPOSE_LABELS.get(raw_p.lower(), raw_p)
    if payload.dueDate:
        try:
            pledge.end_date = datetime.strptime(payload.dueDate[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            pass
    if payload.status:
        raw_s = payload.status.upper()
        if raw_s in PledgeStatus.__members__:
            pledge.status = PledgeStatus[raw_s]

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    pledge_meta_map = dict(meta.get("pledge_meta") or {})
    entry = dict(pledge_meta_map.get(str(pledge.id)) or {})
    if payload.paymentSchedule:
        entry["paymentSchedule"] = payload.paymentSchedule.replace("-", " ").title()
    if payload.notes is not None:
        entry["notes"] = payload.notes.strip()
    pledge_meta_map[str(pledge.id)] = entry
    meta["pledge_meta"] = pledge_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    await session.commit()
    await session.refresh(pledge)

    total_paid = (
        await session.execute(
            select(func.coalesce(func.sum(Offering.amount), 0)).where(
                Offering.pledge_id == pledge.id,
                Offering.deleted_at.is_(None),
            )
        )
    ).scalar() or 0
    mem_obj = (
        await session.execute(select(Member).where(Member.id == pledge.member_id))
    ).scalar_one_or_none()

    formatted = _format_pledge(pledge, mem_obj, None, float(total_paid), entry)
    return {
        "status": "success",
        "message": "Pledge updated successfully",
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

    pledge.soft_delete()
    await session.commit()
    return {"status": "success", "message": "Pledge deleted successfully"}


# ---------------------------------------------------------------------------
# Expenses (/church/expenses)
# ---------------------------------------------------------------------------
@router.get("/expenses")
async def list_expenses(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    search: str = Query("", max_length=100),
    category: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Expense.church_id == actor.church_id, Expense.deleted_at.is_(None)]
    if category and category.lower() != "all":
        conds.append(Expense.category.ilike(category.strip()))
    if search:
        s = f"%{search.strip()}%"
        conds.append(or_(Expense.category.ilike(s), Expense.description.ilike(s)))

    rows = (
        await session.execute(
            select(Expense).where(*conds).order_by(Expense.incurred_at.desc())
        )
    ).scalars().all()

    formatted = [
        {
            "id": str(e.id),
            "_id": str(e.id),
            "amount": float(e.amount),
            "currency": e.currency or "KES",
            "category": e.category,
            "description": e.description or "",
            "date": _iso(e.incurred_at),
            "createdAt": _iso(e.created_at),
        }
        for e in rows
    ]
    return {"status": "success", "expenses": formatted, "data": formatted}


@router.post("/expenses")
async def create_expense(
    payload: ExpensePayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    try:
        amt = Decimal(str(payload.amount))
    except Exception:
        amt = Decimal("0.00")
    if amt <= 0:
        raise HTTPException(status_code=400, detail="Expense amount must be greater than 0")

    inc_dt = _now()
    if payload.date:
        try:
            inc_dt = datetime.strptime(payload.date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            inc_dt = _now()

    exp = Expense(
        amount=amt,
        currency="KES",
        category=(payload.category or "General").strip(),
        description=(payload.description or "").strip(),
        incurred_at=inc_dt,
        church_id=actor.church_id,
        branch_id=payload.branchId,
        approved_by_member_id=actor.member.id if actor.member else None,
    )
    session.add(exp)
    await session.commit()
    await session.refresh(exp)

    out = {
        "id": str(exp.id),
        "_id": str(exp.id),
        "amount": float(exp.amount),
        "currency": exp.currency or "KES",
        "category": exp.category,
        "description": exp.description or "",
        "date": _iso(exp.incurred_at),
        "createdAt": _iso(exp.created_at),
    }
    return {"status": "success", "message": "Expense recorded successfully", "expense": out, "data": out}


@router.delete("/expenses/{expense_id}")
async def delete_expense(
    expense_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    exp = (
        await session.execute(
            select(Expense).where(
                Expense.id == expense_id,
                Expense.church_id == actor.church_id,
                Expense.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not exp:
        raise HTTPException(status_code=404, detail="Expense not found")

    exp.soft_delete()
    await session.commit()
    return {"status": "success", "message": "Expense deleted successfully"}


# ---------------------------------------------------------------------------
# Contributions and Financial Summary
# ---------------------------------------------------------------------------
@router.put("/finance/goal")
async def update_monthly_goal(
    payload: FinanceGoalPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    fin_settings = dict(meta.get("finance_settings") or {})
    try:
        goal_val = float(payload.monthlyGoal or 50000)
    except Exception:
        goal_val = 50000.0
    fin_settings["monthlyGoal"] = goal_val
    meta["finance_settings"] = fin_settings
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {
        "status": "success",
        "message": "Monthly goal updated successfully",
        "monthlyGoal": goal_val,
    }


@router.get("/summary")
@router.get("/finance/summary")
async def get_financial_summary(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church = await _get_church(session, actor.church_id)
    fin_settings = dict((church.metadata_json or {}).get("finance_settings") or {})
    monthly_goal = float(fin_settings.get("monthlyGoal") or 50000.0)

    total_offerings = (
        await session.execute(
            select(func.coalesce(func.sum(Offering.amount), 0)).where(
                Offering.church_id == actor.church_id, Offering.deleted_at.is_(None)
            )
        )
    ).scalar() or 0

    total_tithes = (
        await session.execute(
            select(func.coalesce(func.sum(Offering.amount), 0)).where(
                Offering.church_id == actor.church_id,
                Offering.deleted_at.is_(None),
                Offering.type == OfferingType.TITHE,
            )
        )
    ).scalar() or 0

    regular_offerings = (
        await session.execute(
            select(func.coalesce(func.sum(Offering.amount), 0)).where(
                Offering.church_id == actor.church_id,
                Offering.deleted_at.is_(None),
                Offering.type == OfferingType.OFFERING,
            )
        )
    ).scalar() or 0

    special_offerings = float(total_offerings) - float(total_tithes) - float(regular_offerings)
    if special_offerings < 0:
        special_offerings = 0.0

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

    tot_inc = max(float(total_offerings), 1.0)
    income_breakdown = [
        {
            "category": "Tithes",
            "amount": float(total_tithes),
            "percentage": round((float(total_tithes) / tot_inc) * 100) if float(total_offerings) > 0 else 0,
        },
        {
            "category": "Offerings",
            "amount": float(regular_offerings),
            "percentage": round((float(regular_offerings) / tot_inc) * 100) if float(total_offerings) > 0 else 0,
        },
        {
            "category": "Special & Building Offerings",
            "amount": round(special_offerings, 2),
            "percentage": round((special_offerings / tot_inc) * 100) if float(total_offerings) > 0 else 0,
        },
    ]

    exp_rows = (
        await session.execute(
            select(Expense.category, func.coalesce(func.sum(Expense.amount), 0))
            .where(Expense.church_id == actor.church_id, Expense.deleted_at.is_(None))
            .group_by(Expense.category)
            .order_by(func.sum(Expense.amount).desc())
        )
    ).all()
    tot_exp = max(float(total_expenses), 1.0)
    expense_breakdown = [
        {
            "category": cat or "General",
            "amount": float(amt or 0),
            "percentage": round((float(amt or 0) / tot_exp) * 100) if float(total_expenses) > 0 else 0,
        }
        for cat, amt in exp_rows
    ]

    summary_data = {
        "totalOfferings": float(total_offerings),
        "totalTithes": float(total_tithes),
        "regularOfferings": float(regular_offerings),
        "specialOfferings": round(special_offerings, 2),
        "totalPledges": float(total_pledges),
        "monthlyGoal": monthly_goal,
        "expenses": float(total_expenses),
        "netIncome": float(total_offerings) - float(total_expenses),
        "incomeBreakdown": income_breakdown,
        "expenseBreakdown": expense_breakdown,
    }
    return {
        **summary_data,
        "status": "success",
        "data": summary_data,
    }


# ---------------------------------------------------------------------------
# Special Event Contributions & Fundraising (/church/contributions)
# ---------------------------------------------------------------------------
_CONTRIBUTION_TYPE_LABELS = {
    "wedding": "Wedding",
    "burial": "Burial",
    "baby-dedication": "Baby Dedication",
    "baby dedication": "Baby Dedication",
    "medical": "Medical Emergency",
    "medical emergency": "Medical Emergency",
    "building": "Church Building",
    "church building": "Church Building",
    "other": "Other",
}

_STANDARD_CONTRIBUTION_TYPES = [
    "Wedding",
    "Burial",
    "Baby Dedication",
    "Medical Emergency",
    "Church Building",
]


async def _get_church(session: DbSession, church_id: str) -> Church:
    church = (
        await session.execute(select(Church).where(Church.id == church_id))
    ).scalar_one_or_none()
    if not church:
        raise HTTPException(status_code=404, detail="Church not found")
    return church


def _normalize_contribution_campaign(c: Dict[str, Any]) -> Dict[str, Any]:
    cid = str(c.get("id") or c.get("_id") or uuid.uuid4())
    raw_type = str(c.get("type") or "Other").strip()
    norm_type = _CONTRIBUTION_TYPE_LABELS.get(raw_type.lower(), raw_type.title() or "Other")
    entries: List[Dict[str, Any]] = list(c.get("entries") or [])

    entries_sum = sum(float(e.get("amount") or 0) for e in entries)
    base_amount = float(c.get("amount") or 0)
    total_amount = entries_sum if entries else base_amount
    target_amount = float(c.get("targetAmount") or c.get("amount") or 0)

    contributors_count = (
        len(entries)
        if entries
        else int(c.get("contributors") or (1 if total_amount > 0 else 0))
    )

    raw_status = str(c.get("status") or "Active").strip().title()
    if raw_status not in ("Active", "Completed", "Ongoing"):
        raw_status = "Active"

    date_str = str(c.get("date") or c.get("createdAt") or _iso(_now()))[:10]

    return {
        **c,
        "id": cid,
        "_id": cid,
        "event": c.get("event") or c.get("title") or "Special Contribution",
        "type": norm_type,
        "date": date_str,
        "amount": round(total_amount, 2),
        "targetAmount": round(target_amount, 2),
        "contributors": contributors_count,
        "status": raw_status,
        "description": c.get("description") or "",
        "entries": entries,
        "createdAt": c.get("createdAt") or _iso(_now()),
        "updatedAt": c.get("updatedAt") or _iso(_now()),
    }


class ContributionCampaignPayload(BaseModel):
    event: Optional[str] = None
    type: Optional[str] = "Wedding"
    targetAmount: Any = 0
    amount: Any = 0
    date: Optional[str] = None
    status: Optional[str] = "Active"
    description: Optional[str] = ""


class ContributionEntryPayload(BaseModel):
    contributorName: Optional[str] = None
    memberId: Optional[str] = None
    amount: Any = 0
    paymentMethod: Optional[str] = "M-Pesa"
    date: Optional[str] = None
    notes: Optional[str] = ""


@router.get("/contributions/stats")
async def get_contributions_stats(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    campaigns = [
        _normalize_contribution_campaign(c)
        for c in list(meta.get("contributions") or [])
    ]

    total_contributions = sum(float(c.get("amount") or 0) for c in campaigns)
    total_target = sum(float(c.get("targetAmount") or 0) for c in campaigns)
    total_events = len(campaigns)
    active_contributions = sum(
        1 for c in campaigns if c.get("status") in ("Active", "Ongoing")
    )
    completed_contributions = sum(
        1 for c in campaigns if c.get("status") == "Completed"
    )

    now_dt = _now()
    current_ym = now_dt.strftime("%Y-%m")
    this_month_total = 0.0

    contributor_totals: Dict[str, float] = {}
    unique_contributors_set = set()
    monthly_buckets: Dict[str, float] = {}

    for i in range(3, -1, -1):
        dt_m = (now_dt.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
        monthly_buckets[dt_m.strftime("%B %Y")] = 0.0

    for c in campaigns:
        c_entries = c.get("entries") or []
        if c_entries:
            for e in c_entries:
                e_amt = float(e.get("amount") or 0)
                e_name = (e.get("contributorName") or "Anonymous").strip() or "Anonymous"
                unique_contributors_set.add(e_name.lower())
                contributor_totals[e_name] = contributor_totals.get(e_name, 0.0) + e_amt
                e_date_str = str(e.get("date") or c.get("date") or "")[:10]
                if e_date_str.startswith(current_ym):
                    this_month_total += e_amt
                try:
                    dt_parsed = datetime.strptime(e_date_str, "%Y-%m-%d")
                    label = dt_parsed.strftime("%B %Y")
                    monthly_buckets[label] = monthly_buckets.get(label, 0.0) + e_amt
                except Exception:
                    pass
        else:
            c_amt = float(c.get("amount") or 0)
            c_date_str = str(c.get("date") or "")[:10]
            if c_date_str.startswith(current_ym):
                this_month_total += c_amt
            try:
                dt_parsed = datetime.strptime(c_date_str, "%Y-%m-%d")
                label = dt_parsed.strftime("%B %Y")
                monthly_buckets[label] = monthly_buckets.get(label, 0.0) + c_amt
            except Exception:
                pass

    total_contributors = (
        len(unique_contributors_set)
        if unique_contributors_set
        else sum(int(c.get("contributors") or 0) for c in campaigns)
    )
    avg_per_event = (
        round(total_contributions / total_events, 2) if total_events > 0 else 0.0
    )

    monthly_trends = [
        {"month": k, "amount": round(v, 2)} for k, v in list(monthly_buckets.items())[-4:]
    ]
    avg_per_month = (
        round(
            sum(m["amount"] for m in monthly_trends) / max(len(monthly_trends), 1),
            2,
        )
        if monthly_trends
        else 0.0
    )

    top_contributors = [
        {"name": name, "amount": round(amt, 2)}
        for name, amt in sorted(
            contributor_totals.items(), key=lambda item: item[1], reverse=True
        )[:6]
    ]

    by_type_map: Dict[str, Dict[str, Any]] = {
        t: {"type": t, "count": 0, "total": 0.0, "targetTotal": 0.0}
        for t in _STANDARD_CONTRIBUTION_TYPES
    }
    for c in campaigns:
        t_name = c.get("type") or "Other"
        if t_name not in by_type_map:
            by_type_map[t_name] = {
                "type": t_name,
                "count": 0,
                "total": 0.0,
                "targetTotal": 0.0,
            }
        by_type_map[t_name]["count"] += 1
        by_type_map[t_name]["total"] = round(
            by_type_map[t_name]["total"] + float(c.get("amount") or 0), 2
        )
        by_type_map[t_name]["targetTotal"] = round(
            by_type_map[t_name]["targetTotal"] + float(c.get("targetAmount") or 0), 2
        )

    stats_payload = {
        "totalContributions": round(total_contributions, 2),
        "totalTarget": round(total_target, 2),
        "totalEvents": total_events,
        "activeContributions": active_contributions,
        "completedContributions": completed_contributions,
        "totalContributors": total_contributors,
        "thisMonthContributions": round(this_month_total, 2),
        "averagePerEvent": avg_per_event,
        "averagePerMonth": avg_per_month,
        "monthlyTrends": monthly_trends,
        "topContributors": top_contributors,
        "byType": list(by_type_map.values()),
    }
    return {
        "status": "success",
        "stats": stats_payload,
        "data": stats_payload,
    }


@router.get("/contributions")
async def list_contributions(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=100),
    type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    campaigns = [
        _normalize_contribution_campaign(c)
        for c in list(meta.get("contributions") or [])
    ]

    if search:
        s = search.strip().lower()
        campaigns = [
            c
            for c in campaigns
            if s in (c.get("event") or "").lower()
            or s in (c.get("type") or "").lower()
            or s in (c.get("description") or "").lower()
        ]
    if type and type.lower() != "all":
        t_norm = _CONTRIBUTION_TYPE_LABELS.get(type.strip().lower(), type.strip())
        campaigns = [
            c
            for c in campaigns
            if (c.get("type") or "").lower() in (type.strip().lower(), t_norm.lower())
        ]
    if status and status.lower() != "all":
        campaigns = [
            c
            for c in campaigns
            if (c.get("status") or "").lower() == status.strip().lower()
        ]

    total = len(campaigns)
    offset = (page - 1) * limit
    paginated = campaigns[offset : offset + limit]
    pages = max(1, math.ceil(total / limit)) if limit else 1

    return {
        "status": "success",
        "contributions": paginated,
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


@router.post("/contributions")
async def create_contribution_campaign(
    payload: ContributionCampaignPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    event_name = (payload.event or "").strip()
    if not event_name:
        raise HTTPException(status_code=400, detail="Event name is required")

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    campaigns: List[Dict[str, Any]] = list(meta.get("contributions") or [])

    try:
        target_amt = float(payload.targetAmount or 0)
    except Exception:
        target_amt = 0.0

    try:
        initial_amt = float(payload.amount or 0)
    except Exception:
        initial_amt = 0.0

    cid = str(uuid.uuid4())
    now_iso = _iso(_now())
    raw_type = (payload.type or "Wedding").strip()
    norm_type = _CONTRIBUTION_TYPE_LABELS.get(raw_type.lower(), raw_type.title() or "Wedding")

    new_campaign = _normalize_contribution_campaign(
        {
            "id": cid,
            "_id": cid,
            "event": event_name,
            "type": norm_type,
            "date": (payload.date or now_iso)[:10],
            "targetAmount": target_amt,
            "amount": initial_amt,
            "contributors": 1 if initial_amt > 0 else 0,
            "status": payload.status or "Active",
            "description": (payload.description or "").strip(),
            "entries": [],
            "createdAt": now_iso,
            "updatedAt": now_iso,
        }
    )
    campaigns.insert(0, new_campaign)
    meta["contributions"] = campaigns
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    queue_audit(
        background_tasks,
        request,
        "CREATE_CONTRIBUTION_CAMPAIGN",
        "CONTRIBUTION",
        cid,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    return {
        "status": "success",
        "message": "Contribution campaign created successfully",
        "contribution": new_campaign,
        "data": new_campaign,
    }


@router.get("/contributions/{contribution_id}")
async def get_contribution_campaign(
    contribution_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    for c in list(meta.get("contributions") or []):
        if c.get("id") == contribution_id or c.get("_id") == contribution_id:
            norm = _normalize_contribution_campaign(c)
            return {"status": "success", "contribution": norm, "data": norm}
    raise HTTPException(status_code=404, detail="Contribution campaign not found")


@router.put("/contributions/{contribution_id}")
@router.patch("/contributions/{contribution_id}")
async def update_contribution_campaign(
    contribution_id: str,
    payload: ContributionCampaignPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    campaigns: List[Dict[str, Any]] = list(meta.get("contributions") or [])

    idx = -1
    for i, c in enumerate(campaigns):
        if c.get("id") == contribution_id or c.get("_id") == contribution_id:
            idx = i
            break
    if idx < 0:
        raise HTTPException(status_code=404, detail="Contribution campaign not found")

    current = dict(campaigns[idx])
    if payload.event is not None and payload.event.strip():
        current["event"] = payload.event.strip()
    if payload.type is not None and payload.type.strip():
        raw_t = payload.type.strip()
        current["type"] = _CONTRIBUTION_TYPE_LABELS.get(raw_t.lower(), raw_t.title())
    if payload.targetAmount is not None and str(payload.targetAmount) != "":
        try:
            current["targetAmount"] = float(payload.targetAmount)
        except Exception:
            pass
    if payload.amount is not None and str(payload.amount) != "":
        try:
            current["amount"] = float(payload.amount)
        except Exception:
            pass
    if payload.date:
        current["date"] = payload.date[:10]
    if payload.status is not None and payload.status.strip():
        current["status"] = payload.status.strip().title()
    if payload.description is not None:
        current["description"] = payload.description.strip()
    current["updatedAt"] = _iso(_now())

    updated = _normalize_contribution_campaign(current)
    campaigns[idx] = updated
    meta["contributions"] = campaigns
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "status": "success",
        "message": "Contribution campaign updated successfully",
        "contribution": updated,
        "data": updated,
    }


@router.post("/contributions/{contribution_id}/entries")
async def add_contribution_entry(
    contribution_id: str,
    payload: ContributionEntryPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    campaigns: List[Dict[str, Any]] = list(meta.get("contributions") or [])

    idx = -1
    for i, c in enumerate(campaigns):
        if c.get("id") == contribution_id or c.get("_id") == contribution_id:
            idx = i
            break
    if idx < 0:
        raise HTTPException(status_code=404, detail="Contribution campaign not found")

    try:
        amt = float(payload.amount or 0)
    except Exception:
        amt = 0.0
    if amt <= 0:
        raise HTTPException(status_code=400, detail="Contribution amount must be greater than 0")

    donor_name = (payload.contributorName or "").strip()
    if not donor_name and payload.memberId:
        mem = (
            await session.execute(
                select(Member).where(
                    Member.id == payload.memberId,
                    Member.church_id == actor.church_id,
                )
            )
        ).scalar_one_or_none()
        if mem:
            donor_name = f"{mem.first_name or ''} {mem.last_name or ''}".strip()
    if not donor_name:
        donor_name = "Anonymous"

    now_iso = _iso(_now())
    entry = {
        "id": f"ce-{uuid.uuid4().hex[:8]}",
        "contributorName": donor_name,
        "memberId": payload.memberId,
        "amount": round(amt, 2),
        "paymentMethod": payload.paymentMethod or "M-Pesa",
        "date": (payload.date or now_iso)[:10],
        "notes": (payload.notes or "").strip(),
        "createdAt": now_iso,
    }

    current = dict(campaigns[idx])
    entries = list(current.get("entries") or [])
    entries.insert(0, entry)
    current["entries"] = entries
    current["updatedAt"] = now_iso

    updated = _normalize_contribution_campaign(current)
    if (
        updated["targetAmount"] > 0
        and updated["amount"] >= updated["targetAmount"]
        and updated["status"] == "Active"
    ):
        updated["status"] = "Completed"

    campaigns[idx] = updated
    meta["contributions"] = campaigns
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "status": "success",
        "message": "Contribution recorded successfully",
        "entry": entry,
        "contribution": updated,
        "data": updated,
    }


@router.delete("/contributions/{contribution_id}/entries/{entry_id}")
async def delete_contribution_entry(
    contribution_id: str,
    entry_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    campaigns: List[Dict[str, Any]] = list(meta.get("contributions") or [])

    idx = -1
    for i, c in enumerate(campaigns):
        if c.get("id") == contribution_id or c.get("_id") == contribution_id:
            idx = i
            break
    if idx < 0:
        raise HTTPException(status_code=404, detail="Contribution campaign not found")

    current = dict(campaigns[idx])
    entries = [
        e for e in list(current.get("entries") or []) if e.get("id") != entry_id
    ]
    current["entries"] = entries
    current["updatedAt"] = _iso(_now())

    updated = _normalize_contribution_campaign(current)
    campaigns[idx] = updated
    meta["contributions"] = campaigns
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "status": "success",
        "message": "Contribution entry removed",
        "contribution": updated,
        "data": updated,
    }


@router.delete("/contributions/{contribution_id}")
async def delete_contribution_campaign(
    contribution_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    campaigns: List[Dict[str, Any]] = list(meta.get("contributions") or [])

    before = len(campaigns)
    campaigns = [
        c
        for c in campaigns
        if c.get("id") != contribution_id and c.get("_id") != contribution_id
    ]
    if len(campaigns) == before:
        raise HTTPException(status_code=404, detail="Contribution campaign not found")

    meta["contributions"] = campaigns
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "status": "success",
        "message": "Contribution campaign deleted successfully",
    }

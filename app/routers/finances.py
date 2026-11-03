from datetime import datetime
from typing import List, Optional

from app.core.deps import (
    CurrentUser,
    DbSession,
    church_id_from,
    member_has_permission,
    require,
    resolve_actor,
)
from app.enums import OfferingType
from app.models.church import Branch
from app.models.finance import Fund, Offering, Pledge
from app.models.member import Member
from app.schemas.finance import (
    FundCreate,
    FundResponse,
    OfferingCreate,
    OfferingResponse,
)
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

router = APIRouter()


# ---------------------------------------------------------------------------
# Funds (offerings must be recorded against one)
# ---------------------------------------------------------------------------
@router.get("/funds", response_model=List[FundResponse])
async def get_funds(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
    include_inactive: bool = Query(False),
):
    """Funds of one church. Needs offering.read or offering.create (finance staff / clerks)."""
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    if actor.member is not None and not (
        await member_has_permission(session, actor.member.id, "offering.read")
        or await member_has_permission(session, actor.member.id, "offering.create")
    ):
        raise HTTPException(status_code=403, detail="Missing permission: offering.read")
    conds = [Fund.church_id == actor.church_id]
    if not include_inactive:
        conds.append(Fund.is_active.is_(True))
    return (
        (await session.execute(select(Fund).where(*conds).order_by(Fund.name)))
        .scalars()
        .all()
    )


@router.post("/funds", response_model=FundResponse)
async def create_fund(
    request: Request,
    background_tasks: BackgroundTasks,
    fund_in: FundCreate,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, fund_in.church_id)
    )
    await require(session, actor, "fund.manage")
    fund = Fund(
        church_id=actor.church_id,
        name=fund_in.name.strip(),
        is_active=fund_in.is_active,
    )
    session.add(fund)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="A fund with this name already exists"
        )
    await session.refresh(fund)
    queue_audit(
        background_tasks,
        request,
        "CREATE_FUND",
        "FUND",
        fund.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )
    return fund


# ---------------------------------------------------------------------------
# Offerings
# ---------------------------------------------------------------------------
@router.get("/offerings", response_model=List[OfferingResponse])
async def get_offerings(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
    branch_id: Optional[str] = Query(None),
    member_id: Optional[str] = Query(None),
    fund_id: Optional[str] = Query(None),
    type: Optional[OfferingType] = Query(None),
    received_from: Optional[datetime] = Query(None),
    received_to: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    Offerings of ONE church. Giving records are confidential: needs offering.read
    (church-wide, or for a single branch when branch_id is given). Total in X-Total-Count.
    """
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    await require(session, actor, "offering.read", branch_id)
    conds = [Offering.church_id == actor.church_id, Offering.deleted_at.is_(None)]
    for column, value in (
        (Offering.branch_id, branch_id),
        (Offering.member_id, member_id),
        (Offering.fund_id, fund_id),
        (Offering.type, type),
    ):
        if value is not None:
            conds.append(column == value)
    if received_from:
        conds.append(Offering.received_at >= received_from)
    if received_to:
        conds.append(Offering.received_at <= received_to)
    response.headers["X-Total-Count"] = str(
        (await session.execute(select(func.count(Offering.id)).where(*conds))).scalar()
        or 0
    )
    rows = await session.execute(
        select(Offering)
        .where(*conds)
        .order_by(Offering.received_at.desc(), Offering.id)
        .limit(limit)
        .offset(offset)
    )
    return rows.scalars().all()


@router.post("/offerings", response_model=OfferingResponse)
async def create_offering(
    request: Request,
    background_tasks: BackgroundTasks,
    offering_in: OfferingCreate,
    current_user: CurrentUser,
    session: DbSession,
):
    """
    Record an offering. member_id is who gave; leave it empty for ANONYMOUS giving
    (it is no longer defaulted to the person recording it).
    """
    actor = await resolve_actor(
        session, current_user, church_id_from(request, offering_in.church_id)
    )
    church_id = actor.church_id
    await require(session, actor, "offering.create", offering_in.branch_id)
    if (
        offering_in.branch_id
        and not (
            await session.execute(
                select(Branch.id).where(
                    Branch.id == offering_in.branch_id,
                    Branch.church_id == church_id,
                    Branch.deleted_at.is_(None),
                )
            )
        ).first()
    ):
        raise HTTPException(status_code=404, detail="Branch not found")
    if (
        offering_in.member_id
        and not (
            await session.execute(
                select(Member.id).where(
                    Member.id == offering_in.member_id,
                    Member.church_id == church_id,
                    Member.deleted_at.is_(None),
                )
            )
        ).first()
    ):
        raise HTTPException(status_code=404, detail="Member not found")
    if not (
        await session.execute(
            select(Fund.id).where(
                Fund.id == offering_in.fund_id,
                Fund.church_id == church_id,
                Fund.is_active.is_(True),
            )
        )
    ).first():
        raise HTTPException(status_code=404, detail="Fund not found")
    if offering_in.pledge_id:
        pledge = (
            await session.execute(
                select(Pledge).where(
                    Pledge.id == offering_in.pledge_id,
                    Pledge.church_id == church_id,
                    Pledge.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not pledge:
            raise HTTPException(status_code=404, detail="Pledge not found")
        if offering_in.member_id and pledge.member_id != offering_in.member_id:
            raise HTTPException(
                status_code=422, detail="The pledge belongs to a different member"
            )
    data = {**offering_in.model_dump(), "church_id": church_id}
    if data.get("received_at") is None:
        data.pop("received_at", None)  # let the column default ("now") apply
    offering = Offering(**data)
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
        # Decimal / Enum are not JSON-serialisable; the old metadata silently failed to save.
        metadata_json={
            "amount": str(offering.amount),
            "currency": offering.currency,
            "type": offering.type.value,
            "fund_id": offering.fund_id,
            "anonymous": offering.member_id is None,
        },
    )
    return offering

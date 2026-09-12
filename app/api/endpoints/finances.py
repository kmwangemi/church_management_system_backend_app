from typing import List

from fastapi import APIRouter, BackgroundTasks, Request
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.finance import Offering
from app.schemas.finance import OfferingCreate, OfferingResponse
from app.services.audit import log_action

router = APIRouter()


@router.get("/offerings", response_model=List[OfferingResponse])
async def get_offerings(current_user: CurrentUser, session: DbSession):
    """
    Get all offerings.
    """
    stmt = select(Offering)
    result = await session.execute(stmt)
    return result.scalars().all()


@router.post("/offerings", response_model=OfferingResponse)
async def create_offering(
    request: Request,
    background_tasks: BackgroundTasks,
    offering_in: OfferingCreate,
    current_user: CurrentUser,
    session: DbSession,
):
    """
    Record new offering.
    """
    new_offering = Offering(**offering_in.dict())
    # Override user_id if not provided, assuming the current user is recording it for themselves
    if not new_offering.user_id:
        new_offering.user_id = current_user.id
    session.add(new_offering)
    await session.commit()
    await session.refresh(new_offering)
    background_tasks.add_task(
        log_action,
        action="RECORD_OFFERING",
        resource_type="OFFERING",
        resource_id=new_offering.id,
        user_id=current_user.id,
        church_id=new_offering.church_id,
        metadata_json={"amount": new_offering.amount, "type": new_offering.type},
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
    )
    return new_offering

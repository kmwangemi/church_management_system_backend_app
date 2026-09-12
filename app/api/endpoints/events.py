from typing import List

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.event import Event
from app.schemas.event import EventCreate, EventResponse
from app.services.audit import log_action

router = APIRouter()


@router.get("/", response_model=List[EventResponse])
async def get_events(current_user: CurrentUser, session: DbSession):
    """
    Get all events.
    """
    stmt = select(Event)
    result = await session.execute(stmt)
    return result.scalars().all()


@router.post("/", response_model=EventResponse)
async def create_event(
    request: Request,
    background_tasks: BackgroundTasks,
    event_in: EventCreate,
    current_user: CurrentUser,
    session: DbSession,
):
    """
    Create new event.
    """
    new_event = Event(**event_in.dict())
    session.add(new_event)
    await session.commit()
    await session.refresh(new_event)
    background_tasks.add_task(
        log_action,
        action="CREATE_EVENT",
        resource_type="EVENT",
        resource_id=new_event.id,
        user_id=current_user.id,
        church_id=new_event.church_id,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
    )
    return new_event


@router.delete("/{event_id}")
async def delete_event(
    event_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    """
    Delete event.
    """
    stmt = select(Event).where(Event.id == event_id)
    result = await session.execute(stmt)
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    org_id = event.church_id
    await session.delete(event)
    await session.commit()
    background_tasks.add_task(
        log_action,
        action="DELETE_EVENT",
        resource_type="EVENT",
        resource_id=event_id,
        user_id=current_user.id,
        church_id=org_id,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
    )
    return {"status": "success", "message": "Event deleted"}

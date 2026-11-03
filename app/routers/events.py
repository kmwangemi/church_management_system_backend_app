from datetime import datetime
from typing import List, Optional

from app.core.deps import CurrentUser, DbSession, church_id_from, require, resolve_actor
from app.enums import EventStatus
from app.models.church import Branch
from app.models.detail import VolunteerAssignment
from app.models.event import Activity, Attendance, Event
from app.schemas.event import EventCreate, EventResponse, EventUpdate
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from sqlalchemy import delete, func, or_, select

router = APIRouter()


async def _check_branch(session, branch_id: Optional[str], church_id: str) -> None:
    if (
        branch_id
        and not (
            await session.execute(
                select(Branch.id).where(
                    Branch.id == branch_id,
                    Branch.church_id == church_id,
                    Branch.deleted_at.is_(None),
                )
            )
        ).first()
    ):
        raise HTTPException(status_code=404, detail="Branch not found")


async def _get_event(session, event_id: str) -> Event:
    event = (
        await session.execute(select(Event).where(Event.id == event_id))
    ).scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.get("/", response_model=List[EventResponse])
async def get_events(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(
        None, description="Church ID (or send X-Church-Id)"
    ),
    branch_id: Optional[str] = Query(
        None, description="Events of this branch plus church-wide events"
    ),
    status: Optional[EventStatus] = Query(None),
    starts_after: Optional[datetime] = Query(None),
    starts_before: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    Events of ONE church (any active member can read the church calendar).
    The total is returned in the X-Total-Count header.
    """
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Event.church_id == actor.church_id]
    if branch_id:
        conds.append(or_(Event.branch_id == branch_id, Event.branch_id.is_(None)))
    if status:
        conds.append(Event.status == status)
    if starts_after:
        conds.append(Event.start_time >= starts_after)
    if starts_before:
        conds.append(Event.start_time <= starts_before)
    response.headers["X-Total-Count"] = str(
        (await session.execute(select(func.count(Event.id)).where(*conds))).scalar()
        or 0
    )
    rows = await session.execute(
        select(Event)
        .where(*conds)
        .order_by(Event.start_time, Event.id)
        .limit(limit)
        .offset(offset)
    )
    return rows.scalars().all()


@router.post("/", response_model=EventResponse)
async def create_event(
    request: Request,
    background_tasks: BackgroundTasks,
    event_in: EventCreate,
    current_user: CurrentUser,
    session: DbSession,
):
    """Create an event. A branch-scoped role can create events for its own branch only."""
    actor = await resolve_actor(
        session, current_user, church_id_from(request, event_in.church_id)
    )
    await _check_branch(session, event_in.branch_id, actor.church_id)
    await require(
        session, actor, "event.create", event_in.branch_id
    )  # no branch = church-wide permission
    event = Event(**{**event_in.model_dump(), "church_id": actor.church_id})
    session.add(event)
    await session.commit()
    await session.refresh(event)
    queue_audit(
        background_tasks,
        request,
        "CREATE_EVENT",
        "EVENT",
        event.id,
        actor_user_id=current_user.id,
        church_id=event.church_id,
    )
    return event


@router.put("/{event_id}", response_model=EventResponse)
async def update_event(
    event_id: str,
    event_in: EventUpdate,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    event = await _get_event(session, event_id)
    actor = await resolve_actor(session, current_user, event.church_id)
    await require(session, actor, "event.update", event.branch_id)
    data = event_in.model_dump(exclude_unset=True)
    if "branch_id" in data and data["branch_id"] != event.branch_id:
        await _check_branch(session, data["branch_id"], actor.church_id)
        await require(session, actor, "event.update", data["branch_id"])
    for key in ("name", "start_time", "status"):
        if key in data and data[key] is None:
            data.pop(key)  # NOT NULL columns
    start = data.get("start_time", event.start_time)
    end = data["end_time"] if "end_time" in data else event.end_time
    if end and start and end.replace(tzinfo=None) < start.replace(tzinfo=None):
        raise HTTPException(
            status_code=422, detail="end_time must not be before start_time"
        )
    for key, value in data.items():
        setattr(event, key, value)
    await session.commit()
    await session.refresh(event)
    queue_audit(
        background_tasks,
        request,
        "UPDATE_EVENT",
        "EVENT",
        event.id,
        actor_user_id=current_user.id,
        church_id=event.church_id,
        metadata_json={"fields": sorted(data)},
    )
    return event


@router.delete("/{event_id}")
async def delete_event(
    event_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    """
    Delete an event that has no attendance yet. Events with attendance are history:
    cancel them instead (PUT status=CANCELLED).
    """
    event = await _get_event(session, event_id)
    actor = await resolve_actor(session, current_user, event.church_id)
    await require(session, actor, "event.delete", event.branch_id)
    attended = (
        await session.execute(
            select(func.count(Attendance.id)).where(Attendance.event_id == event.id)
        )
    ).scalar() or 0
    if attended:
        raise HTTPException(
            status_code=409,
            detail=f"This event has {attended} attendance record(s). Cancel it instead (PUT status=CANCELLED).",
        )
    church_id = event.church_id
    # Explicit child deletes: works the same on every database (no reliance on ORM FK nulling).
    await session.execute(delete(Activity).where(Activity.event_id == event.id))
    await session.execute(
        delete(VolunteerAssignment).where(VolunteerAssignment.event_id == event.id)
    )
    await session.execute(delete(Event).where(Event.id == event.id))
    await session.commit()
    queue_audit(
        background_tasks,
        request,
        "DELETE_EVENT",
        "EVENT",
        event_id,
        actor_user_id=current_user.id,
        church_id=church_id,
    )
    return {"status": "success", "message": "Event deleted"}

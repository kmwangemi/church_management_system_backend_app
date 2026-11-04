from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional

from app.core.deps import CurrentUser, DbSession, church_id_from, require, resolve_actor
from app.enums import EventStatus
from app.models.church import Branch
from app.models.event import Activity, Attendance, Event
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _format_event(event: Event, attendee_count: int = 0) -> Dict[str, Any]:
    st = event.start_time
    et = event.end_time
    start_date_str = st.strftime("%Y-%m-%d") if st else ""
    start_time_str = st.strftime("%H:%M") if st else ""
    end_date_str = et.strftime("%Y-%m-%d") if et else start_date_str
    end_time_str = et.strftime("%H:%M") if et else ""

    return {
        "_id": str(event.id),
        "id": str(event.id),
        "title": event.name,
        "name": event.name,
        "description": event.description or "",
        "type": "service",
        "date": start_date_str,
        "time": f"{start_time_str} - {end_time_str}".strip(" -"),
        "startDate": start_date_str,
        "startTime": start_time_str,
        "endDate": end_date_str,
        "endTime": end_time_str,
        "start_time": _iso(event.start_time),
        "end_time": _iso(event.end_time),
        "location": event.location or "Main Sanctuary",
        "organizer": "Church Administration",
        "status": event.status.value.lower() if hasattr(event.status, "value") else str(event.status).lower(),
        "registeredAttendees": attendee_count,
        "expectedAttendees": 100,
        "maxAttendees": 500,
        "capacity": 500,
        "isRecurring": bool(event.recurrence_rule),
        "recurring": event.recurrence_rule or "None",
        "recurrenceRule": event.recurrence_rule,
        "churchId": str(event.church_id),
        "branchId": str(event.branch_id) if event.branch_id else None,
        "createdAt": _iso(event.created_at),
        "updatedAt": _iso(event.updated_at),
    }


class FlexibleEventPayload(BaseModel):
    title: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    location: Optional[str] = None
    expectedAttendees: Optional[Any] = None
    organizer: Optional[str] = None
    requiresRegistration: Optional[bool] = False
    isRecurring: Optional[bool] = False
    recurringPattern: Optional[str] = None
    registrationDeadline: Optional[str] = None
    maxAttendees: Optional[Any] = None
    notes: Optional[str] = None
    branchId: Optional[str] = None
    branch_id: Optional[str] = None
    churchId: Optional[str] = None
    church_id: Optional[str] = None
    status: Optional[str] = None


@router.get("")
@router.get("/")
async def get_events(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
    branch_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    search: str = Query("", max_length=100),
    offset: Optional[int] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Event.church_id == actor.church_id]
    if branch_id:
        conds.append(or_(Event.branch_id == branch_id, Event.branch_id.is_(None)))
    if search:
        s = f"%{search.strip()}%"
        conds.append(or_(Event.name.ilike(s), Event.description.ilike(s), Event.location.ilike(s)))

    total = (
        await session.execute(select(func.count(Event.id)).where(*conds))
    ).scalar() or 0

    if offset is None:
        offset = (page - 1) * limit

    rows = (
        (
            await session.execute(
                select(Event)
                .where(*conds)
                .order_by(Event.start_time.desc(), Event.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    formatted_events = []
    for evt in rows:
        att_cnt = (
            await session.execute(
                select(func.count(Attendance.id)).where(Attendance.event_id == evt.id)
            )
        ).scalar() or 0
        formatted_events.append(_format_event(evt, att_cnt))

    pages = math.ceil(total / limit) if limit else 1
    response.headers["X-Total-Count"] = str(total)

    return {
        "events": formatted_events,
        "data": formatted_events,
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
async def create_event(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: FlexibleEventPayload,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, payload.churchId or payload.church_id or church_id)
    )
    name = (payload.title or payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Event title is required")

    start_dt = payload.start_time
    if not start_dt:
        if payload.startDate:
            time_part = payload.startTime or "09:00"
            try:
                start_dt = datetime.strptime(f"{payload.startDate} {time_part}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except Exception:
                start_dt = _now()
        else:
            start_dt = _now()

    end_dt = payload.end_time
    if not end_dt and payload.endDate:
        time_part = payload.endTime or "11:00"
        try:
            end_dt = datetime.strptime(f"{payload.endDate} {time_part}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except Exception:
            end_dt = None

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

    status_enum = EventStatus.ACTIVE
    if payload.status:
        st_upper = payload.status.upper()
        if st_upper in EventStatus.__members__:
            status_enum = EventStatus[st_upper]

    event = Event(
        name=name,
        church_id=actor.church_id,
        branch_id=branch_id,
        start_time=start_dt,
        end_time=end_dt,
        location=payload.location or "",
        description=payload.description or "",
        recurrence_rule=payload.recurringPattern if payload.isRecurring else None,
        status=status_enum,
    )
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

    formatted = _format_event(event)
    return {
        "status": "success",
        "message": "Event created successfully",
        "event": formatted,
        "data": formatted,
    }


@router.get("/{event_id}")
async def get_event(
    event_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    event = (
        await session.execute(
            select(Event).where(
                Event.id == event_id, Event.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    att_cnt = (
        await session.execute(
            select(func.count(Attendance.id)).where(Attendance.event_id == event.id)
        )
    ).scalar() or 0

    formatted = _format_event(event, att_cnt)
    return {
        "event": formatted,
        "data": {"event": formatted},
    }


@router.put("/{event_id}")
async def update_event(
    event_id: str,
    payload: FlexibleEventPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    event = (
        await session.execute(
            select(Event).where(
                Event.id == event_id, Event.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    name = payload.title or payload.name
    if name is not None:
        event.name = name.strip()
    if payload.description is not None:
        event.description = payload.description
    if payload.location is not None:
        event.location = payload.location

    if payload.startDate:
        time_part = payload.startTime or "09:00"
        try:
            event.start_time = datetime.strptime(f"{payload.startDate} {time_part}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except Exception:
            pass

    if payload.endDate:
        time_part = payload.endTime or "11:00"
        try:
            event.end_time = datetime.strptime(f"{payload.endDate} {time_part}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except Exception:
            pass

    if payload.status:
        st_upper = payload.status.upper()
        if st_upper in EventStatus.__members__:
            event.status = EventStatus[st_upper]

    event.updated_at = _now()
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
    )

    formatted = _format_event(event)
    return {
        "status": "success",
        "message": "Event updated successfully",
        "event": formatted,
        "data": formatted,
    }


@router.delete("/{event_id}")
async def delete_event(
    event_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    event = (
        await session.execute(
            select(Event).where(
                Event.id == event_id, Event.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await session.execute(delete(Attendance).where(Attendance.event_id == event.id))
    await session.execute(delete(Activity).where(Activity.event_id == event.id))
    await session.execute(delete(Event).where(Event.id == event.id))
    await session.commit()

    queue_audit(
        background_tasks,
        request,
        "DELETE_EVENT",
        "EVENT",
        event_id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    return {"status": "success", "message": "Event deleted successfully"}

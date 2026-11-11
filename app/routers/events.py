from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional

from app.core.deps import CurrentUser, DbSession, church_id_from, require, resolve_actor
from app.enums import AttendanceStatus, EventStatus
from app.models.church import Branch, Church
from app.models.event import Activity, Attendance, Event
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


_EVENT_TYPE_LABELS = {
    "service": "Service",
    "conference": "Conference",
    "study": "Bible Study",
    "bible study": "Bible Study",
    "outreach": "Outreach",
    "meeting": "Meeting",
    "social": "Social Event",
    "social event": "Social Event",
    "training": "Training",
    "other": "Other",
}


async def _get_church(session: DbSession, church_id: str) -> Church:
    church = (
        await session.execute(select(Church).where(Church.id == church_id))
    ).scalar_one_or_none()
    if not church:
        raise HTTPException(status_code=404, detail="Church not found")
    return church


def _format_event(
    event: Event,
    attendee_count: int = 0,
    meta_entry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    extra = dict(meta_entry or {})
    st = event.start_time
    et = event.end_time
    start_date_str = st.strftime("%Y-%m-%d") if st else ""
    start_time_str = st.strftime("%H:%M") if st else "09:00"
    end_date_str = et.strftime("%Y-%m-%d") if et else start_date_str
    end_time_str = et.strftime("%H:%M") if et else "11:00"

    raw_type = (extra.get("type") or "service").strip()
    type_label = _EVENT_TYPE_LABELS.get(raw_type.lower(), raw_type.title())

    raw_status = (
        event.status.value
        if hasattr(event.status, "value")
        else str(event.status)
    ).upper()
    display_status = extra.get("displayStatus")
    if raw_status == "CANCELLED":
        display_status = "Cancelled"
    elif raw_status == "COMPLETED":
        display_status = "Completed"
    elif not display_status:
        display_status = (
            "Open for Registration"
            if extra.get("requiresRegistration")
            else "Scheduled"
        )

    rec_rule = event.recurrence_rule or extra.get("recurringPattern") or ""
    is_recurring = bool(rec_rule and rec_rule.lower() != "none")
    recurring_label = rec_rule.capitalize() if is_recurring else "None"

    expected_att = int(extra.get("expectedAttendees") or 100)
    max_att = int(extra.get("maxAttendees") or max(expected_att, 500))

    return {
        "_id": str(event.id),
        "id": str(event.id),
        "title": event.name,
        "name": event.name,
        "description": event.description or "",
        "type": type_label,
        "typeValue": raw_type.lower(),
        "date": start_date_str,
        "time": start_time_str,
        "startDate": start_date_str,
        "startTime": start_time_str,
        "endDate": end_date_str,
        "endTime": end_time_str,
        "start_time": _iso(event.start_time),
        "end_time": _iso(event.end_time),
        "location": event.location or "Main Sanctuary",
        "organizer": extra.get("organizer") or "Church Administration",
        "status": display_status,
        "rawStatus": raw_status,
        "registeredAttendees": attendee_count,
        "expectedAttendees": expected_att,
        "maxAttendees": max_att,
        "capacity": max_att,
        "requiresRegistration": bool(extra.get("requiresRegistration", False)),
        "registrationDeadline": extra.get("registrationDeadline") or "",
        "isRecurring": is_recurring,
        "recurring": recurring_label,
        "recurringPattern": rec_rule.lower() if is_recurring else "",
        "recurrenceRule": event.recurrence_rule,
        "notes": extra.get("notes") or "",
        "remindersSentCount": int(extra.get("remindersSentCount") or 0),
        "lastReminderSentAt": extra.get("lastReminderSentAt"),
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
    requiresRegistration: Optional[bool] = None
    isRecurring: Optional[bool] = None
    recurringPattern: Optional[str] = None
    registrationDeadline: Optional[str] = None
    maxAttendees: Optional[Any] = None
    notes: Optional[str] = None
    branchId: Optional[str] = None
    branch_id: Optional[str] = None
    churchId: Optional[str] = None
    church_id: Optional[str] = None
    status: Optional[str] = None


class EventAttendancePayload(BaseModel):
    memberId: str
    status: Optional[str] = "PRESENT"


class EventReminderPayload(BaseModel):
    channel: Optional[str] = "all"
    message: Optional[str] = None


def _parse_event_datetime(date_str: Optional[str], time_str: Optional[str], default_time: str = "09:00") -> Optional[datetime]:
    if not date_str:
        return None
    clean_date = date_str[:10]
    clean_time = (time_str or default_time).strip()[:5]
    try:
        return datetime.strptime(f"{clean_date} {clean_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        try:
            return datetime.strptime(clean_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None


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
    church = await _get_church(session, actor.church_id)
    event_meta_map: Dict[str, Any] = dict(
        (church.metadata_json or {}).get("event_meta") or {}
    )

    conds = [Event.church_id == actor.church_id]
    if branch_id:
        conds.append(or_(Event.branch_id == branch_id, Event.branch_id.is_(None)))

    rows = (
        (
            await session.execute(
                select(Event)
                .where(*conds)
                .order_by(Event.start_time.desc(), Event.id)
            )
        )
        .scalars()
        .all()
    )

    att_rows = (
        await session.execute(
            select(Attendance.event_id, func.count(Attendance.id))
            .join(Event, Event.id == Attendance.event_id)
            .where(Event.church_id == actor.church_id)
            .group_by(Attendance.event_id)
        )
    ).all()
    att_by_event = {str(eid): int(cnt or 0) for eid, cnt in att_rows}

    all_formatted = [
        _format_event(
            evt,
            att_by_event.get(str(evt.id), 0),
            event_meta_map.get(str(evt.id)),
        )
        for evt in rows
    ]

    if status and status.lower() != "all":
        st_low = status.strip().lower()
        all_formatted = [
            e
            for e in all_formatted
            if (e.get("status") or "").lower() == st_low
            or (e.get("rawStatus") or "").lower() == st_low
        ]

    if search:
        s = search.strip().lower()
        all_formatted = [
            e
            for e in all_formatted
            if s in (e.get("title") or "").lower()
            or s in (e.get("description") or "").lower()
            or s in (e.get("location") or "").lower()
            or s in (e.get("organizer") or "").lower()
            or s in (e.get("type") or "").lower()
        ]

    total = len(all_formatted)
    if offset is None:
        offset = (page - 1) * limit
    formatted_events = all_formatted[offset : offset + limit]

    pages = max(1, math.ceil(total / limit)) if limit else 1
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
        session,
        current_user,
        church_id_from(request, payload.churchId or payload.church_id or church_id),
    )
    name = (payload.title or payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Event title is required")

    start_dt = payload.start_time or _parse_event_datetime(
        payload.startDate, payload.startTime, "09:00"
    ) or _now()
    end_dt = payload.end_time or _parse_event_datetime(
        payload.endDate or payload.startDate, payload.endTime, "11:00"
    )

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
    display_status = "Open for Registration" if payload.requiresRegistration else "Scheduled"
    if payload.status:
        st_upper = payload.status.strip().upper()
        if st_upper in EventStatus.__members__:
            status_enum = EventStatus[st_upper]
        if st_upper == "CANCELLED":
            display_status = "Cancelled"
            status_enum = EventStatus.CANCELLED
        elif st_upper == "COMPLETED":
            display_status = "Completed"
            status_enum = EventStatus.COMPLETED
        elif payload.status.strip() in (
            "Scheduled",
            "Open for Registration",
            "Completed",
            "Cancelled",
        ):
            display_status = payload.status.strip()

    rec_rule = (
        payload.recurringPattern.strip()
        if payload.isRecurring and payload.recurringPattern
        else None
    )

    event = Event(
        name=name,
        church_id=actor.church_id,
        branch_id=branch_id,
        start_time=start_dt,
        end_time=end_dt,
        location=(payload.location or "").strip(),
        description=(payload.description or "").strip(),
        recurrence_rule=rec_rule,
        status=status_enum,
    )
    session.add(event)
    await session.flush()

    try:
        exp_att = int(float(str(payload.expectedAttendees))) if payload.expectedAttendees else 100
    except Exception:
        exp_att = 100

    try:
        max_att = int(float(str(payload.maxAttendees))) if payload.maxAttendees else max(exp_att, 500)
    except Exception:
        max_att = max(exp_att, 500)

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    event_meta_map = dict(meta.get("event_meta") or {})
    meta_entry = {
        "type": (payload.type or "service").strip(),
        "organizer": (payload.organizer or current_user.display_name or "Church Administration").strip(),
        "expectedAttendees": exp_att,
        "maxAttendees": max_att,
        "requiresRegistration": bool(payload.requiresRegistration),
        "registrationDeadline": (payload.registrationDeadline or "")[:10] if payload.registrationDeadline else "",
        "isRecurring": bool(payload.isRecurring),
        "recurringPattern": rec_rule or "",
        "notes": (payload.notes or "").strip(),
        "displayStatus": display_status,
        "remindersSentCount": 0,
    }
    event_meta_map[str(event.id)] = meta_entry
    meta["event_meta"] = event_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

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

    formatted = _format_event(event, 0, meta_entry)
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

    church = await _get_church(session, actor.church_id)
    event_meta_map: Dict[str, Any] = dict(
        (church.metadata_json or {}).get("event_meta") or {}
    )

    att_cnt = (
        await session.execute(
            select(func.count(Attendance.id)).where(Attendance.event_id == event.id)
        )
    ).scalar() or 0

    formatted = _format_event(event, att_cnt, event_meta_map.get(str(event.id)))
    return {
        "event": formatted,
        "data": {"event": formatted},
    }


@router.put("/{event_id}")
@router.patch("/{event_id}")
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
    if name is not None and name.strip():
        event.name = name.strip()
    if payload.description is not None:
        event.description = payload.description.strip()
    if payload.location is not None:
        event.location = payload.location.strip()

    if payload.startDate or payload.startTime:
        base_date = (
            payload.startDate[:10]
            if payload.startDate
            else (event.start_time.strftime("%Y-%m-%d") if event.start_time else _now().strftime("%Y-%m-%d"))
        )
        base_time = (
            payload.startTime
            if payload.startTime
            else (event.start_time.strftime("%H:%M") if event.start_time else "09:00")
        )
        parsed_st = _parse_event_datetime(base_date, base_time, "09:00")
        if parsed_st:
            event.start_time = parsed_st

    if payload.endDate or payload.endTime:
        base_end_date = (
            payload.endDate[:10]
            if payload.endDate
            else (
                event.end_time.strftime("%Y-%m-%d")
                if event.end_time
                else (event.start_time.strftime("%Y-%m-%d") if event.start_time else _now().strftime("%Y-%m-%d"))
            )
        )
        base_end_time = (
            payload.endTime
            if payload.endTime
            else (event.end_time.strftime("%H:%M") if event.end_time else "11:00")
        )
        parsed_et = _parse_event_datetime(base_end_date, base_end_time, "11:00")
        if parsed_et:
            event.end_time = parsed_et

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    event_meta_map = dict(meta.get("event_meta") or {})
    entry = dict(event_meta_map.get(str(event.id)) or {})

    if payload.type is not None and payload.type.strip():
        entry["type"] = payload.type.strip()
    if payload.organizer is not None and payload.organizer.strip():
        entry["organizer"] = payload.organizer.strip()
    if payload.expectedAttendees is not None and str(payload.expectedAttendees) != "":
        try:
            entry["expectedAttendees"] = int(float(str(payload.expectedAttendees)))
        except Exception:
            pass
    if payload.maxAttendees is not None and str(payload.maxAttendees) != "":
        try:
            entry["maxAttendees"] = int(float(str(payload.maxAttendees)))
        except Exception:
            pass
    if payload.requiresRegistration is not None:
        entry["requiresRegistration"] = bool(payload.requiresRegistration)
    if payload.registrationDeadline is not None:
        entry["registrationDeadline"] = payload.registrationDeadline[:10] if payload.registrationDeadline else ""
    if payload.notes is not None:
        entry["notes"] = payload.notes.strip()

    if payload.isRecurring is not None:
        entry["isRecurring"] = bool(payload.isRecurring)
        if not payload.isRecurring:
            event.recurrence_rule = None
            entry["recurringPattern"] = ""
    if payload.recurringPattern is not None:
        if payload.recurringPattern.strip() and payload.recurringPattern.lower() != "none":
            event.recurrence_rule = payload.recurringPattern.strip()
            entry["recurringPattern"] = payload.recurringPattern.strip()
            entry["isRecurring"] = True
        else:
            event.recurrence_rule = None
            entry["recurringPattern"] = ""
            entry["isRecurring"] = False

    if payload.status:
        st_clean = payload.status.strip()
        st_upper = st_clean.upper()
        if st_upper == "CANCELLED":
            event.status = EventStatus.CANCELLED
            entry["displayStatus"] = "Cancelled"
        elif st_upper == "COMPLETED":
            event.status = EventStatus.COMPLETED
            entry["displayStatus"] = "Completed"
        elif st_upper == "DRAFT":
            event.status = EventStatus.DRAFT
            entry["displayStatus"] = "Draft"
        else:
            event.status = EventStatus.ACTIVE
            entry["displayStatus"] = st_clean

    event_meta_map[str(event.id)] = entry
    meta["event_meta"] = event_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    event.updated_at = _now()
    await session.commit()
    await session.refresh(event)

    att_cnt = (
        await session.execute(
            select(func.count(Attendance.id)).where(Attendance.event_id == event.id)
        )
    ).scalar() or 0

    queue_audit(
        background_tasks,
        request,
        "UPDATE_EVENT",
        "EVENT",
        event.id,
        actor_user_id=current_user.id,
        church_id=event.church_id,
    )

    formatted = _format_event(event, att_cnt, entry)
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


# ---------------------------------------------------------------------------
# Event Attendance Management
# ---------------------------------------------------------------------------
@router.get("/{event_id}/attendance")
async def list_event_attendance(
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

    rows = (
        await session.execute(
            select(Attendance, Member, User)
            .join(Member, Member.id == Attendance.member_id)
            .outerjoin(User, User.id == Member.user_id)
            .where(Attendance.event_id == event.id)
            .order_by(Attendance.checked_in_at.desc(), Attendance.created_at.desc())
        )
    ).all()

    attendees = []
    for att, mem, usr in rows:
        full_name = (
            f"{mem.first_name or ''} {mem.last_name or ''}".strip()
            or (usr.display_name if usr else "Member")
        )
        st_val = (
            att.status.value
            if hasattr(att.status, "value")
            else str(att.status)
        )
        attendees.append(
            {
                "id": str(att.id),
                "_id": str(att.id),
                "eventId": str(event.id),
                "memberId": str(mem.id),
                "memberName": full_name,
                "email": mem.email or (usr.email if usr else ""),
                "phoneNumber": mem.phone_number or "",
                "status": st_val.capitalize(),
                "checkedInAt": _iso(att.checked_in_at or att.created_at),
            }
        )

    return {
        "status": "success",
        "attendees": attendees,
        "total": len(attendees),
        "data": attendees,
    }


@router.post("/{event_id}/attendance")
async def record_event_attendance(
    event_id: str,
    payload: EventAttendancePayload,
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

    mem = (
        await session.execute(
            select(Member).where(
                or_(
                    Member.id == payload.memberId,
                    Member.user_id == payload.memberId,
                ),
                Member.church_id == actor.church_id,
                Member.deleted_at.is_(None),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not mem:
        raise HTTPException(status_code=404, detail="Member not found in church")

    raw_st = (payload.status or "PRESENT").strip().upper()
    att_status = (
        AttendanceStatus[raw_st]
        if raw_st in AttendanceStatus.__members__
        else AttendanceStatus.PRESENT
    )

    existing = (
        await session.execute(
            select(Attendance).where(
                Attendance.event_id == event.id,
                Attendance.member_id == mem.id,
            )
        )
    ).scalar_one_or_none()

    if existing:
        existing.status = att_status
        existing.checked_in_at = _now()
        if actor.member:
            existing.checked_in_by_member_id = actor.member.id
        att = existing
    else:
        att = Attendance(
            event_id=event.id,
            member_id=mem.id,
            checked_in_by_member_id=actor.member.id if actor.member else None,
            checked_in_at=_now(),
            status=att_status,
        )
        session.add(att)

    await session.commit()
    await session.refresh(att)

    total_att = (
        await session.execute(
            select(func.count(Attendance.id)).where(Attendance.event_id == event.id)
        )
    ).scalar() or 0

    full_name = f"{mem.first_name or ''} {mem.last_name or ''}".strip() or "Member"
    record_out = {
        "id": str(att.id),
        "_id": str(att.id),
        "eventId": str(event.id),
        "memberId": str(mem.id),
        "memberName": full_name,
        "email": mem.email or "",
        "phoneNumber": mem.phone_number or "",
        "status": att_status.value.capitalize(),
        "checkedInAt": _iso(att.checked_in_at),
    }

    return {
        "status": "success",
        "message": "Attendance recorded successfully",
        "attendance": record_out,
        "registeredAttendees": int(total_att),
        "data": record_out,
    }


@router.delete("/{event_id}/attendance/{attendance_id}")
async def remove_event_attendance(
    event_id: str,
    attendance_id: str,
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

    att = (
        await session.execute(
            select(Attendance).where(
                Attendance.id == attendance_id,
                Attendance.event_id == event.id,
            )
        )
    ).scalar_one_or_none()
    if not att:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    await session.execute(delete(Attendance).where(Attendance.id == att.id))
    await session.commit()

    total_att = (
        await session.execute(
            select(func.count(Attendance.id)).where(Attendance.event_id == event.id)
        )
    ).scalar() or 0

    return {
        "status": "success",
        "message": "Attendee removed from event",
        "registeredAttendees": int(total_att),
    }


# ---------------------------------------------------------------------------
# Event Reminders
# ---------------------------------------------------------------------------
@router.post("/{event_id}/reminders")
async def send_event_reminders(
    event_id: str,
    payload: EventReminderPayload,
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

    att_cnt = (
        await session.execute(
            select(func.count(Attendance.id)).where(Attendance.event_id == event.id)
        )
    ).scalar() or 0

    if att_cnt > 0:
        recipient_count = int(att_cnt)
    else:
        recipient_count = (
            await session.execute(
                select(func.count(Member.id)).where(
                    Member.church_id == actor.church_id,
                    Member.deleted_at.is_(None),
                )
            )
        ).scalar() or 1

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    event_meta_map = dict(meta.get("event_meta") or {})
    entry = dict(event_meta_map.get(str(event.id)) or {})
    entry["remindersSentCount"] = int(entry.get("remindersSentCount") or 0) + 1
    entry["lastReminderSentAt"] = _iso(_now())
    entry["lastReminderChannel"] = payload.channel or "all"
    if payload.message:
        entry["lastReminderMessage"] = payload.message.strip()
    event_meta_map[str(event.id)] = entry
    meta["event_meta"] = event_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    queue_audit(
        background_tasks,
        request,
        "SEND_EVENT_REMINDER",
        "EVENT",
        event.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    return {
        "status": "success",
        "message": f"Event reminders sent to {recipient_count} recipient(s)",
        "recipientCount": recipient_count,
        "remindersSentCount": entry["remindersSentCount"],
        "lastReminderSentAt": entry["lastReminderSentAt"],
    }

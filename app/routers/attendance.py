from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional
import uuid

from app.core.deps import CurrentUser, DbSession, church_id_from, resolve_actor
from app.enums import AttendanceStatus
from app.models.event import Attendance, Event
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


class AttendanceRecordItem(BaseModel):
    userId: Optional[str] = None
    memberId: Optional[str] = None
    status: Optional[str] = "present"
    checkInTime: Optional[str] = None
    checkOutTime: Optional[str] = None
    notes: Optional[str] = None


class AttendancePayload(BaseModel):
    serviceScheduleId: Optional[str] = None
    eventId: Optional[str] = None
    attendanceDate: Optional[str] = None
    records: Optional[List[AttendanceRecordItem]] = None
    remarks: Optional[str] = None
    weatherConditions: Optional[str] = None
    specialEvents: Optional[List[str]] = None


async def _get_or_create_default_event(session: DbSession, church_id: str) -> str:
    evt = (
        await session.execute(
            select(Event.id)
            .where(Event.church_id == church_id)
            .order_by(Event.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if evt:
        return str(evt)
    # create default event
    new_evt = Event(
        name="General Worship Service",
        church_id=church_id,
        start_time=_now(),
    )
    session.add(new_evt)
    await session.commit()
    await session.refresh(new_evt)
    return str(new_evt.id)


@router.get("")
@router.get("/")
async def list_attendance(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: str = Query("", max_length=100),
    status: Optional[str] = Query(None),
    branchId: Optional[str] = Query(None),
    serviceScheduleId: Optional[str] = Query(None),
    startDate: Optional[str] = Query(None),
    endDate: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )

    query = (
        select(Attendance, Member, User, Event)
        .join(Member, Member.id == Attendance.member_id)
        .outerjoin(User, User.id == Member.user_id)
        .join(Event, Event.id == Attendance.event_id)
        .where(Event.church_id == actor.church_id)
    )

    if search:
        s = f"%{search.strip()}%"
        query = query.where(
            or_(
                Member.first_name.ilike(s),
                Member.last_name.ilike(s),
                Member.membership_number.ilike(s),
                Event.name.ilike(s),
            )
        )

    total = (
        await session.execute(
            select(func.count(Attendance.id))
            .join(Event, Event.id == Attendance.event_id)
            .where(Event.church_id == actor.church_id)
        )
    ).scalar() or 0

    offset = (page - 1) * limit
    rows = (
        (
            await session.execute(
                query.order_by(Attendance.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .all()
    )

    # Format into groups or list
    records = []
    for att, m, u, evt in rows:
        first_name = (m.first_name if m and m.first_name else "") or ""
        last_name = (m.last_name if m and m.last_name else "") or ""
        if not first_name and not last_name and u and u.display_name:
            parts = u.display_name.strip().split(" ", 1)
            first_name = parts[0] if parts else ""
            last_name = parts[1] if len(parts) > 1 else ""
        user_info = {
            "_id": str(m.id),
            "id": str(m.id),
            "firstName": first_name,
            "lastName": last_name,
            "email": (m.email if m and m.email else (u.email if u else "")) or "",
            "membershipNumber": m.membership_number or "",
        }
        records.append(
            {
                "_id": str(att.id),
                "id": str(att.id),
                "attendanceDate": _iso(att.checked_in_at or att.created_at),
                "date": _iso(att.checked_in_at or att.created_at),
                "serviceScheduleId": {
                    "_id": str(evt.id),
                    "name": evt.name,
                },
                "event": {
                    "_id": str(evt.id),
                    "name": evt.name,
                },
                "records": [
                    {
                        "_id": str(att.id),
                        "userId": user_info,
                        "member": user_info,
                        "status": att.status.value.lower(),
                        "checkInTime": _iso(att.checked_in_at),
                    }
                ],
                "totalPresent": 1 if att.status == AttendanceStatus.PRESENT else 0,
                "totalAbsent": 1 if att.status == AttendanceStatus.ABSENT else 0,
                "totalLate": 1 if att.status == AttendanceStatus.LATE else 0,
                "status": "approved",
                "remarks": "Service attendance record",
                "createdAt": _iso(att.created_at),
            }
        )

    pages = math.ceil(total / limit) if limit else 1
    response.headers["X-Total-Count"] = str(total)

    return {
        "attendance": records,
        "attendances": records,
        "data": records,
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
async def create_attendance(
    request: Request,
    payload: AttendancePayload,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )

    event_id = payload.eventId or payload.serviceScheduleId
    if not event_id:
        event_id = await _get_or_create_default_event(session, actor.church_id)

    # Check event exists
    event_exists = (
        await session.execute(
            select(Event.id).where(
                Event.id == event_id, Event.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not event_exists:
        event_id = await _get_or_create_default_event(session, actor.church_id)

    records = payload.records or []
    created_attendances = []

    for item in records:
        raw_mem_id = item.userId or item.memberId
        if not raw_mem_id:
            continue
        mem_exists = (
            await session.execute(
                select(Member.id).where(
                    or_(Member.id == raw_mem_id, Member.user_id == raw_mem_id),
                    Member.church_id == actor.church_id,
                    Member.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if not mem_exists:
            continue
        mem_id = str(mem_exists)

        raw_status = (item.status or "present").upper()
        status_enum = (
            AttendanceStatus[raw_status]
            if raw_status in AttendanceStatus.__members__
            else AttendanceStatus.PRESENT
        )

        existing = (
            await session.execute(
                select(Attendance).where(
                    Attendance.event_id == event_id, Attendance.member_id == mem_id
                )
            )
        ).scalar_one_or_none()

        if existing:
            existing.status = status_enum
            existing.checked_in_at = _now()
            created_attendances.append(existing)
        else:
            att = Attendance(
                event_id=event_id,
                member_id=mem_id,
                status=status_enum,
                checked_in_at=_now(),
            )
            session.add(att)
            created_attendances.append(att)

    await session.commit()

    first_att_id = str(created_attendances[0].id) if created_attendances else str(uuid.uuid4())
    res_obj = {
        "_id": first_att_id,
        "id": first_att_id,
        "attendanceDate": payload.attendanceDate or _iso(_now()),
        "status": "approved",
        "message": "Attendance record has been created successfully.",
    }

    queue_audit(
        background_tasks,
        request,
        "CREATE_ATTENDANCE",
        "ATTENDANCE",
        first_att_id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    return {
        "message": "Attendance record has been created successfully.",
        "attendance": res_obj,
        "data": res_obj,
    }


@router.get("/{attendance_id}")
async def get_attendance(
    attendance_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    att_row = (
        await session.execute(
            select(Attendance, Member, User, Event)
            .join(Member, Member.id == Attendance.member_id)
            .outerjoin(User, User.id == Member.user_id)
            .join(Event, Event.id == Attendance.event_id)
            .where(
                Attendance.id == attendance_id,
                Event.church_id == actor.church_id,
            )
        )
    ).first()

    if not att_row:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    att, m, u, evt = att_row
    first_name = (m.first_name if m and m.first_name else "") or ""
    last_name = (m.last_name if m and m.last_name else "") or ""
    if not first_name and not last_name and u and u.display_name:
        parts = u.display_name.strip().split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""
    user_info = {
        "_id": str(m.id),
        "id": str(m.id),
        "firstName": first_name,
        "lastName": last_name,
        "email": (m.email if m and m.email else (u.email if u else "")) or "",
        "membershipNumber": m.membership_number or "",
    }
    res_obj = {
        "_id": str(att.id),
        "id": str(att.id),
        "attendanceDate": _iso(att.checked_in_at or att.created_at),
        "date": _iso(att.checked_in_at or att.created_at),
        "serviceScheduleId": {"_id": str(evt.id), "name": evt.name},
        "records": [
            {
                "_id": str(att.id),
                "userId": user_info,
                "status": att.status.value.lower(),
                "checkInTime": _iso(att.checked_in_at),
            }
        ],
        "status": "approved",
    }
    return {"attendance": res_obj, "data": res_obj}


@router.put("/{attendance_id}")
async def update_attendance(
    attendance_id: str,
    payload: AttendancePayload,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    att = (
        await session.execute(
            select(Attendance)
            .join(Event, Event.id == Attendance.event_id)
            .where(
                Attendance.id == attendance_id,
                Event.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()

    if not att:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    if payload.records and len(payload.records) > 0:
        raw_status = (payload.records[0].status or "present").upper()
        if raw_status in AttendanceStatus.__members__:
            att.status = AttendanceStatus[raw_status]

    att.updated_at = _now()
    await session.commit()

    res_obj = {
        "_id": str(att.id),
        "id": str(att.id),
        "status": att.status.value.lower(),
    }
    return {
        "message": "Attendance record has been updated successfully.",
        "attendance": res_obj,
        "data": res_obj,
    }


@router.delete("/{attendance_id}")
async def delete_attendance(
    attendance_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    force: bool = Query(False),
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    att = (
        await session.execute(
            select(Attendance)
            .join(Event, Event.id == Attendance.event_id)
            .where(
                Attendance.id == attendance_id,
                Event.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()

    if not att:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    await session.delete(att)
    await session.commit()

    return {"message": "Attendance record has been deleted successfully."}


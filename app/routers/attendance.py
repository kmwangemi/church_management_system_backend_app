from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional
import uuid

from app.core.deps import CurrentUser, DbSession, church_id_from, resolve_actor
from app.enums import AttendanceStatus
from app.models.church import Branch, Church
from app.models.event import Attendance, Event
from app.models.member import Member
from app.models.user import User
from app.routers.admin import _extract_branch_schedules
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


async def _get_church(session: DbSession, church_id: str) -> Church:
    church = (
        await session.execute(select(Church).where(Church.id == church_id))
    ).scalar_one_or_none()
    if not church:
        raise HTTPException(status_code=404, detail="Church not found")
    return church


async def _resolve_schedule_info(
    session: DbSession, church_id: str, schedule_id: Optional[str]
) -> Dict[str, Any]:
    branches = (
        await session.execute(
            select(Branch).where(
                Branch.church_id == church_id, Branch.deleted_at.is_(None)
            )
        )
    ).scalars().all()
    all_schedules: List[Dict[str, Any]] = []
    for b in branches:
        all_schedules.extend(_extract_branch_schedules(b))

    if schedule_id:
        for s in all_schedules:
            if s.get("id") == schedule_id or s.get("_id") == schedule_id:
                return s

        evt = (
            await session.execute(
                select(Event).where(
                    Event.id == schedule_id, Event.church_id == church_id
                )
            )
        ).scalar_one_or_none()
        if evt:
            return {
                "id": str(evt.id),
                "_id": str(evt.id),
                "service": evt.name,
                "name": evt.name,
                "day": evt.start_time.strftime("%A").lower() if evt.start_time else "sunday",
                "time": evt.start_time.strftime("%H:%M") if evt.start_time else "09:00",
                "type": "worship",
                "branchId": {"_id": str(evt.branch_id), "id": str(evt.branch_id)}
                if evt.branch_id
                else None,
            }

    if all_schedules:
        return all_schedules[0]

    return {
        "id": f"{church_id}-0",
        "_id": f"{church_id}-0",
        "service": "Sunday Morning Service",
        "name": "Sunday Morning Service",
        "day": "sunday",
        "time": "09:00",
        "type": "worship",
        "branchId": None,
    }


def _format_user_info(m: Member, u: Optional[User] = None) -> Dict[str, Any]:
    first_name = (m.first_name or "").strip()
    last_name = (m.last_name or "").strip()
    if not first_name and not last_name and u and u.display_name:
        parts = u.display_name.strip().split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""
    email = (m.email or (u.email if u else "")) or ""
    avatar = (m.photo_url or (u.profile_picture_url if u else "")) or ""
    return {
        "_id": str(m.id),
        "id": str(m.id),
        "firstName": first_name,
        "lastName": last_name,
        "email": email,
        "phoneNumber": m.phone_number or "",
        "profilePictureUrl": avatar,
        "membershipNumber": m.membership_number or "",
    }


def _format_attendance_session(
    evt: Event,
    att_rows: List[Any],
    session_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    meta = dict(session_meta or {})
    member_notes_map: Dict[str, Any] = dict(meta.get("memberNotes") or {})

    records = []
    total_present = 0
    total_late = 0
    total_absent = 0
    total_excused = 0

    for att, m, u in att_rows:
        st_low = (
            att.status.value.lower()
            if hasattr(att.status, "value")
            else str(att.status).lower()
        )
        if st_low == "present":
            total_present += 1
        elif st_low == "late":
            total_late += 1
        elif st_low == "absent":
            total_absent += 1
        elif st_low == "excused":
            total_excused += 1

        m_note = dict(member_notes_map.get(str(m.id)) or {})
        user_info = _format_user_info(m, u)
        records.append(
            {
                "_id": str(att.id),
                "id": str(att.id),
                "userId": user_info,
                "member": user_info,
                "status": st_low,
                "checkInTime": m_note.get("checkInTime")
                or _iso(att.checked_in_at or att.created_at),
                "checkOutTime": m_note.get("checkOutTime"),
                "notes": m_note.get("notes") or "",
            }
        )

    total_records = len(records)
    total_attended = total_present + total_late
    attendance_pct = (
        round((total_attended / total_records) * 100, 1)
        if total_records > 0
        else 0.0
    )

    date_str = meta.get("attendanceDate") or (
        evt.start_time.strftime("%Y-%m-%d")
        if evt.start_time
        else _now().strftime("%Y-%m-%d")
    )
    sched_id = meta.get("serviceScheduleId") or str(evt.id)
    svc_name = meta.get("serviceName") or evt.name or "Sunday Service"
    sched_obj = {
        "_id": sched_id,
        "id": sched_id,
        "service": svc_name,
        "name": svc_name,
        "day": meta.get("day") or "sunday",
        "time": meta.get("time") or "09:00",
        "type": meta.get("type") or "worship",
    }

    return {
        "_id": str(evt.id),
        "id": str(evt.id),
        "churchId": str(evt.church_id),
        "branchId": str(evt.branch_id) if evt.branch_id else "",
        "serviceScheduleId": sched_obj,
        "event": {"_id": str(evt.id), "id": str(evt.id), "name": svc_name},
        "attendanceDate": date_str,
        "date": date_str,
        "totalExpected": total_records,
        "totalPresent": total_present,
        "totalLate": total_late,
        "totalAbsent": total_absent,
        "totalExcused": total_excused,
        "totalAttended": total_attended,
        "attendancePercentage": attendance_pct,
        "records": records,
        "status": meta.get("status") or "approved",
        "remarks": meta.get("remarks") or "",
        "weatherConditions": meta.get("weatherConditions") or "",
        "specialEvents": meta.get("specialEvents") or [],
        "isActive": bool(meta.get("isActive", True)),
        "createdAt": _iso(evt.created_at) or "",
        "updatedAt": _iso(evt.updated_at) or "",
    }


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
    status: Optional[str] = None
    isActive: Optional[bool] = None


class QuickCheckInPayload(BaseModel):
    memberId: str
    serviceScheduleId: Optional[str] = None
    attendanceDate: Optional[str] = None
    status: Optional[str] = "present"
    notes: Optional[str] = None


@router.get("")
@router.get("/")
async def list_attendance(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
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
    church = await _get_church(session, actor.church_id)
    sessions_meta_map: Dict[str, Any] = dict(
        (church.metadata_json or {}).get("attendance_sessions") or {}
    )

    rows = (
        await session.execute(
            select(Attendance, Member, User, Event)
            .join(Member, Member.id == Attendance.member_id)
            .outerjoin(User, User.id == Member.user_id)
            .join(Event, Event.id == Attendance.event_id)
            .where(Event.church_id == actor.church_id)
            .order_by(Event.start_time.desc(), Attendance.created_at.asc())
        )
    ).all()

    grouped_events: Dict[str, Event] = {}
    grouped_rows: Dict[str, List[Any]] = {}
    for att, m, u, evt in rows:
        eid = str(evt.id)
        grouped_events[eid] = evt
        grouped_rows.setdefault(eid, []).append((att, m, u))

    all_sessions: List[Dict[str, Any]] = []
    for eid, evt in grouped_events.items():
        s_meta = sessions_meta_map.get(eid)
        formatted = _format_attendance_session(evt, grouped_rows[eid], s_meta)
        all_sessions.append(formatted)

    # Sort sessions newest date first
    all_sessions.sort(
        key=lambda x: (x.get("attendanceDate") or "", x.get("createdAt") or ""),
        reverse=True,
    )

    if startDate:
        sd = startDate[:10]
        all_sessions = [
            s for s in all_sessions if (s.get("attendanceDate") or "")[:10] >= sd
        ]
    if endDate:
        ed = endDate[:10]
        all_sessions = [
            s for s in all_sessions if (s.get("attendanceDate") or "")[:10] <= ed
        ]
    if status and status.lower() != "all":
        st_low = status.strip().lower()
        all_sessions = [
            s for s in all_sessions if (s.get("status") or "").lower() == st_low
        ]
    if serviceScheduleId:
        all_sessions = [
            s
            for s in all_sessions
            if s.get("id") == serviceScheduleId
            or (
                isinstance(s.get("serviceScheduleId"), dict)
                and s["serviceScheduleId"].get("_id") == serviceScheduleId
            )
        ]
    if search:
        q = search.strip().lower()
        filtered = []
        for s in all_sessions:
            svc_match = q in (
                (s.get("serviceScheduleId") or {}).get("service") or ""
            ).lower()
            mem_match = any(
                q in (r.get("userId", {}).get("firstName") or "").lower()
                or q in (r.get("userId", {}).get("lastName") or "").lower()
                or q in (r.get("userId", {}).get("email") or "").lower()
                for r in s.get("records", [])
            )
            if svc_match or mem_match:
                filtered.append(s)
        all_sessions = filtered

    total = len(all_sessions)
    offset = (page - 1) * limit
    paginated = all_sessions[offset : offset + limit]
    pages = max(1, math.ceil(total / limit)) if limit else 1
    response.headers["X-Total-Count"] = str(total)

    return {
        "attendance": paginated,
        "attendances": paginated,
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
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    sessions_meta_map: Dict[str, Any] = dict(meta.get("attendance_sessions") or {})

    raw_sched_id = payload.serviceScheduleId or payload.eventId
    sched_info = await _resolve_schedule_info(session, actor.church_id, raw_sched_id)
    sched_id = str(sched_info.get("_id") or sched_info.get("id") or f"{actor.church_id}-0")
    svc_name = sched_info.get("service") or sched_info.get("name") or "Sunday Service"

    clean_date = (
        payload.attendanceDate[:10]
        if payload.attendanceDate
        else _now().strftime("%Y-%m-%d")
    )
    try:
        dt_start = datetime.strptime(clean_date, "%Y-%m-%d").replace(
            hour=9, minute=0, tzinfo=timezone.utc
        )
    except Exception:
        dt_start = _now()

    # Check if an attendance session Event already exists for this date & schedule
    target_event: Optional[Event] = None
    for eid, sm in sessions_meta_map.items():
        if (
            isinstance(sm, dict)
            and sm.get("attendanceDate") == clean_date
            and sm.get("serviceScheduleId") == sched_id
        ):
            target_event = (
                await session.execute(
                    select(Event).where(
                        Event.id == eid, Event.church_id == actor.church_id
                    )
                )
            ).scalar_one_or_none()
            if target_event:
                break

    if not target_event and raw_sched_id:
        # Check if raw_sched_id is directly an Event ID on that date
        direct_evt = (
            await session.execute(
                select(Event).where(
                    Event.id == raw_sched_id, Event.church_id == actor.church_id
                )
            )
        ).scalar_one_or_none()
        if direct_evt:
            target_event = direct_evt

    branch_id = None
    b_obj = sched_info.get("branchId")
    if isinstance(b_obj, dict):
        cand_bid = b_obj.get("_id") or b_obj.get("id")
        if cand_bid:
            b_exists = (
                await session.execute(
                    select(Branch.id).where(
                        Branch.id == cand_bid, Branch.church_id == actor.church_id
                    )
                )
            ).scalar_one_or_none()
            if b_exists:
                branch_id = str(b_exists)

    if not target_event:
        target_event = Event(
            name=svc_name,
            church_id=actor.church_id,
            branch_id=branch_id,
            start_time=dt_start,
            description=payload.remarks or f"Attendance for {svc_name} on {clean_date}",
        )
        session.add(target_event)
        await session.flush()

    session_entry = dict(sessions_meta_map.get(str(target_event.id)) or {})
    member_notes_map = dict(session_entry.get("memberNotes") or {})

    for item in payload.records or []:
        raw_mem_id = item.userId or item.memberId
        if not raw_mem_id:
            continue
        mem = (
            await session.execute(
                select(Member)
                .where(
                    or_(Member.id == raw_mem_id, Member.user_id == raw_mem_id),
                    Member.church_id == actor.church_id,
                    Member.deleted_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if not mem:
            continue

        raw_status = (item.status or "present").strip().upper()
        status_enum = (
            AttendanceStatus[raw_status]
            if raw_status in AttendanceStatus.__members__
            else AttendanceStatus.PRESENT
        )

        existing = (
            await session.execute(
                select(Attendance).where(
                    Attendance.event_id == target_event.id,
                    Attendance.member_id == mem.id,
                )
            )
        ).scalar_one_or_none()

        if existing:
            existing.status = status_enum
            existing.checked_in_at = _now()
        else:
            att = Attendance(
                event_id=target_event.id,
                member_id=mem.id,
                checked_in_by_member_id=actor.member.id if actor.member else None,
                status=status_enum,
                checked_in_at=_now(),
            )
            session.add(att)

        member_notes_map[str(mem.id)] = {
            "notes": (item.notes or "").strip(),
            "checkInTime": item.checkInTime or _iso(_now()),
            "checkOutTime": item.checkOutTime,
        }

    session_entry.update(
        {
            "serviceScheduleId": sched_id,
            "serviceName": svc_name,
            "day": sched_info.get("day") or "sunday",
            "time": sched_info.get("time") or "09:00",
            "type": sched_info.get("type") or "worship",
            "attendanceDate": clean_date,
            "status": (payload.status or session_entry.get("status") or "approved").lower(),
            "remarks": (
                payload.remarks
                if payload.remarks is not None
                else session_entry.get("remarks", "")
            ),
            "weatherConditions": (
                payload.weatherConditions
                if payload.weatherConditions is not None
                else session_entry.get("weatherConditions", "")
            ),
            "specialEvents": (
                payload.specialEvents
                if payload.specialEvents is not None
                else session_entry.get("specialEvents", [])
            ),
            "memberNotes": member_notes_map,
            "isActive": True,
        }
    )
    sessions_meta_map[str(target_event.id)] = session_entry
    meta["attendance_sessions"] = sessions_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    await session.commit()

    att_rows = (
        await session.execute(
            select(Attendance, Member, User)
            .join(Member, Member.id == Attendance.member_id)
            .outerjoin(User, User.id == Member.user_id)
            .where(Attendance.event_id == target_event.id)
            .order_by(Attendance.created_at.asc())
        )
    ).all()

    res_obj = _format_attendance_session(target_event, att_rows, session_entry)

    queue_audit(
        background_tasks,
        request,
        "CREATE_ATTENDANCE",
        "ATTENDANCE",
        target_event.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    return {
        "message": "Attendance record has been created successfully.",
        "attendance": res_obj,
        "data": res_obj,
    }


@router.post("/quick-checkin")
async def quick_check_in(
    payload: QuickCheckInPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    clean_date = (
        payload.attendanceDate[:10]
        if payload.attendanceDate
        else _now().strftime("%Y-%m-%d")
    )
    return await create_attendance(
        request=request,
        payload=AttendancePayload(
            serviceScheduleId=payload.serviceScheduleId,
            attendanceDate=clean_date,
            records=[
                AttendanceRecordItem(
                    userId=payload.memberId,
                    memberId=payload.memberId,
                    status=payload.status or "present",
                    checkInTime=_iso(_now()),
                    notes=payload.notes,
                )
            ],
        ),
        background_tasks=background_tasks,
        current_user=current_user,
        session=session,
        church_id=church_id,
    )


@router.get("/{attendance_id}")
async def get_attendance(
    attendance_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    sessions_meta_map: Dict[str, Any] = dict(
        (church.metadata_json or {}).get("attendance_sessions") or {}
    )

    evt = (
        await session.execute(
            select(Event).where(
                Event.id == attendance_id, Event.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()

    if not evt:
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
        if att:
            evt = await session.get(Event, att.event_id)

    if not evt:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    att_rows = (
        await session.execute(
            select(Attendance, Member, User)
            .join(Member, Member.id == Attendance.member_id)
            .outerjoin(User, User.id == Member.user_id)
            .where(Attendance.event_id == evt.id)
            .order_by(Attendance.created_at.asc())
        )
    ).all()

    res_obj = _format_attendance_session(
        evt, att_rows, sessions_meta_map.get(str(evt.id))
    )
    return {"attendance": res_obj, "data": res_obj}


@router.put("/{attendance_id}")
@router.patch("/{attendance_id}")
async def update_attendance(
    attendance_id: str,
    payload: AttendancePayload,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    sessions_meta_map: Dict[str, Any] = dict(meta.get("attendance_sessions") or {})

    evt = (
        await session.execute(
            select(Event).where(
                Event.id == attendance_id, Event.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()

    single_att: Optional[Attendance] = None
    if not evt:
        single_att = (
            await session.execute(
                select(Attendance)
                .join(Event, Event.id == Attendance.event_id)
                .where(
                    Attendance.id == attendance_id,
                    Event.church_id == actor.church_id,
                )
            )
        ).scalar_one_or_none()
        if single_att:
            evt = await session.get(Event, single_att.event_id)

    if not evt:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    session_entry = dict(sessions_meta_map.get(str(evt.id)) or {})
    member_notes_map = dict(session_entry.get("memberNotes") or {})

    if single_att and payload.records and len(payload.records) == 1 and not payload.serviceScheduleId:
        item = payload.records[0]
        raw_status = (item.status or "present").strip().upper()
        if raw_status in AttendanceStatus.__members__:
            single_att.status = AttendanceStatus[raw_status]
        if item.notes is not None:
            m_note = dict(member_notes_map.get(str(single_att.member_id)) or {})
            m_note["notes"] = item.notes.strip()
            member_notes_map[str(single_att.member_id)] = m_note
            session_entry["memberNotes"] = member_notes_map
    elif payload.records is not None:
        # Full session records update
        wanted_member_ids = set()
        for item in payload.records:
            raw_mem_id = item.userId or item.memberId
            if not raw_mem_id:
                continue
            mem = (
                await session.execute(
                    select(Member)
                    .where(
                        or_(Member.id == raw_mem_id, Member.user_id == raw_mem_id),
                        Member.church_id == actor.church_id,
                        Member.deleted_at.is_(None),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if not mem:
                continue
            wanted_member_ids.add(str(mem.id))
            raw_status = (item.status or "present").strip().upper()
            status_enum = (
                AttendanceStatus[raw_status]
                if raw_status in AttendanceStatus.__members__
                else AttendanceStatus.PRESENT
            )
            existing = (
                await session.execute(
                    select(Attendance).where(
                        Attendance.event_id == evt.id,
                        Attendance.member_id == mem.id,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                existing.status = status_enum
            else:
                session.add(
                    Attendance(
                        event_id=evt.id,
                        member_id=mem.id,
                        checked_in_by_member_id=actor.member.id if actor.member else None,
                        status=status_enum,
                        checked_in_at=_now(),
                    )
                )
            prev_note = dict(member_notes_map.get(str(mem.id)) or {})
            member_notes_map[str(mem.id)] = {
                "notes": (
                    item.notes.strip()
                    if item.notes is not None
                    else prev_note.get("notes", "")
                ),
                "checkInTime": item.checkInTime
                or prev_note.get("checkInTime")
                or _iso(_now()),
                "checkOutTime": item.checkOutTime or prev_note.get("checkOutTime"),
            }

        # Remove unselected members from this session if records list was non-empty
        if wanted_member_ids:
            existing_rows = (
                await session.execute(
                    select(Attendance).where(Attendance.event_id == evt.id)
                )
            ).scalars().all()
            for r in existing_rows:
                if str(r.member_id) not in wanted_member_ids:
                    await session.delete(r)

        session_entry["memberNotes"] = member_notes_map

    if payload.serviceScheduleId:
        sched_info = await _resolve_schedule_info(
            session, actor.church_id, payload.serviceScheduleId
        )
        session_entry["serviceScheduleId"] = str(
            sched_info.get("_id") or sched_info.get("id")
        )
        session_entry["serviceName"] = (
            sched_info.get("service") or sched_info.get("name") or evt.name
        )
        evt.name = session_entry["serviceName"]

    if payload.attendanceDate:
        session_entry["attendanceDate"] = payload.attendanceDate[:10]
    if payload.remarks is not None:
        session_entry["remarks"] = payload.remarks.strip()
    if payload.weatherConditions is not None:
        session_entry["weatherConditions"] = payload.weatherConditions.strip()
    if payload.specialEvents is not None:
        session_entry["specialEvents"] = payload.specialEvents
    if payload.status is not None:
        session_entry["status"] = payload.status.strip().lower()
    if payload.isActive is not None:
        session_entry["isActive"] = bool(payload.isActive)

    sessions_meta_map[str(evt.id)] = session_entry
    meta["attendance_sessions"] = sessions_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    evt.updated_at = _now()
    await session.commit()

    att_rows = (
        await session.execute(
            select(Attendance, Member, User)
            .join(Member, Member.id == Attendance.member_id)
            .outerjoin(User, User.id == Member.user_id)
            .where(Attendance.event_id == evt.id)
            .order_by(Attendance.created_at.asc())
        )
    ).all()

    res_obj = _format_attendance_session(evt, att_rows, session_entry)
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
    evt = (
        await session.execute(
            select(Event).where(
                Event.id == attendance_id, Event.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()

    if evt:
        await session.execute(
            delete(Attendance).where(Attendance.event_id == evt.id)
        )
        church = await _get_church(session, actor.church_id)
        meta = dict(church.metadata_json or {})
        sessions_meta_map = dict(meta.get("attendance_sessions") or {})
        sessions_meta_map.pop(str(evt.id), None)
        meta["attendance_sessions"] = sessions_meta_map
        church.metadata_json = meta
        flag_modified(church, "metadata_json")
        await session.commit()
        return {"message": "Attendance session has been deleted successfully."}

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

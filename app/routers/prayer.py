from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional, Tuple
import uuid

from app.core.deps import CurrentUser, DbSession, church_id_from, resolve_actor
from app.enums import MembershipStatus, PrayerStatus, PrayerVisibility
from app.models.church import Church
from app.models.detail import BishopDetails, PastorDetails
from app.models.member import Member
from app.models.operation import PrayerRequest
from app.models.user import User
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm.attributes import flag_modified

router = APIRouter()

STANDARD_CATEGORIES = [
    ("health", "Health"),
    ("family", "Family"),
    ("career", "Career"),
    ("financial", "Financial"),
    ("spiritual", "Spiritual"),
    ("thanksgiving", "Thanksgiving"),
    ("guidance", "Guidance"),
    ("other", "Other"),
]


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


def _normalize_category(raw: Optional[str]) -> str:
    if not raw:
        return "Other"
    val = raw.strip().lower()
    mapping = {
        "health": "Health",
        "health & healing": "Health",
        "family": "Family",
        "family & relationships": "Family",
        "relationship": "Family",
        "relationships": "Family",
        "career": "Career",
        "career & work": "Career",
        "financial": "Financial",
        "finance": "Financial",
        "spiritual": "Spiritual",
        "spiritual growth": "Spiritual",
        "thanksgiving": "Thanksgiving",
        "thanksgiving & praise": "Thanksgiving",
        "guidance": "Guidance",
        "guidance & wisdom": "Guidance",
        "other": "Other",
        "general": "Other",
    }
    return mapping.get(val, raw.strip().title())


def _normalize_priority(raw: Optional[str]) -> str:
    if not raw:
        return "Medium"
    val = raw.strip().lower()
    mapping = {
        "low": "Low",
        "medium": "Medium",
        "high": "High",
        "urgent": "High",
    }
    return mapping.get(val, "Medium")


def _normalize_status_display(
    pr_status: PrayerStatus, meta_status: Optional[str] = None
) -> str:
    if meta_status:
        ms = meta_status.strip().lower()
        if ms in ("in progress", "in_progress", "progress"):
            return "In Progress"
        if ms == "answered":
            return "Answered"
        if ms == "closed":
            return "Closed"
        if ms == "active":
            return "Active"
    st_val = pr_status.value.upper() if hasattr(pr_status, "value") else str(pr_status).upper()
    if st_val == "ANSWERED":
        return "Answered"
    if st_val == "CLOSED":
        return "Closed"
    return "Active"


async def _ensure_counselors(
    session: DbSession, church: Church, meta: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], bool]:
    existing = meta.get("prayer_counselors")
    if isinstance(existing, list):
        return list(existing), False

    # Seed initial counselors from church pastors/bishops or active leaders if available
    seeded: List[Dict[str, Any]] = []
    pastor_rows = (
        await session.execute(
            select(Member, PastorDetails)
            .join(PastorDetails, PastorDetails.member_id == Member.id)
            .where(
                Member.church_id == church.id,
                Member.deleted_at.is_(None),
            )
            .limit(10)
        )
    ).all()
    for m, pd in pastor_rows:
        cid = f"cns_{str(m.id)[:8]}"
        seeded.append(
            {
                "id": cid,
                "_id": cid,
                "memberId": str(m.id),
                "name": m.full_name,
                "email": m.email or "",
                "phone": m.phone_number or "",
                "specialization": ["General Counseling", "Spiritual Guidance", "Family"],
                "maxCapacity": 10,
                "experience": "5+ years",
                "status": "Available",
                "avatar": m.photo_url or "",
                "createdAt": _iso(_now()),
            }
        )

    bishop_rows = (
        await session.execute(
            select(Member, BishopDetails)
            .join(BishopDetails, BishopDetails.member_id == Member.id)
            .where(
                Member.church_id == church.id,
                Member.deleted_at.is_(None),
            )
            .limit(5)
        )
    ).all()
    seen_member_ids = {c.get("memberId") for c in seeded}
    for m, bd in bishop_rows:
        if str(m.id) in seen_member_ids:
            continue
        cid = f"cns_{str(m.id)[:8]}"
        seeded.append(
            {
                "id": cid,
                "_id": cid,
                "memberId": str(m.id),
                "name": m.full_name,
                "email": m.email or "",
                "phone": m.phone_number or "",
                "specialization": ["Leadership", "Marriage", "Spiritual Guidance"],
                "maxCapacity": 12,
                "experience": "10+ years",
                "status": "Available",
                "avatar": m.photo_url or "",
                "createdAt": _iso(_now()),
            }
        )

    meta["prayer_counselors"] = seeded
    return seeded, True


def _format_prayer(
    pr: PrayerRequest,
    m: Optional[Member] = None,
    u: Optional[User] = None,
    pmeta: Optional[Dict[str, Any]] = None,
    counselors_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    pm = pmeta or {}
    is_anon = bool(pm.get("isAnonymous", m is None))
    if is_anon:
        requester = "Anonymous"
        requester_avatar = None
    else:
        requester = (
            f"{m.first_name or ''} {m.last_name or ''}".strip()
            if m
            else (u.display_name if u and u.display_name else pm.get("requesterName") or "Church Member")
        ) or "Church Member"
        requester_avatar = m.photo_url if m else None

    cnt = int(pm.get("prayerCount", 1) or 0)
    category = _normalize_category(pm.get("category") or "Other")
    priority = _normalize_priority(pm.get("priority") or "Medium")
    status_display = _normalize_status_display(pr.status, pm.get("status_display"))
    updates = list(pm.get("updates") or [])
    counselor_id = pm.get("counselorId") or None
    counselor_name = pm.get("counselorName") or None
    if counselor_id and counselors_by_id and counselor_id in counselors_by_id:
        counselor_name = counselors_by_id[counselor_id].get("name") or counselor_name

    last_update_dt = _iso(pr.updated_at or pr.created_at)
    if updates and isinstance(updates[-1], dict) and updates[-1].get("date"):
        last_update_dt = updates[-1]["date"]

    return {
        "_id": str(pr.id),
        "id": str(pr.id),
        "title": pr.title,
        "description": pr.content,
        "content": pr.content,
        "category": category,
        "priority": priority,
        "status": status_display,
        "isPublic": pr.visibility == PrayerVisibility.PUBLIC,
        "isAnonymous": is_anon,
        "requester": requester,
        "requesterAvatar": requester_avatar,
        "memberId": None if is_anon else (str(m.id) if m else pm.get("memberId")),
        "author": requester,
        "prayerCount": cnt,
        "prayingCount": cnt,
        "dateSubmitted": _iso(pr.created_at),
        "lastUpdate": last_update_dt,
        "updates": updates,
        "counselorId": counselor_id,
        "counselorName": counselor_name,
        "createdAt": _iso(pr.created_at),
        "updatedAt": _iso(pr.updated_at),
    }


def _enrich_counselors(
    counselors: List[Dict[str, Any]],
    formatted_prayers: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    cases_by_counselor: Dict[str, List[Dict[str, Any]]] = {}
    for p in formatted_prayers:
        cid = p.get("counselorId")
        if cid and p.get("status") in ("Active", "In Progress"):
            cases_by_counselor.setdefault(cid, []).append(
                {
                    "id": p["id"],
                    "title": p["title"],
                    "category": p["category"],
                    "priority": p["priority"],
                    "status": p["status"],
                    "requester": p["requester"],
                    "dateSubmitted": p["dateSubmitted"],
                }
            )

    enriched = []
    for c in counselors:
        cid = c.get("id") or c.get("_id")
        assigned = cases_by_counselor.get(cid, [])
        max_cap = max(1, int(c.get("maxCapacity", 10) or 10))
        current_cases = len(assigned)
        raw_status = c.get("status") or "Available"
        if raw_status == "Unavailable":
            effective_status = "Unavailable"
        elif current_cases >= max_cap:
            effective_status = "Full"
        else:
            effective_status = "Available"

        specs = c.get("specialization")
        if isinstance(specs, str):
            spec_list = [s.strip() for s in specs.split(",") if s.strip()]
        elif isinstance(specs, list):
            spec_list = [str(s).strip() for s in specs if str(s).strip()]
        else:
            spec_list = ["General Counseling"]

        enriched.append(
            {
                **c,
                "id": cid,
                "_id": cid,
                "specialization": spec_list,
                "currentCases": current_cases,
                "maxCapacity": max_cap,
                "status": effective_status,
                "experience": c.get("experience") or "3+ years",
                "assignedCases": assigned,
            }
        )
    return enriched


class PrayerCreatePayload(BaseModel):
    memberId: Optional[str] = None
    title: str
    description: str
    category: Optional[str] = "Other"
    priority: Optional[str] = "Medium"
    isAnonymous: Optional[bool] = False
    isPublic: Optional[bool] = True
    status: Optional[str] = "Active"
    counselorId: Optional[str] = None


class PrayerUpdatePayload(BaseModel):
    memberId: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    priority: Optional[str] = None
    isAnonymous: Optional[bool] = None
    isPublic: Optional[bool] = None
    status: Optional[str] = None
    counselorId: Optional[str] = None


class PrayerCountPayload(BaseModel):
    prayerRequestId: str


async def _load_all_formatted_prayers(
    session: DbSession,
    church_id: str,
    prayer_meta_map: Dict[str, Any],
    counselors_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows = (
        await session.execute(
            select(PrayerRequest, Member, User)
            .outerjoin(Member, Member.id == PrayerRequest.member_id)
            .outerjoin(User, User.id == Member.user_id)
            .where(PrayerRequest.church_id == church_id)
            .order_by(PrayerRequest.created_at.desc())
        )
    ).all()
    return [
        _format_prayer(
            pr,
            m,
            u,
            prayer_meta_map.get(str(pr.id)),
            counselors_by_id,
        )
        for pr, m, u in rows
    ]


# ---------------------------------------------------------------------------
# Stats & Analytics (/church/prayer-requests/stats)
# ---------------------------------------------------------------------------
@router.get("/stats")
async def get_prayer_stats(
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
    prayer_meta_map: Dict[str, Any] = dict(meta.get("prayer_meta") or {})
    counselors, seeded = await _ensure_counselors(session, church, meta)
    if seeded:
        church.metadata_json = meta
        flag_modified(church, "metadata_json")
        await session.commit()

    counselors_by_id = {
        (c.get("id") or c.get("_id")): c for c in counselors if c.get("id") or c.get("_id")
    }
    all_prayers = await _load_all_formatted_prayers(
        session, actor.church_id, prayer_meta_map, counselors_by_id
    )
    enriched_counselors = _enrich_counselors(counselors, all_prayers)

    total_requests = len(all_prayers)
    active_requests = sum(
        1 for p in all_prayers if p["status"] in ("Active", "In Progress")
    )
    in_progress_requests = sum(
        1 for p in all_prayers if p["status"] == "In Progress"
    )
    answered_requests = sum(1 for p in all_prayers if p["status"] == "Answered")
    closed_requests = sum(1 for p in all_prayers if p["status"] == "Closed")
    total_prayers = sum(int(p.get("prayerCount", 0) or 0) for p in all_prayers)
    active_counselors = sum(
        1 for c in enriched_counselors if c.get("status") == "Available"
    )

    # Category breakdown
    cat_totals: Dict[str, Dict[str, int]] = {
        label: {"total": 0, "active": 0, "answered": 0}
        for _, label in STANDARD_CATEGORIES
    }
    for p in all_prayers:
        cat_label = p.get("category") or "Other"
        if cat_label not in cat_totals:
            cat_totals[cat_label] = {"total": 0, "active": 0, "answered": 0}
        cat_totals[cat_label]["total"] += 1
        if p["status"] in ("Active", "In Progress"):
            cat_totals[cat_label]["active"] += 1
        elif p["status"] == "Answered":
            cat_totals[cat_label]["answered"] += 1

    categories_out = []
    for slug, label in STANDARD_CATEGORIES:
        counts = cat_totals.get(label, {"total": 0, "active": 0, "answered": 0})
        pct = (
            round((counts["total"] / total_requests) * 100)
            if total_requests > 0
            else 0
        )
        categories_out.append(
            {
                "slug": slug,
                "name": label,
                "count": counts["total"],
                "activeCount": counts["active"],
                "answeredCount": counts["answered"],
                "percentage": pct,
            }
        )

    # Priority breakdown
    pri_counts = {"High": 0, "Medium": 0, "Low": 0}
    for p in all_prayers:
        pri = p.get("priority") or "Medium"
        pri_counts[pri] = pri_counts.get(pri, 0) + 1
    priorities_out = [
        {
            "priority": k,
            "count": v,
            "percentage": (
                round((v / total_requests) * 100) if total_requests > 0 else 0
            ),
        }
        for k, v in pri_counts.items()
    ]

    return {
        "success": True,
        "data": {
            "totalRequests": total_requests,
            "activeRequests": active_requests,
            "inProgressRequests": in_progress_requests,
            "answeredRequests": answered_requests,
            "closedRequests": closed_requests,
            "totalPrayers": total_prayers,
            "activeCounselors": active_counselors,
            "totalCounselors": len(enriched_counselors),
            "categories": categories_out,
            "priorities": priorities_out,
            "counselorWorkload": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "currentCases": c["currentCases"],
                    "maxCapacity": c["maxCapacity"],
                    "status": c["status"],
                    "percentage": round(
                        (c["currentCases"] / max(1, c["maxCapacity"])) * 100
                    ),
                }
                for c in enriched_counselors
            ],
        },
    }


# ---------------------------------------------------------------------------
# Counselors CRUD (/church/prayer-requests/counselors)
# ---------------------------------------------------------------------------
@router.get("/counselors")
async def list_counselors(
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
    prayer_meta_map: Dict[str, Any] = dict(meta.get("prayer_meta") or {})
    counselors, seeded = await _ensure_counselors(session, church, meta)
    if seeded:
        church.metadata_json = meta
        flag_modified(church, "metadata_json")
        await session.commit()

    counselors_by_id = {
        (c.get("id") or c.get("_id")): c for c in counselors if c.get("id") or c.get("_id")
    }
    all_prayers = await _load_all_formatted_prayers(
        session, actor.church_id, prayer_meta_map, counselors_by_id
    )
    enriched = _enrich_counselors(counselors, all_prayers)
    return {
        "success": True,
        "data": {
            "counselors": enriched,
            "total": len(enriched),
        },
        "counselors": enriched,
    }


@router.post("/counselors")
async def create_counselor(
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    counselors, _ = await _ensure_counselors(session, church, meta)

    member_id = payload.get("memberId")
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip()
    phone = (payload.get("phone") or "").strip()
    avatar = ""

    if member_id:
        mem_obj = (
            await session.execute(
                select(Member).where(
                    or_(Member.id == member_id, Member.user_id == member_id),
                    Member.church_id == actor.church_id,
                    Member.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if mem_obj:
            member_id = str(mem_obj.id)
            name = name or mem_obj.full_name
            email = email or (mem_obj.email or "")
            phone = phone or (mem_obj.phone_number or "")
            avatar = mem_obj.photo_url or ""

    if not name:
        raise HTTPException(status_code=400, detail="Counselor name is required")

    specs = payload.get("specialization")
    if isinstance(specs, str):
        spec_list = [s.strip() for s in specs.split(",") if s.strip()]
    elif isinstance(specs, list):
        spec_list = [str(s).strip() for s in specs if str(s).strip()]
    else:
        spec_list = ["General Counseling"]
    if not spec_list:
        spec_list = ["General Counseling"]

    cid = f"cns_{uuid.uuid4().hex[:10]}"
    max_cap = max(1, int(payload.get("maxCapacity", 10) or 10))
    new_counselor = {
        "id": cid,
        "_id": cid,
        "memberId": member_id,
        "name": name,
        "email": email,
        "phone": phone,
        "specialization": spec_list,
        "maxCapacity": max_cap,
        "currentCases": 0,
        "experience": (payload.get("experience") or "3+ years").strip(),
        "status": payload.get("status") or "Available",
        "avatar": avatar,
        "createdAt": _iso(_now()),
    }
    counselors.insert(0, new_counselor)
    meta["prayer_counselors"] = counselors
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "success": True,
        "data": new_counselor,
        "message": "Counselor added successfully",
    }


@router.put("/counselors/{counselor_id}")
@router.patch("/counselors/{counselor_id}")
async def update_counselor(
    counselor_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    counselors, _ = await _ensure_counselors(session, church, meta)

    target = next(
        (
            c
            for c in counselors
            if c.get("id") == counselor_id or c.get("_id") == counselor_id
        ),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail="Counselor not found")

    for k in ("name", "email", "phone", "experience", "status"):
        if k in payload and payload[k] is not None:
            target[k] = str(payload[k]).strip()

    if "maxCapacity" in payload and payload["maxCapacity"] is not None:
        target["maxCapacity"] = max(1, int(payload["maxCapacity"] or 10))

    if "specialization" in payload and payload["specialization"] is not None:
        specs = payload["specialization"]
        if isinstance(specs, str):
            target["specialization"] = [
                s.strip() for s in specs.split(",") if s.strip()
            ] or ["General Counseling"]
        elif isinstance(specs, list):
            target["specialization"] = [
                str(s).strip() for s in specs if str(s).strip()
            ] or ["General Counseling"]

    target["updatedAt"] = _iso(_now())
    meta["prayer_counselors"] = counselors
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "success": True,
        "data": target,
        "message": "Counselor updated successfully",
    }


@router.delete("/counselors/{counselor_id}")
async def delete_counselor(
    counselor_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    counselors, _ = await _ensure_counselors(session, church, meta)
    meta["prayer_counselors"] = [
        c
        for c in counselors
        if c.get("id") != counselor_id and c.get("_id") != counselor_id
    ]
    # Also unassign any prayer requests assigned to this counselor
    prayer_meta_map: Dict[str, Any] = dict(meta.get("prayer_meta") or {})
    for pid, pm in prayer_meta_map.items():
        if isinstance(pm, dict) and pm.get("counselorId") == counselor_id:
            pm["counselorId"] = None
            pm["counselorName"] = None
    meta["prayer_meta"] = prayer_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    return {"success": True, "message": "Counselor removed successfully"}


# ---------------------------------------------------------------------------
# Prayer Requests List & CRUD (/church/prayer-requests)
# ---------------------------------------------------------------------------
@router.get("")
@router.get("/")
async def list_prayer_requests(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=100),
    category: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    isPublic: Optional[bool] = Query(None),
    memberId: Optional[str] = Query(None),
    counselorId: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    prayer_meta_map: Dict[str, Any] = dict(meta.get("prayer_meta") or {})
    counselors, _ = await _ensure_counselors(session, church, meta)
    counselors_by_id = {
        (c.get("id") or c.get("_id")): c for c in counselors if c.get("id") or c.get("_id")
    }

    conds = [PrayerRequest.church_id == actor.church_id]
    if memberId:
        conds.append(PrayerRequest.member_id == memberId)
    if isPublic is not None:
        if isPublic:
            conds.append(PrayerRequest.visibility == PrayerVisibility.PUBLIC)
        else:
            conds.append(PrayerRequest.visibility != PrayerVisibility.PUBLIC)

    rows = (
        await session.execute(
            select(PrayerRequest, Member, User)
            .outerjoin(Member, Member.id == PrayerRequest.member_id)
            .outerjoin(User, User.id == Member.user_id)
            .where(*conds)
            .order_by(PrayerRequest.created_at.desc())
        )
    ).all()

    all_formatted = [
        _format_prayer(
            pr,
            m,
            u,
            prayer_meta_map.get(str(pr.id)),
            counselors_by_id,
        )
        for pr, m, u in rows
    ]

    # Apply rich filters across formatted records
    filtered = []
    for item in all_formatted:
        if search:
            q = search.strip().lower()
            haystack = f"{item.get('title', '')} {item.get('description', '')} {item.get('category', '')} {item.get('requester', '')} {item.get('counselorName', '')}".lower()
            if q not in haystack:
                continue
        if category and category.lower() not in ("all", ""):
            norm_cat = _normalize_category(category).lower()
            if (item.get("category") or "").lower() != norm_cat and (
                item.get("category") or ""
            ).lower() != category.lower():
                continue
        if priority and priority.lower() not in ("all", ""):
            norm_pri = _normalize_priority(priority).lower()
            if (item.get("priority") or "").lower() != norm_pri:
                continue
        if status and status.lower() not in ("all", ""):
            st_q = status.strip().lower()
            item_st = (item.get("status") or "Active").lower()
            if st_q in ("progress", "in_progress", "in progress"):
                if item_st != "in progress":
                    continue
            elif item_st != st_q:
                continue
        if counselorId and item.get("counselorId") != counselorId:
            continue
        filtered.append(item)

    total = len(filtered)
    pages = max(1, math.ceil(total / limit)) if limit else 1
    offset = (page - 1) * limit
    paginated = filtered[offset : offset + limit]
    response.headers["X-Total-Count"] = str(total)

    return {
        "success": True,
        "data": {
            "prayerRequests": paginated,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "pages": pages,
                "hasNext": page < pages,
                "hasPrev": page > 1,
            },
        },
        "prayerRequests": paginated,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": pages,
        },
    }


@router.post("")
@router.post("/")
async def create_prayer_request(
    request: Request,
    payload: PrayerCreatePayload,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church = await _get_church(session, actor.church_id)

    mem_id = None
    mem_obj = None
    if not payload.isAnonymous:
        raw_mem_id = payload.memberId or (
            str(actor.member.id) if actor.member else None
        )
        if raw_mem_id:
            mem_obj = (
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
            if mem_obj:
                mem_id = str(mem_obj.id)

    vis = (
        PrayerVisibility.PUBLIC if payload.isPublic else PrayerVisibility.PRIVATE
    )
    stat = PrayerStatus.ACTIVE

    prayer = PrayerRequest(
        title=payload.title.strip(),
        content=payload.description or "",
        status=stat,
        visibility=vis,
        member_id=mem_id,
        church_id=actor.church_id,
    )
    session.add(prayer)
    await session.flush()

    meta = dict(church.metadata_json or {})
    prayer_meta_map: Dict[str, Any] = dict(meta.get("prayer_meta") or {})
    counselors, _ = await _ensure_counselors(session, church, meta)
    counselors_by_id = {
        (c.get("id") or c.get("_id")): c for c in counselors if c.get("id") or c.get("_id")
    }

    status_disp = "In Progress" if payload.counselorId else "Active"
    pmeta = {
        "category": _normalize_category(payload.category),
        "priority": _normalize_priority(payload.priority),
        "isAnonymous": bool(payload.isAnonymous),
        "prayerCount": 1,
        "status_display": status_disp,
        "counselorId": payload.counselorId,
        "counselorName": (
            counselors_by_id[payload.counselorId]["name"]
            if payload.counselorId and payload.counselorId in counselors_by_id
            else None
        ),
        "updates": [],
    }
    prayer_meta_map[str(prayer.id)] = pmeta
    meta["prayer_meta"] = prayer_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    await session.commit()
    await session.refresh(prayer)

    queue_audit(
        background_tasks,
        request,
        "CREATE_PRAYER_REQUEST",
        "PRAYER_REQUEST",
        prayer.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    formatted = _format_prayer(prayer, mem_obj, None, pmeta, counselors_by_id)
    return {
        "success": True,
        "status": "success",
        "message": "Prayer request has been submitted successfully.",
        "data": formatted,
        "prayerRequest": formatted,
    }


@router.patch("")
@router.patch("/")
async def update_prayer_count(
    payload: PrayerCountPayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    pr_id = str(payload.prayerRequestId)

    meta = dict(church.metadata_json or {})
    prayer_meta_map: Dict[str, Any] = dict(meta.get("prayer_meta") or {})
    pmeta = dict(prayer_meta_map.get(pr_id) or {})
    cnt = int(pmeta.get("prayerCount", 1) or 0) + 1
    pmeta["prayerCount"] = cnt
    prayer_meta_map[pr_id] = pmeta
    meta["prayer_meta"] = prayer_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "success": True,
        "status": "success",
        "message": "Thank you for praying!",
        "data": {
            "prayerRequestId": pr_id,
            "prayerCount": cnt,
        },
    }


@router.post("/{prayer_id}/updates")
async def add_prayer_request_update(
    prayer_id: str,
    payload: Dict[str, Any],
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    prayer = (
        await session.execute(
            select(PrayerRequest).where(
                PrayerRequest.id == prayer_id,
                PrayerRequest.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not prayer:
        raise HTTPException(status_code=404, detail="Prayer request not found")

    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Update message is required")

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    prayer_meta_map: Dict[str, Any] = dict(meta.get("prayer_meta") or {})
    pmeta = dict(prayer_meta_map.get(str(prayer.id)) or {})
    updates = list(pmeta.get("updates") or [])

    author_name = (
        actor.member.full_name
        if actor.member
        else (current_user.display_name or "Church Admin")
    )
    now_iso = _iso(_now())
    updates.append(
        {
            "id": f"upd_{uuid.uuid4().hex[:8]}",
            "date": now_iso,
            "message": message,
            "author": author_name,
        }
    )
    pmeta["updates"] = updates

    new_status = payload.get("status")
    if new_status:
        st_norm = _normalize_status_display(prayer.status, new_status)
        pmeta["status_display"] = st_norm
        if st_norm == "Answered":
            prayer.status = PrayerStatus.ANSWERED
        elif st_norm == "Closed":
            prayer.status = PrayerStatus.CLOSED
        else:
            prayer.status = PrayerStatus.ACTIVE

    prayer.updated_at = _now()
    prayer_meta_map[str(prayer.id)] = pmeta
    meta["prayer_meta"] = prayer_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    await session.refresh(prayer)

    counselors, _ = await _ensure_counselors(session, church, meta)
    counselors_by_id = {
        (c.get("id") or c.get("_id")): c for c in counselors if c.get("id") or c.get("_id")
    }
    mem_obj = None
    if prayer.member_id:
        mem_obj = (
            await session.execute(
                select(Member).where(Member.id == prayer.member_id)
            )
        ).scalar_one_or_none()

    formatted = _format_prayer(prayer, mem_obj, None, pmeta, counselors_by_id)
    return {
        "success": True,
        "status": "success",
        "message": "Prayer request update added successfully",
        "data": formatted,
    }


@router.get("/{prayer_id}")
async def get_prayer_request(
    prayer_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    prayer_meta_map: Dict[str, Any] = dict(meta.get("prayer_meta") or {})
    counselors, _ = await _ensure_counselors(session, church, meta)
    counselors_by_id = {
        (c.get("id") or c.get("_id")): c for c in counselors if c.get("id") or c.get("_id")
    }

    row = (
        await session.execute(
            select(PrayerRequest, Member, User)
            .outerjoin(Member, Member.id == PrayerRequest.member_id)
            .outerjoin(User, User.id == Member.user_id)
            .where(
                PrayerRequest.id == prayer_id,
                PrayerRequest.church_id == actor.church_id,
            )
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Prayer request not found")

    pr, m, u = row
    formatted = _format_prayer(
        pr, m, u, prayer_meta_map.get(str(pr.id)), counselors_by_id
    )
    return {"success": True, "prayerRequest": formatted, "data": formatted}


@router.put("/{prayer_id}")
@router.patch("/{prayer_id}")
async def update_prayer_request(
    prayer_id: str,
    payload: PrayerUpdatePayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    prayer = (
        await session.execute(
            select(PrayerRequest).where(
                PrayerRequest.id == prayer_id,
                PrayerRequest.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not prayer:
        raise HTTPException(status_code=404, detail="Prayer request not found")

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    prayer_meta_map: Dict[str, Any] = dict(meta.get("prayer_meta") or {})
    pmeta = dict(prayer_meta_map.get(str(prayer.id)) or {})
    counselors, _ = await _ensure_counselors(session, church, meta)
    counselors_by_id = {
        (c.get("id") or c.get("_id")): c for c in counselors if c.get("id") or c.get("_id")
    }

    if payload.title is not None and payload.title.strip():
        prayer.title = payload.title.strip()
    if payload.description is not None:
        prayer.content = payload.description
    if payload.isPublic is not None:
        prayer.visibility = (
            PrayerVisibility.PUBLIC if payload.isPublic else PrayerVisibility.PRIVATE
        )
    if payload.isAnonymous is not None:
        pmeta["isAnonymous"] = bool(payload.isAnonymous)
    if payload.category is not None:
        pmeta["category"] = _normalize_category(payload.category)
    if payload.priority is not None:
        pmeta["priority"] = _normalize_priority(payload.priority)

    if payload.counselorId is not None:
        cid = payload.counselorId.strip() if payload.counselorId else None
        pmeta["counselorId"] = cid
        pmeta["counselorName"] = (
            counselors_by_id[cid]["name"]
            if cid and cid in counselors_by_id
            else None
        )
        if cid and pmeta.get("status_display", "Active") == "Active" and not payload.status:
            pmeta["status_display"] = "In Progress"

    if payload.status is not None:
        st_disp = _normalize_status_display(prayer.status, payload.status)
        pmeta["status_display"] = st_disp
        if st_disp == "Answered":
            prayer.status = PrayerStatus.ANSWERED
        elif st_disp == "Closed":
            prayer.status = PrayerStatus.CLOSED
        else:
            prayer.status = PrayerStatus.ACTIVE

    prayer.updated_at = _now()
    prayer_meta_map[str(prayer.id)] = pmeta
    meta["prayer_meta"] = prayer_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()
    await session.refresh(prayer)

    mem_obj = None
    if prayer.member_id:
        mem_obj = (
            await session.execute(
                select(Member).where(Member.id == prayer.member_id)
            )
        ).scalar_one_or_none()

    formatted = _format_prayer(prayer, mem_obj, None, pmeta, counselors_by_id)
    return {
        "success": True,
        "status": "success",
        "message": "Prayer request updated successfully",
        "data": formatted,
    }


@router.delete("/{prayer_id}")
async def delete_prayer_request(
    prayer_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    prayer = (
        await session.execute(
            select(PrayerRequest).where(
                PrayerRequest.id == prayer_id,
                PrayerRequest.church_id == actor.church_id,
            )
        )
    ).scalar_one_or_none()
    if not prayer:
        raise HTTPException(status_code=404, detail="Prayer request not found")

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    prayer_meta_map: Dict[str, Any] = dict(meta.get("prayer_meta") or {})
    prayer_meta_map.pop(str(prayer.id), None)
    meta["prayer_meta"] = prayer_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    await session.delete(prayer)
    await session.commit()
    return {
        "success": True,
        "status": "success",
        "message": "Prayer request deleted successfully",
    }

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
from typing import Any, Dict, List, Optional

from app.core.deps import CurrentUser, DbSession, church_id_from, resolve_actor
from app.enums import AssetCondition, AssetStatus
from app.models.church import Branch, Church
from app.models.operation import Asset, MaintenanceRecord
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
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


_ASSET_TYPE_LABELS = {
    "vehicle": "Vehicle",
    "property": "Property",
    "equipment": "Equipment",
    "furniture": "Furniture",
    "technology": "Technology",
    "musical": "Musical Instruments",
    "computer": "Computer",
    "appliance": "Appliance",
    "building-material": "Building Material",
    "book": "Book/Literature",
    "other": "Other",
}

_ASSET_TYPE_CATEGORIES = {
    "Vehicle": "Transportation",
    "Property": "Real Estate",
    "Equipment": "Audio/Visual & Equipment",
    "Furniture": "Furniture & Fixtures",
    "Technology": "IT & Electronics",
    "Musical Instruments": "Worship & Music",
    "Computer": "IT & Electronics",
    "Appliance": "Facilities",
    "Building Material": "Construction",
    "Book/Literature": "Education",
    "Other": "General",
}

_SCHEDULE_DAYS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
    "quarterly": 90,
    "annually": 365,
}


def _compute_next_maintenance(base_dt: datetime, schedule: str) -> str:
    days = _SCHEDULE_DAYS.get((schedule or "monthly").lower(), 30)
    return (base_dt + timedelta(days=days)).strftime("%Y-%m-%d")


def _normalize_asset_meta(
    payload: Optional["AssetPayload"] = None,
    existing: Optional[Dict[str, Any]] = None,
    purchase_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    base = dict(existing or {})
    if payload is not None:
        if payload.type is not None and payload.type.strip():
            raw_t = payload.type.strip()
            norm_t = _ASSET_TYPE_LABELS.get(raw_t.lower(), raw_t.title())
            base["type"] = norm_t
            if not payload.category:
                base["category"] = _ASSET_TYPE_CATEGORIES.get(norm_t, norm_t)
        if payload.category is not None and payload.category.strip():
            base["category"] = payload.category.strip()
        if payload.serialNumber is not None:
            base["serialNumber"] = payload.serialNumber.strip()
        if payload.warranty is not None:
            base["warranty"] = payload.warranty.strip()
        if payload.supplier is not None:
            base["supplier"] = payload.supplier.strip()
        if payload.maintenanceSchedule is not None and payload.maintenanceSchedule.strip():
            base["maintenanceSchedule"] = payload.maintenanceSchedule.strip().lower()
        if payload.lastMaintenance is not None and payload.lastMaintenance.strip():
            base["lastMaintenance"] = payload.lastMaintenance.strip()[:10]
        if payload.nextMaintenance is not None and payload.nextMaintenance.strip():
            base["nextMaintenance"] = payload.nextMaintenance.strip()[:10]
        if payload.notes is not None:
            base["notes"] = payload.notes.strip()

    asset_type = base.get("type") or "Equipment"
    category = base.get("category") or _ASSET_TYPE_CATEGORIES.get(asset_type, asset_type)
    schedule = base.get("maintenanceSchedule") or "monthly"
    ref_dt = purchase_dt or _now()
    last_maint = base.get("lastMaintenance") or ref_dt.strftime("%Y-%m-%d")
    next_maint = base.get("nextMaintenance")
    if not next_maint:
        try:
            last_dt = datetime.strptime(last_maint[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            last_dt = ref_dt
        next_maint = _compute_next_maintenance(last_dt, schedule)

    return {
        "type": asset_type,
        "category": category,
        "serialNumber": base.get("serialNumber") or "N/A",
        "warranty": base.get("warranty") or "N/A",
        "supplier": base.get("supplier") or "",
        "maintenanceSchedule": schedule,
        "lastMaintenance": last_maint,
        "nextMaintenance": next_maint,
        "notes": base.get("notes") or "",
        "maintenance_meta": dict(base.get("maintenance_meta") or {}),
    }


def _format_asset(
    asset: Asset,
    branch_name: str = "",
    meta_entry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cond_raw = (
        asset.condition.value
        if hasattr(asset.condition, "value")
        else str(asset.condition)
    )
    stat_raw = (
        asset.status.value
        if hasattr(asset.status, "value")
        else str(asset.status)
    )
    norm_meta = _normalize_asset_meta(None, meta_entry, asset.purchase_date or asset.created_at)

    return {
        "_id": str(asset.id),
        "id": str(asset.id),
        "name": asset.name,
        "type": norm_meta["type"],
        "category": norm_meta["category"],
        "value": float(asset.value) if asset.value is not None else 0.0,
        "purchaseDate": _iso(asset.purchase_date or asset.created_at),
        "condition": cond_raw.title(),
        "status": stat_raw.title(),
        "branchId": str(asset.branch_id) if asset.branch_id else None,
        "branch": branch_name or "Main Campus",
        "location": branch_name or "Main Campus",
        "serialNumber": norm_meta["serialNumber"],
        "warranty": norm_meta["warranty"],
        "supplier": norm_meta["supplier"],
        "maintenanceSchedule": norm_meta["maintenanceSchedule"],
        "lastMaintenance": norm_meta["lastMaintenance"],
        "nextMaintenance": norm_meta["nextMaintenance"],
        "notes": norm_meta["notes"] or asset.description or "",
        "description": asset.description or norm_meta["notes"] or "",
        "createdAt": _iso(asset.created_at),
        "updatedAt": _iso(asset.updated_at),
    }


class AssetPayload(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = "Equipment"
    category: Optional[str] = None
    value: Any = 0
    purchaseDate: Optional[str] = None
    condition: Optional[str] = "good"
    status: Optional[str] = "active"
    branchId: Optional[str] = None
    branch_id: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None
    serialNumber: Optional[str] = None
    warranty: Optional[str] = None
    supplier: Optional[str] = None
    maintenanceSchedule: Optional[str] = "monthly"
    lastMaintenance: Optional[str] = None
    nextMaintenance: Optional[str] = None


class MaintenancePayload(BaseModel):
    date: Optional[str] = None
    cost: Any = 0
    description: str
    type: Optional[str] = "Routine Maintenance"
    performedBy: Optional[str] = None
    status: Optional[str] = "Completed"
    nextMaintenanceDate: Optional[str] = None
    setAssetStatus: Optional[str] = None


class BulkAssetImportPayload(BaseModel):
    assets: List[AssetPayload]


@router.get("/stats")
async def get_asset_stats(
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church = await _get_church(session, actor.church_id)
    asset_meta_map: Dict[str, Any] = dict(
        (church.metadata_json or {}).get("asset_meta") or {}
    )

    rows = (
        await session.execute(
            select(Asset, Branch.name)
            .outerjoin(Branch, Branch.id == Asset.branch_id)
            .where(Asset.church_id == actor.church_id)
        )
    ).all()

    formatted = [
        _format_asset(a, b_name or "", asset_meta_map.get(str(a.id)))
        for a, b_name in rows
    ]

    total_value = sum(float(a.get("value") or 0) for a in formatted)
    vehicles = sum(1 for a in formatted if (a.get("type") or "").lower() == "vehicle")
    properties = sum(1 for a in formatted if (a.get("type") or "").lower() == "property")
    equipment = sum(
        1
        for a in formatted
        if (a.get("type") or "").lower()
        not in ("vehicle", "property")
    )
    maintenance_due = sum(
        1
        for a in formatted
        if (a.get("status") or "").lower() == "maintenance"
    )

    stats_data = {
        "total": len(formatted),
        "totalValue": total_value,
        "vehicles": vehicles,
        "properties": properties,
        "equipment": equipment,
        "maintenance": maintenance_due,
    }
    return {"status": "success", "stats": stats_data, "data": stats_data}


@router.get("")
@router.get("/")
async def list_assets(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: str = Query("", max_length=100),
    branch_id: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    condition: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    church = await _get_church(session, actor.church_id)
    asset_meta_map: Dict[str, Any] = dict(
        (church.metadata_json or {}).get("asset_meta") or {}
    )

    conds = [Asset.church_id == actor.church_id]
    if branch_id and branch_id.lower() != "all":
        conds.append(Asset.branch_id == branch_id)

    rows = (
        (
            await session.execute(
                select(Asset, Branch.name)
                .outerjoin(Branch, Branch.id == Asset.branch_id)
                .where(*conds)
                .order_by(Asset.created_at.desc())
            )
        )
        .all()
    )

    all_formatted = [
        _format_asset(a, b_name or "", asset_meta_map.get(str(a.id)))
        for a, b_name in rows
    ]

    if search:
        s = search.strip().lower()
        all_formatted = [
            item
            for item in all_formatted
            if s in (item.get("name") or "").lower()
            or s in (item.get("category") or "").lower()
            or s in (item.get("type") or "").lower()
            or s in (item.get("location") or "").lower()
            or s in (item.get("serialNumber") or "").lower()
        ]
    if type and type.lower() != "all":
        t_low = type.lower()
        if t_low in ("vehicles", "vehicle"):
            all_formatted = [
                item for item in all_formatted if (item.get("type") or "").lower() == "vehicle"
            ]
        elif t_low in ("property", "properties"):
            all_formatted = [
                item for item in all_formatted if (item.get("type") or "").lower() == "property"
            ]
        elif t_low == "equipment":
            all_formatted = [
                item
                for item in all_formatted
                if (item.get("type") or "").lower() not in ("vehicle", "property")
            ]
        else:
            all_formatted = [
                item for item in all_formatted if (item.get("type") or "").lower() == t_low
            ]
    if condition and condition.lower() != "all":
        all_formatted = [
            item
            for item in all_formatted
            if (item.get("condition") or "").lower() == condition.lower()
        ]
    if status and status.lower() != "all":
        all_formatted = [
            item
            for item in all_formatted
            if (item.get("status") or "").lower() == status.lower()
        ]

    total = len(all_formatted)
    offset = (page - 1) * limit
    formatted = all_formatted[offset : offset + limit]
    pages = max(1, math.ceil(total / limit)) if limit else 1
    response.headers["X-Total-Count"] = str(total)

    return {
        "assets": formatted,
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


@router.post("")
@router.post("/")
async def create_asset(
    request: Request,
    payload: AssetPayload,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Asset name is required")

    try:
        val = Decimal(str(payload.value))
    except Exception:
        val = Decimal("0.00")

    p_date = _now()
    if payload.purchaseDate:
        try:
            p_date = datetime.strptime(payload.purchaseDate[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            p_date = _now()

    raw_cond = (payload.condition or "good").upper()
    cond_enum = (
        AssetCondition[raw_cond]
        if raw_cond in AssetCondition.__members__
        else AssetCondition.GOOD
    )

    raw_stat = (payload.status or "active").upper()
    stat_enum = (
        AssetStatus[raw_stat]
        if raw_stat in AssetStatus.__members__
        else AssetStatus.ACTIVE
    )

    branch_id = payload.branchId or payload.branch_id
    branch_name = "Main Campus"
    if branch_id:
        b_row = (
            await session.execute(
                select(Branch).where(
                    Branch.id == branch_id, Branch.church_id == actor.church_id
                )
            )
        ).scalar_one_or_none()
        if b_row:
            branch_name = b_row.name
        else:
            branch_id = None

    desc_text = payload.notes if payload.notes is not None else (payload.description or "")

    asset = Asset(
        name=name,
        church_id=actor.church_id,
        branch_id=branch_id,
        value=val,
        purchase_date=p_date,
        condition=cond_enum,
        status=stat_enum,
        description=desc_text,
    )
    session.add(asset)
    await session.flush()

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    asset_meta_map = dict(meta.get("asset_meta") or {})
    norm_meta = _normalize_asset_meta(payload, None, p_date)
    asset_meta_map[str(asset.id)] = norm_meta
    meta["asset_meta"] = asset_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    await session.commit()
    await session.refresh(asset)

    queue_audit(
        background_tasks,
        request,
        "CREATE_ASSET",
        "ASSET",
        asset.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    formatted = _format_asset(asset, branch_name, norm_meta)
    return {
        "status": "success",
        "message": "Asset created successfully",
        "asset": formatted,
        "data": formatted,
    }


@router.post("/import")
async def import_assets(
    payload: BulkAssetImportPayload,
    request: Request,
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
    asset_meta_map = dict(meta.get("asset_meta") or {})

    default_branch = (
        await session.execute(
            select(Branch).where(Branch.church_id == actor.church_id).limit(1)
        )
    ).scalar_one_or_none()

    created_list: List[Dict[str, Any]] = []
    for item in payload.assets:
        name = (item.name or "").strip()
        if not name:
            continue
        try:
            val = Decimal(str(item.value or 0))
        except Exception:
            val = Decimal("0.00")

        p_date = _now()
        if item.purchaseDate:
            try:
                p_date = datetime.strptime(item.purchaseDate[:10], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except Exception:
                p_date = _now()

        raw_cond = (item.condition or "good").upper()
        cond_enum = (
            AssetCondition[raw_cond]
            if raw_cond in AssetCondition.__members__
            else AssetCondition.GOOD
        )
        raw_stat = (item.status or "active").upper()
        stat_enum = (
            AssetStatus[raw_stat]
            if raw_stat in AssetStatus.__members__
            else AssetStatus.ACTIVE
        )

        b_id = item.branchId or item.branch_id or (str(default_branch.id) if default_branch else None)
        b_name = default_branch.name if default_branch else "Main Campus"

        asset = Asset(
            name=name,
            church_id=actor.church_id,
            branch_id=b_id,
            value=val,
            purchase_date=p_date,
            condition=cond_enum,
            status=stat_enum,
            description=item.notes or item.description or "",
        )
        session.add(asset)
        await session.flush()

        norm_meta = _normalize_asset_meta(item, None, p_date)
        asset_meta_map[str(asset.id)] = norm_meta
        created_list.append(_format_asset(asset, b_name, norm_meta))

    meta["asset_meta"] = asset_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")
    await session.commit()

    return {
        "status": "success",
        "message": f"Imported {len(created_list)} assets successfully",
        "importedCount": len(created_list),
        "assets": created_list,
        "data": created_list,
    }


@router.get("/{asset_id}")
async def get_asset(
    asset_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    row = (
        await session.execute(
            select(Asset, Branch.name)
            .outerjoin(Branch, Branch.id == Asset.branch_id)
            .where(Asset.id == asset_id, Asset.church_id == actor.church_id)
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    church = await _get_church(session, actor.church_id)
    asset_meta_map = dict((church.metadata_json or {}).get("asset_meta") or {})
    asset, b_name = row
    formatted = _format_asset(asset, b_name or "", asset_meta_map.get(str(asset.id)))
    return {"asset": formatted, "data": {"asset": formatted}}


@router.put("/{asset_id}")
@router.patch("/{asset_id}")
async def update_asset(
    asset_id: str,
    payload: AssetPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    asset = (
        await session.execute(
            select(Asset).where(
                Asset.id == asset_id, Asset.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    if payload.name is not None and payload.name.strip():
        asset.name = payload.name.strip()
    if payload.description is not None or payload.notes is not None:
        asset.description = (
            payload.notes if payload.notes is not None else payload.description
        )
    if payload.value is not None and str(payload.value) != "":
        try:
            asset.value = Decimal(str(payload.value))
        except Exception:
            pass
    if payload.purchaseDate:
        try:
            asset.purchase_date = datetime.strptime(
                payload.purchaseDate[:10], "%Y-%m-%d"
            ).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    if payload.condition:
        raw_cond = payload.condition.upper()
        if raw_cond in AssetCondition.__members__:
            asset.condition = AssetCondition[raw_cond]
    if payload.status:
        raw_stat = payload.status.upper()
        if raw_stat in AssetStatus.__members__:
            asset.status = AssetStatus[raw_stat]

    b_id = payload.branchId or payload.branch_id
    if b_id:
        b_row = (
            await session.execute(
                select(Branch).where(
                    Branch.id == b_id, Branch.church_id == actor.church_id
                )
            )
        ).scalar_one_or_none()
        if b_row:
            asset.branch_id = b_row.id

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    asset_meta_map = dict(meta.get("asset_meta") or {})
    updated_meta = _normalize_asset_meta(
        payload,
        asset_meta_map.get(str(asset.id)),
        asset.purchase_date or asset.created_at,
    )
    asset_meta_map[str(asset.id)] = updated_meta
    meta["asset_meta"] = asset_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    asset.updated_at = _now()
    await session.commit()
    await session.refresh(asset)

    branch_name = "Main Campus"
    if asset.branch_id:
        b_name = (
            await session.execute(select(Branch.name).where(Branch.id == asset.branch_id))
        ).scalar_one_or_none()
        if b_name:
            branch_name = b_name

    queue_audit(
        background_tasks,
        request,
        "UPDATE_ASSET",
        "ASSET",
        asset.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    formatted = _format_asset(asset, branch_name, updated_meta)
    return {
        "status": "success",
        "message": "Asset updated successfully",
        "asset": formatted,
        "data": formatted,
    }


@router.delete("/{asset_id}")
async def delete_asset(
    asset_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    asset = (
        await session.execute(
            select(Asset).where(
                Asset.id == asset_id, Asset.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    asset_meta_map = dict(meta.get("asset_meta") or {})
    if str(asset.id) in asset_meta_map:
        asset_meta_map.pop(str(asset.id), None)
        meta["asset_meta"] = asset_meta_map
        church.metadata_json = meta
        flag_modified(church, "metadata_json")

    await session.delete(asset)
    await session.commit()

    queue_audit(
        background_tasks,
        request,
        "DELETE_ASSET",
        "ASSET",
        asset_id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    return {"status": "success", "message": "Asset deleted successfully"}


# ---------------------------------------------------------------------------
# Asset Maintenance Schedule & History
# ---------------------------------------------------------------------------
@router.get("/{asset_id}/maintenance")
async def list_asset_maintenance(
    asset_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    asset = (
        await session.execute(
            select(Asset).where(
                Asset.id == asset_id, Asset.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    church = await _get_church(session, actor.church_id)
    asset_meta_map = dict((church.metadata_json or {}).get("asset_meta") or {})
    this_meta = dict(asset_meta_map.get(str(asset.id)) or {})
    maint_meta_map = dict(this_meta.get("maintenance_meta") or {})

    records = (
        await session.execute(
            select(MaintenanceRecord)
            .where(MaintenanceRecord.asset_id == asset.id)
            .order_by(MaintenanceRecord.date.desc())
        )
    ).scalars().all()

    formatted_records: List[Dict[str, Any]] = []
    total_cost = 0.0
    for r in records:
        cost_val = float(r.cost) if r.cost is not None else 0.0
        total_cost += cost_val
        extra = dict(maint_meta_map.get(str(r.id)) or {})
        formatted_records.append(
            {
                "id": str(r.id),
                "_id": str(r.id),
                "assetId": str(asset.id),
                "assetName": asset.name,
                "date": _iso(r.date),
                "cost": cost_val,
                "description": r.description or "",
                "type": extra.get("type") or "Routine Maintenance",
                "performedBy": extra.get("performedBy") or "Church Maintenance Team",
                "status": extra.get("status") or "Completed",
                "createdAt": _iso(r.created_at),
            }
        )

    return {
        "status": "success",
        "records": formatted_records,
        "totalCost": total_cost,
        "data": {
            "records": formatted_records,
            "total": len(formatted_records),
            "totalCost": total_cost,
        },
    }


@router.post("/{asset_id}/maintenance")
async def create_asset_maintenance(
    asset_id: str,
    payload: MaintenancePayload,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    asset = (
        await session.execute(
            select(Asset).where(
                Asset.id == asset_id, Asset.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    try:
        cost_dec = Decimal(str(payload.cost or 0))
    except Exception:
        cost_dec = Decimal("0.00")

    maint_dt = _now()
    if payload.date:
        try:
            maint_dt = datetime.strptime(payload.date[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            maint_dt = _now()

    record = MaintenanceRecord(
        asset_id=asset.id,
        cost=cost_dec,
        date=maint_dt,
        description=payload.description.strip(),
    )
    session.add(record)
    await session.flush()

    church = await _get_church(session, actor.church_id)
    meta = dict(church.metadata_json or {})
    asset_meta_map = dict(meta.get("asset_meta") or {})
    norm_meta = _normalize_asset_meta(
        None,
        asset_meta_map.get(str(asset.id)),
        asset.purchase_date or asset.created_at,
    )

    maint_meta_map = dict(norm_meta.get("maintenance_meta") or {})
    maint_meta_map[str(record.id)] = {
        "type": payload.type or "Routine Maintenance",
        "performedBy": payload.performedBy or "Church Maintenance Team",
        "status": payload.status or "Scheduled",
    }
    norm_meta["maintenance_meta"] = maint_meta_map

    # Update lastMaintenance and nextMaintenance dates
    date_str = maint_dt.strftime("%Y-%m-%d")
    if (payload.status or "").lower() == "completed":
        norm_meta["lastMaintenance"] = date_str
        norm_meta["nextMaintenance"] = (
            payload.nextMaintenanceDate[:10]
            if payload.nextMaintenanceDate
            else _compute_next_maintenance(maint_dt, norm_meta["maintenanceSchedule"])
        )
    else:
        norm_meta["nextMaintenance"] = (
            payload.nextMaintenanceDate[:10]
            if payload.nextMaintenanceDate
            else date_str
        )

    if payload.setAssetStatus:
        raw_stat = payload.setAssetStatus.upper()
        if raw_stat in AssetStatus.__members__:
            asset.status = AssetStatus[raw_stat]

    asset_meta_map[str(asset.id)] = norm_meta
    meta["asset_meta"] = asset_meta_map
    church.metadata_json = meta
    flag_modified(church, "metadata_json")

    asset.updated_at = _now()
    await session.commit()
    await session.refresh(record)

    record_out = {
        "id": str(record.id),
        "_id": str(record.id),
        "assetId": str(asset.id),
        "assetName": asset.name,
        "date": _iso(record.date),
        "cost": float(record.cost),
        "description": record.description,
        "type": payload.type or "Routine Maintenance",
        "performedBy": payload.performedBy or "Church Maintenance Team",
        "status": payload.status or "Scheduled",
        "createdAt": _iso(record.created_at),
    }

    return {
        "status": "success",
        "message": "Maintenance record saved successfully",
        "record": record_out,
        "data": record_out,
    }


@router.delete("/{asset_id}/maintenance/{record_id}")
async def delete_asset_maintenance(
    asset_id: str,
    record_id: str,
    request: Request,
    current_user: CurrentUser,
    session: DbSession,
):
    actor = await resolve_actor(session, current_user, church_id_from(request))
    asset = (
        await session.execute(
            select(Asset).where(
                Asset.id == asset_id, Asset.church_id == actor.church_id
            )
        )
    ).scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    record = (
        await session.execute(
            select(MaintenanceRecord).where(
                MaintenanceRecord.id == record_id,
                MaintenanceRecord.asset_id == asset.id,
            )
        )
    ).scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Maintenance record not found")

    await session.delete(record)
    await session.commit()

    return {"status": "success", "message": "Maintenance record deleted successfully"}

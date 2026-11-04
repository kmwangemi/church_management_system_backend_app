from datetime import datetime, timezone
from decimal import Decimal
import math
from typing import Any, Dict, List, Optional

from app.core.deps import CurrentUser, DbSession, church_id_from, resolve_actor
from app.enums import AssetCondition, AssetStatus
from app.models.church import Branch
from app.models.operation import Asset
from app.services.audit import queue_audit
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _format_asset(asset: Asset, branch_name: str = "") -> Dict[str, Any]:
    cond_val = asset.condition.value.lower() if hasattr(asset.condition, "value") else str(asset.condition).lower()
    stat_val = asset.status.value.lower() if hasattr(asset.status, "value") else str(asset.status).lower()
    return {
        "_id": str(asset.id),
        "id": str(asset.id),
        "name": asset.name,
        "type": "Equipment",
        "category": "Equipment",
        "value": float(asset.value) if asset.value else 0.0,
        "purchaseDate": _iso(asset.purchase_date),
        "condition": cond_val,
        "status": stat_val,
        "branchId": str(asset.branch_id) if asset.branch_id else None,
        "branch": branch_name or "Main Sanctuary",
        "location": branch_name or "Main Sanctuary",
        "description": asset.description or "",
        "createdAt": _iso(asset.created_at),
        "updatedAt": _iso(asset.updated_at),
    }


class AssetPayload(BaseModel):
    name: str
    type: Optional[str] = "Equipment"
    value: Any = 0
    purchaseDate: Optional[str] = None
    condition: Optional[str] = "good"
    status: Optional[str] = "active"
    branchId: Optional[str] = None
    branch_id: Optional[str] = None
    description: Optional[str] = None
    serialNumber: Optional[str] = None
    warranty: Optional[str] = None
    supplier: Optional[str] = None


@router.get("")
@router.get("/")
async def list_assets(
    request: Request,
    response: Response,
    current_user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
    search: str = Query("", max_length=100),
    branch_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    church_id: Optional[str] = Query(None),
):
    actor = await resolve_actor(
        session, current_user, church_id_from(request, church_id)
    )
    conds = [Asset.church_id == actor.church_id]
    if branch_id:
        conds.append(Asset.branch_id == branch_id)
    if search:
        s = f"%{search.strip()}%"
        conds.append(or_(Asset.name.ilike(s), Asset.description.ilike(s)))

    total = (
        await session.execute(select(func.count(Asset.id)).where(*conds))
    ).scalar() or 0

    offset = (page - 1) * limit
    rows = (
        (
            await session.execute(
                select(Asset, Branch.name)
                .outerjoin(Branch, Branch.id == Asset.branch_id)
                .where(*conds)
                .order_by(Asset.name)
                .offset(offset)
                .limit(limit)
            )
        )
        .all()
    )

    formatted = [_format_asset(a, b_name or "") for a, b_name in rows]
    pages = math.ceil(total / limit) if limit else 1
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

    try:
        val = Decimal(str(payload.value))
    except Exception:
        val = Decimal("0.00")

    p_date = None
    if payload.purchaseDate:
        try:
            p_date = datetime.strptime(payload.purchaseDate, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            p_date = _now()

    raw_cond = (payload.condition or "good").upper()
    cond_enum = AssetCondition[raw_cond] if raw_cond in AssetCondition.__members__ else AssetCondition.GOOD

    raw_stat = (payload.status or "active").upper()
    stat_enum = AssetStatus[raw_stat] if raw_stat in AssetStatus.__members__ else AssetStatus.ACTIVE

    branch_id = payload.branchId or payload.branch_id
    if branch_id:
        b_exists = (
            await session.execute(
                select(Branch.id).where(
                    Branch.id == branch_id, Branch.church_id == actor.church_id
                )
            )
        ).scalar_one_or_none()
        if not b_exists:
            branch_id = None

    asset = Asset(
        name=payload.name.strip(),
        church_id=actor.church_id,
        branch_id=branch_id,
        value=val,
        purchase_date=p_date,
        condition=cond_enum,
        status=stat_enum,
        description=payload.description or "",
    )
    session.add(asset)
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

    formatted = _format_asset(asset)
    return {
        "status": "success",
        "message": "Asset created successfully",
        "asset": formatted,
        "data": formatted,
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

    asset, b_name = row
    formatted = _format_asset(asset, b_name or "")
    return {"asset": formatted, "data": {"asset": formatted}}


@router.put("/{asset_id}")
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

    if payload.name:
        asset.name = payload.name.strip()
    if payload.description is not None:
        asset.description = payload.description
    if payload.value is not None:
        try:
            asset.value = Decimal(str(payload.value))
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

    asset.updated_at = _now()
    await session.commit()
    await session.refresh(asset)

    queue_audit(
        background_tasks,
        request,
        "UPDATE_ASSET",
        "ASSET",
        asset.id,
        actor_user_id=current_user.id,
        church_id=actor.church_id,
    )

    formatted = _format_asset(asset)
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


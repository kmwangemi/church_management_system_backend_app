from app.routers import (
    admin,
    auth,
    events,
    finances,
    groups,
    superadmin,
    upload,
    users,
)
from fastapi import APIRouter

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
api_router.include_router(users.router, prefix="/users", tags=["Users"])
api_router.include_router(groups.router, prefix="/groups", tags=["Groups"])
api_router.include_router(events.router, prefix="/events", tags=["Events"])
api_router.include_router(finances.router, prefix="/finances", tags=["Finances"])
api_router.include_router(superadmin.router, prefix="/superadmin", tags=["Superadmin"])
api_router.include_router(admin.router, prefix="/admin", tags=["Admin"])
# Alias for clients that call /church/...; hidden from the docs so each endpoint is listed once.
api_router.include_router(
    admin.router, prefix="/church", tags=["Church"], include_in_schema=False
)
# The upload router already defines BOTH /upload and /shared/upload, so mount it once, with no prefix.
# (It was mounted twice, which registered /shared/upload twice and /shared/shared/upload.)
api_router.include_router(upload.router, tags=["Upload"])

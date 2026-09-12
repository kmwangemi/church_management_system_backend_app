from fastapi import APIRouter
from app.api.endpoints import auth, users, groups, events, finances

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(groups.router, prefix="/groups", tags=["groups"])
api_router.include_router(events.router, prefix="/events", tags=["events"])
api_router.include_router(finances.router, prefix="/finances", tags=["finances"])

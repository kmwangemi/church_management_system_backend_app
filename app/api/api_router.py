from app.routers import (
    admin,
    assets,
    attendance,
    auth,
    communications,
    departments,
    discipleship,
    events,
    finances,
    groups,
    prayer,
    superadmin,
    upload,
    users,
    volunteers,
)
from fastapi import APIRouter

api_router = APIRouter()

# Core Auth & Users
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
api_router.include_router(users.router, prefix="/users", tags=["Users"])

# Superadmin & Admin
api_router.include_router(superadmin.router, prefix="/superadmin", tags=["Superadmin"])
api_router.include_router(admin.router, prefix="/admin", tags=["Admin"])
api_router.include_router(admin.router, prefix="/church", tags=["Church"], include_in_schema=False)

# Departments
api_router.include_router(departments.router, prefix="/church/departments", tags=["Departments"])
api_router.include_router(departments.router, prefix="/admin/departments", tags=["Departments"], include_in_schema=False)
api_router.include_router(departments.router, prefix="/departments", tags=["Departments"], include_in_schema=False)

# Small Groups
api_router.include_router(groups.router, prefix="/church/groups", tags=["Groups"])
api_router.include_router(groups.router, prefix="/admin/groups", tags=["Groups"], include_in_schema=False)
api_router.include_router(groups.router, prefix="/groups", tags=["Groups"], include_in_schema=False)

# Attendance
api_router.include_router(attendance.router, prefix="/church/attendance", tags=["Attendance"])
api_router.include_router(attendance.router, prefix="/admin/attendance", tags=["Attendance"], include_in_schema=False)
api_router.include_router(attendance.router, prefix="/attendance", tags=["Attendance"], include_in_schema=False)

# Events
api_router.include_router(events.router, prefix="/church/events", tags=["Events"])
api_router.include_router(events.router, prefix="/admin/events", tags=["Events"], include_in_schema=False)
api_router.include_router(events.router, prefix="/events", tags=["Events"], include_in_schema=False)

# Finances, Offerings & Pledges
api_router.include_router(finances.router, prefix="/church", tags=["Finances"])
api_router.include_router(finances.router, prefix="/admin", tags=["Finances"], include_in_schema=False)
api_router.include_router(finances.router, prefix="/finances", tags=["Finances"], include_in_schema=False)

# Assets
api_router.include_router(assets.router, prefix="/church/assets", tags=["Assets"])
api_router.include_router(assets.router, prefix="/admin/assets", tags=["Assets"], include_in_schema=False)
api_router.include_router(assets.router, prefix="/assets", tags=["Assets"], include_in_schema=False)

# Communications (Announcements & Messages)
api_router.include_router(communications.router, prefix="/church", tags=["Communications"])
api_router.include_router(communications.router, prefix="/admin", tags=["Communications"], include_in_schema=False)

# Discipleship (Disciples & Milestones)
api_router.include_router(discipleship.router, prefix="/church", tags=["Discipleship"])
api_router.include_router(discipleship.router, prefix="/admin", tags=["Discipleship"], include_in_schema=False)

# Volunteers
api_router.include_router(volunteers.router, prefix="/church/volunteers", tags=["Volunteers"])
api_router.include_router(volunteers.router, prefix="/admin/volunteers", tags=["Volunteers"], include_in_schema=False)
api_router.include_router(volunteers.router, prefix="/volunteers", tags=["Volunteers"], include_in_schema=False)

# Prayer Requests
api_router.include_router(prayer.router, prefix="/church/prayer-requests", tags=["Prayer"])
api_router.include_router(prayer.router, prefix="/admin/prayer-requests", tags=["Prayer"], include_in_schema=False)
api_router.include_router(prayer.router, prefix="/prayer-requests", tags=["Prayer"], include_in_schema=False)

# Uploads
api_router.include_router(upload.router, tags=["Upload"])

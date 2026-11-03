from app.schemas.auth import UserResponse
from app.core.deps import CurrentUser
from fastapi import APIRouter

router = APIRouter()


@router.get("/me", response_model=UserResponse)
async def read_user_me(current_user: CurrentUser):
    """
    Get the logged-in user (the login identity only).
    For the user's church memberships and roles use GET /auth/verify.
    """
    return current_user

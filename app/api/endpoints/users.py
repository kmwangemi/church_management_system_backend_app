from fastapi import APIRouter
from app.api.deps import CurrentUser
from app.schemas.user import UserResponse

router = APIRouter()

@router.get("/me", response_model=UserResponse)
async def read_user_me(current_user: CurrentUser):
    """
    Get current user.
    """
    return current_user

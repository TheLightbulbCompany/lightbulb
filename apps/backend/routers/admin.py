"""Admin dashboard router — /api/v1/admin/*

Every endpoint is gated by Depends(require_platform_admin): the surface is for
human operators authenticated by Clerk. The Spark metrics live at /ops/spark.
"""

from fastapi import APIRouter, Depends

from core.auth import AuthContext, require_platform_admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/me", description="Auth probe: returns the calling admin's profile or 403.")
async def admin_me(auth: AuthContext = Depends(require_platform_admin)):
    return {"user_id": auth.user_id, "email": auth.email, "is_admin": True}

"""Admin page: scrub rules and the references they are derived from."""
from fastapi import APIRouter, Depends, HTTPException

from ..models import User
from ..security import get_current_user
from ..services import rule_catalog

router = APIRouter(prefix="/api/rule-catalog", tags=["rule-catalog"])


@router.get("")
def get_catalog(user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(403, "Admins only")
    return rule_catalog.catalog()
